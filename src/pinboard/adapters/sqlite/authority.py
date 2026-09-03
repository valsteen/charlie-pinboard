"""Read and change authority records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row, stale_write
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure


def validate_attempt_authority(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    attempt_counters = {value.attempt_id: value.generation_high_water for value in state.authority.attempt_counters}
    for anchor in state.authority.attempt_generations:
        high_water = attempt_counters.get(anchor.attempt_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "An attempt generation exceeds its retained counter.")
    for lease in state.authority.attempt_leases:
        high_water = attempt_counters.get(lease.attempt_id)
        if high_water is None or lease.generation != high_water:
            raise StorageError(error_code, "The current attempt lease does not match its retained counter.")
    preparation_counters = {
        value.item_id: value.generation_high_water for value in state.authority.preparation_counters
    }
    for anchor in state.authority.preparation_generations:
        high_water = preparation_counters.get(anchor.item_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "A preparation generation exceeds its retained counter.")
    for lease in state.authority.preparation_leases:
        high_water = preparation_counters.get(lease.item_id)
        if high_water is None or lease.generation != high_water:
            raise StorageError(error_code, "The current preparation lease does not match its retained counter.")


def read_authority(connection: sqlite3.Connection) -> stored_state.AuthorityRecords:
    coordination_rows = tuple(
        connection.execute(
            """
            SELECT lease_id, task_id, host_id, generation, acquired_at, expires_at, status AS state
            FROM coordination_lease
            ORDER BY singleton
            """
        ).fetchall()
    )
    if len(coordination_rows) > 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple coordination leases.")
    coordination = decode_row(coordination_rows[0], stored_state.StoredCoordinationLease) if coordination_rows else None
    counters = tuple(
        decode_row(row, stored_state.AttemptLeaseCounter)
        for row in connection.execute(
            "SELECT attempt_id, generation_high_water FROM attempt_lease_counters ORDER BY attempt_id"
        ).fetchall()
    )
    generations = tuple(
        decode_row(row, stored_state.AttemptLeaseGeneration)
        for row in connection.execute(
            """
            SELECT attempt_id, generation, lease_id, task_id, host_id
            FROM attempt_lease_generations
            ORDER BY attempt_id, generation
            """
        ).fetchall()
    )
    leases = tuple(
        decode_row(row, stored_state.StoredAttemptLease)
        for row in connection.execute(
            """
            SELECT attempt_id, generation, acquired_at, expires_at, status AS state
            FROM attempt_leases
            ORDER BY attempt_id
            """
        ).fetchall()
    )
    preparation_counters = tuple(
        decode_row(row, stored_state.PreparationLeaseCounter)
        for row in connection.execute(
            "SELECT item_id, generation_high_water FROM preparation_lease_counters ORDER BY item_id"
        ).fetchall()
    )
    preparation_generations = tuple(
        decode_row(row, stored_state.PreparationLeaseGeneration)
        for row in connection.execute(
            """
            SELECT item_id, generation, lease_id, task_id, host_id
            FROM preparation_lease_generations
            ORDER BY item_id, generation
            """
        ).fetchall()
    )
    preparation_leases = tuple(
        decode_row(row, stored_state.StoredPreparationLease)
        for row in connection.execute(
            """
            SELECT item_id, generation, definition_revision, definition_digest,
                   acquired_at, expires_at, status AS state
            FROM preparation_leases
            ORDER BY item_id
            """
        ).fetchall()
    )
    return stored_state.AuthorityRecords(
        coordination,
        counters,
        generations,
        leases,
        preparation_counters,
        preparation_generations,
        preparation_leases,
    )


def fence_attempt_authority(
    connection: sqlite3.Connection,
    change: decision_models.AttemptAuthorityChange,
    decided_at: datetime,
) -> DecisionFailure | None:
    before = change.before
    after = change.after
    if after.lease_id is not None or after.generation != before.generation + 1:
        raise StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            "Attempt-authority fencing must allocate one revoked generation.",
        )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM attempt_lease_generations
        WHERE attempt_id = ? AND generation = ?
        """,
        (before.attempt, before.generation),
    ).fetchone()
    if anchor is None:
        return stale_write("The retained attempt generation is missing.")
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE attempt_lease_counters
                SET generation_high_water = ?
                WHERE attempt_id = ? AND generation_high_water = ?
                """,
                (after.generation, before.attempt, before.generation),
            ),
            "The attempt-authority counter is stale.",
        )
    ) is not None:
        return failure
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE attempt_leases
                SET generation = ?, expires_at = ?, status = 'revoked'
                WHERE attempt_id = ? AND generation = ?
                """,
                (after.generation, decided_at.isoformat(), before.attempt, before.generation),
            ),
            "The current attempt lease is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (before.attempt, after.generation, anchor["lease_id"], anchor["task_id"], anchor["host_id"]),
    )
    return None


def write_coordination_authority(
    connection: sqlite3.Connection,
    before: work_models.CoordinationLeaseAuthority | None,
    after: work_models.CoordinationLeaseAuthority,
    stale_message: str,
) -> DecisionFailure | None:
    if before is None:
        return require_one_changed_row(
            connection.execute(
                """
                INSERT INTO coordination_lease (
                    singleton, lease_id, task_id, host_id, generation, acquired_at, expires_at, status
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO NOTHING
                """,
                (
                    after.lease_id,
                    after.task_id,
                    after.host_id,
                    after.generation,
                    after.acquired_at.isoformat(),
                    after.expires_at.isoformat(),
                    after.state.value,
                ),
            ),
            stale_message,
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE coordination_lease
            SET lease_id = ?, task_id = ?, host_id = ?, generation = ?, acquired_at = ?, expires_at = ?, status = ?
            WHERE singleton = 1 AND lease_id = ? AND task_id = ? AND host_id = ? AND generation = ?
                AND acquired_at = ? AND expires_at = ? AND status = ?
            """,
            (
                after.lease_id,
                after.task_id,
                after.host_id,
                after.generation,
                after.acquired_at.isoformat(),
                after.expires_at.isoformat(),
                after.state.value,
                before.lease_id,
                before.task_id,
                before.host_id,
                before.generation,
                before.acquired_at.isoformat(),
                before.expires_at.isoformat(),
                before.state.value,
            ),
        ),
        stale_message,
    )


def write_attempt_authority(
    connection: sqlite3.Connection, decision: authority_models.AttemptAuthorityDecision
) -> DecisionFailure | None:
    after = decision.current_after
    retained_counter = connection.execute(
        "SELECT generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
        (decision.attempt,),
    ).fetchone()
    if retained_counter is None:
        if decision.counter_before != 0:
            return stale_write("The attempt counter is missing.")
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    INSERT INTO attempt_lease_counters (attempt_id, generation_high_water)
                    VALUES (?, ?)
                    ON CONFLICT(attempt_id) DO NOTHING
                    """,
                    (decision.attempt, decision.counter_after),
                ),
                "The attempt counter already exists.",
            )
        ) is not None:
            return failure
    else:
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                UPDATE attempt_lease_counters
                SET generation_high_water = ?
                WHERE attempt_id = ? AND generation_high_water = ?
                """,
                    (decision.counter_after, decision.attempt, decision.counter_before),
                ),
                "The attempt-authority counter is stale.",
            )
        ) is not None:
            return failure
    connection.execute(
        """
        INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id, generation) DO NOTHING
        """,
        (after.attempt, after.generation, after.lease_id, after.task_id, after.host_id),
    )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM attempt_lease_generations
        WHERE attempt_id = ? AND generation = ?
        """,
        (after.attempt, after.generation),
    ).fetchone()
    if anchor is None or tuple(anchor) != (after.lease_id, after.task_id, after.host_id):
        return stale_write("The retained attempt generation conflicts.")
    current_before = decision.current_before
    if current_before is None:
        return require_one_changed_row(
            connection.execute(
                """
                INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO NOTHING
                """,
                (
                    after.attempt,
                    after.generation,
                    after.acquired_at.isoformat(),
                    after.expires_at.isoformat(),
                    after.state.value,
                ),
            ),
            "The current attempt lease already exists.",
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE attempt_leases
            SET generation = ?, acquired_at = ?, expires_at = ?, status = ?
            WHERE attempt_id = ? AND generation = ? AND acquired_at = ? AND expires_at = ? AND status = ?
            """,
            (
                after.generation,
                after.acquired_at.isoformat(),
                after.expires_at.isoformat(),
                after.state.value,
                current_before.attempt,
                current_before.generation,
                current_before.acquired_at.isoformat(),
                current_before.expires_at.isoformat(),
                current_before.state.value,
            ),
        ),
        "The current attempt lease changed before persistence.",
    )


def write_preparation_authority(
    connection: sqlite3.Connection, decision: authority_models.PreparationAuthorityDecision
) -> DecisionFailure | None:
    after = decision.current_after
    retained_counter = connection.execute(
        "SELECT generation_high_water FROM preparation_lease_counters WHERE item_id = ?",
        (decision.item,),
    ).fetchone()
    if retained_counter is None:
        if decision.counter_before != 0:
            return stale_write("The preparation counter is missing.")
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    INSERT INTO preparation_lease_counters (item_id, generation_high_water)
                    VALUES (?, ?)
                    ON CONFLICT(item_id) DO NOTHING
                    """,
                    (decision.item, decision.counter_after),
                ),
                "The preparation counter already exists.",
            )
        ) is not None:
            return failure
    elif (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE preparation_lease_counters
                SET generation_high_water = ?
                WHERE item_id = ? AND generation_high_water = ?
                """,
                (decision.counter_after, decision.item, decision.counter_before),
            ),
            "The preparation-authority counter is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, generation) DO NOTHING
        """,
        (after.item, after.generation, after.lease_id, after.task_id, after.host_id),
    )
    anchor = connection.execute(
        """
        SELECT lease_id, task_id, host_id
        FROM preparation_lease_generations
        WHERE item_id = ? AND generation = ?
        """,
        (after.item, after.generation),
    ).fetchone()
    if anchor is None or tuple(anchor) != (after.lease_id, after.task_id, after.host_id):
        return stale_write("The retained preparation generation conflicts.")
    before = decision.current_before
    if before is None:
        return require_one_changed_row(
            connection.execute(
                """
                INSERT INTO preparation_leases (
                    item_id, generation, definition_revision, definition_digest, acquired_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    after.item,
                    after.generation,
                    after.definition_revision,
                    after.definition_digest,
                    after.acquired_at.isoformat(),
                    after.expires_at.isoformat(),
                    after.state.value,
                ),
            ),
            "The current preparation lease already exists.",
        )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE preparation_leases
            SET generation = ?, definition_revision = ?, definition_digest = ?,
                acquired_at = ?, expires_at = ?, status = ?
            WHERE item_id = ? AND generation = ? AND definition_revision = ? AND definition_digest = ?
                AND acquired_at = ? AND expires_at = ? AND status = ?
            """,
            (
                after.generation,
                after.definition_revision,
                after.definition_digest,
                after.acquired_at.isoformat(),
                after.expires_at.isoformat(),
                after.state.value,
                before.item,
                before.generation,
                before.definition_revision,
                before.definition_digest,
                before.acquired_at.isoformat(),
                before.expires_at.isoformat(),
                before.state.value,
            ),
        ),
        "The current preparation lease changed before persistence.",
    )


def consume_preparation_authority(
    connection: sqlite3.Connection,
    authority: work_models.PreparationCommandAuthority,
    consumed_at: datetime,
) -> DecisionFailure | None:
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE preparation_lease_counters
                SET generation_high_water = ?
                WHERE item_id = ? AND generation_high_water = ?
                """,
                (authority.generation + 1, authority.item, authority.generation),
            ),
            "The preparation-authority counter is stale.",
        )
    ) is not None:
        return failure
    connection.execute(
        """
        INSERT INTO preparation_lease_generations (item_id, generation, lease_id, task_id, host_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            authority.item,
            authority.generation + 1,
            authority.lease_id,
            authority.task_id,
            authority.host_id,
        ),
    )
    return require_one_changed_row(
        connection.execute(
            """
            UPDATE preparation_leases
            SET generation = ?, expires_at = ?, status = 'revoked'
            WHERE item_id = ? AND generation = ? AND definition_revision = ? AND definition_digest = ?
                AND expires_at = ? AND status = 'active'
            """,
            (
                authority.generation + 1,
                consumed_at.isoformat(),
                authority.item,
                authority.generation,
                authority.definition_revision,
                authority.definition_digest,
                authority.expires_at.isoformat(),
            ),
        ),
        "The preparation authority changed before activation.",
    )
