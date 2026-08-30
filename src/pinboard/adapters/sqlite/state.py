"""Compose, validate, and initialize complete state on a supplied connection.

This module reads and writes SQLite but never commits, rolls back, closes the
connection, calls callbacks, reads the filesystem, or obtains time. SQLite and
persisted-invariant failures remain exceptional; the transaction owner stays in
``store``.
"""

import sqlite3
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.artifacts import insert_artifacts, read_artifacts
from pinboard.adapters.sqlite.authority import insert_authority, read_authority, validate_attempt_authority
from pinboard.adapters.sqlite.database import APPLICATION, SCHEMA_VERSION, decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import insert_focus, insert_lifecycle, read_focus, read_lifecycle
from pinboard.adapters.sqlite.proposals import insert_proposals, read_proposals
from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    TaskId,
)


def _stored_json(column: str, value: str) -> work_models.CanonicalJson:
    encoded = value.encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {column!r} has invalid JSON.") from error
    return work_models.CanonicalJson(encoded)


class _StoredTransitionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: stored_state.TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: stored_state.TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_json: str
    outcome_schema: str
    outcome_json: str
    committed_at: datetime

    def receipt(self) -> stored_state.StoredTransitionReceipt:
        return stored_state.StoredTransitionReceipt(
            self.history_id,
            self.project_revision,
            self.action_id,
            self.action_kind,
            self.subject_id,
            self.artifact_ref_id,
            self.authorization,
            self.actor_task_id,
            self.actor_host_id,
            self.input_schema,
            _stored_json("input_json", self.input_json),
            self.outcome_schema,
            _stored_json("outcome_json", self.outcome_json),
            self.committed_at,
        )


def _read_project(connection: sqlite3.Connection) -> stored_state.ProjectRecord:
    rows = tuple(
        connection.execute(
            """
            SELECT application, schema_version, revision, host_epoch, created_at, updated_at
            FROM project_meta
            ORDER BY singleton
            """
        ).fetchall()
    )
    if len(rows) != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
    return decode_row(rows[0], stored_state.ProjectRecord)


def _read_history(connection: sqlite3.Connection) -> tuple[stored_state.StoredTransitionReceipt, ...]:
    return tuple(
        decode_row(row, _StoredTransitionRow).receipt()
        for row in connection.execute(
            """
            SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                   authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                   input_json, outcome_schema, outcome_json, committed_at
            FROM transition_history
            ORDER BY history_id
            """
        ).fetchall()
    )


def _validate_current_state(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    validate_attempt_authority(state, error_code)
    positions = sorted(value.queue_position for value in state.lifecycle.work_items if value.queue_position is not None)
    if positions != list(range(1, len(positions) + 1)):
        raise StorageError(error_code, "Live work-item queue positions must be contiguous and one-based.")


def read_state(connection: sqlite3.Connection) -> stored_state.StoredWorkState:
    project = _read_project(connection)
    state = stored_state.StoredWorkState(
        read_lifecycle(connection, project),
        read_proposals(connection),
        read_artifacts(connection),
        read_authority(connection),
        _read_history(connection),
        read_focus(connection),
    )
    _validate_current_state(state, StorageErrorCode.INVALID_STATE)
    return state


def _json_text(value: work_models.CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


def append_history(
    connection: sqlite3.Connection,
    records: tuple[stored_state.StoredTransitionReceipt, ...],
) -> None:
    connection.executemany(
        """
        INSERT INTO transition_history (
            history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
            artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
            input_json, outcome_schema, outcome_json, committed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            (
                value.history_id,
                value.project_revision,
                value.action_id,
                value.action_kind.value,
                value.subject_id,
                value.artifact_ref_id,
                None if value.artifact_ref_id is None else "evidence",
                value.authorization.value,
                value.actor_task_id,
                value.actor_host_id,
                value.input_schema,
                _json_text(value.input_payload),
                value.outcome_schema,
                _json_text(value.outcome_payload),
                value.committed_at.isoformat(),
            )
            for value in records
        ),
    )


def insert_initial_state(connection: sqlite3.Connection, state: stored_state.StoredWorkState) -> None:
    project = state.lifecycle.project
    if project.application != APPLICATION or project.schema_version != SCHEMA_VERSION:
        raise StorageError(
            StorageErrorCode.INVALID_STATE, "Stored state does not match the current application schema."
        )
    _validate_current_state(state, StorageErrorCode.INVARIANT_VIOLATION)
    occupied_rows = (
        row["count"]
        for table in (
            "artifact_refs",
            "work_items",
            "item_scope_revisions",
            "item_dependencies",
            "item_artifacts",
            "attempts",
            "proposals",
            "proposal_evidence",
            "proposal_freshness",
            "coordination_lease",
            "attempt_lease_counters",
            "attempt_lease_generations",
            "attempt_leases",
            "current_focus",
            "transition_history",
        )
        for row in connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
    )
    try:
        occupied = sum(msgspec.convert(tuple(occupied_rows), type=tuple[int, ...], strict=True))
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error
    current_revision = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if occupied != 0 or current_revision is None or current_revision[0] != 0:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Initial state requires a new empty database.")
    connection.execute("PRAGMA defer_foreign_keys = ON")
    insert_artifacts(connection, state.artifact_references)
    insert_lifecycle(connection, state.lifecycle)
    insert_proposals(connection, state.proposals)
    insert_authority(connection, state.authority)
    insert_focus(connection, state.focus)
    append_history(connection, state.transition_receipts)
    connection.execute(
        """
        UPDATE project_meta
        SET revision = ?, host_epoch = ?, created_at = ?, updated_at = ?
        WHERE singleton = 1
        """,
        (project.revision, project.host_epoch, project.created_at.isoformat(), project.updated_at.isoformat()),
    )
