import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import assert_never

import msgspec

from charlie_pinboard.adapters.files.artifacts import verify_reference
from charlie_pinboard.adapters.sqlite.database import (
    APPLICATION,
    SCHEMA_VERSION,
    open_database,
    read_operation,
    write_transaction,
)
from charlie_pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from charlie_pinboard.adapters.sqlite.models import OpenMode
from charlie_pinboard.application.artifacts import ArtifactRef
from charlie_pinboard.application.errors import MutationContractError
from charlie_pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from charlie_pinboard.application.mutations import expected_stored_state
from charlie_pinboard.application.stored_state import (
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AuthorityRecords,
    ItemArtifactLink,
    ItemDependency,
    ItemScopeRevision,
    LifecycleRecords,
    ProjectRecord,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.decision_models import TransitionReceipt
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    TaskId,
)
from charlie_pinboard.domain.work_models import ArtifactRole, CanonicalJson


def _decode_row[Record](row: sqlite3.Row, record_type: type[Record]) -> Record:
    try:
        return msgspec.convert(dict(row), type=record_type, strict=True)
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error


def _stored_json(column: str, value: str) -> CanonicalJson:
    encoded = value.encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {column!r} has invalid JSON.") from error
    return CanonicalJson(encoded)


class _StoredTransitionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_json: str
    outcome_schema: str
    outcome_json: str
    committed_at: datetime

    def receipt(self) -> StoredTransitionReceipt:
        return StoredTransitionReceipt(
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


def _validate_attempt_authority(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    attempt_counters = {value.attempt_id: value.generation_high_water for value in state.authority.attempt_counters}
    for anchor in state.authority.attempt_generations:
        high_water = attempt_counters.get(anchor.attempt_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "An attempt generation exceeds its retained counter.")
    for lease in state.authority.attempt_leases:
        high_water = attempt_counters.get(lease.attempt_id)
        if high_water is None or lease.generation != high_water:
            raise StorageError(error_code, "The current attempt lease does not match its retained counter.")


def _validate_current_state(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    _validate_attempt_authority(state, error_code)


class _StoredStateReader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _rows(self, query: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self._connection.execute(query).fetchall())

    def read(self) -> StoredWorkState:
        state = StoredWorkState(
            self._lifecycle(),
            self._proposals(),
            self._artifacts(),
            self._authority(),
            self._history(),
            self._focus(),
        )
        _validate_current_state(state, StorageErrorCode.INVALID_STATE)
        return state

    def _project(self) -> ProjectRecord:
        rows = self._rows(
            """
            SELECT application, schema_version, revision, host_epoch, created_at, updated_at
            FROM project_meta
            ORDER BY singleton
            """
        )
        if len(rows) != 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
        return _decode_row(rows[0], ProjectRecord)

    def _lifecycle(self) -> LifecycleRecords:
        items = tuple(
            _decode_row(row, StoredWorkItem)
            for row in self._rows(
                """
                SELECT item_id, user_label, state, timing, source, trigger, why_it_matters, effect, unlock,
                       outcome_evidence, next_action, notes, scope_revision, scope_digest, subject_revision,
                       recorded_at, updated_at
                FROM work_items
                ORDER BY item_id
                """
            )
        )
        scopes = tuple(
            _decode_row(row, ItemScopeRevision)
            for row in self._rows(
                """
                SELECT item_id, scope_revision AS revision, scope_digest AS digest,
                       accepted_project_revision, accepted_at
                FROM item_scope_revisions
                ORDER BY item_id, scope_revision
                """
            )
        )
        dependencies = tuple(
            _decode_row(row, ItemDependency)
            for row in self._rows(
                "SELECT item_id, dependency_id, position FROM item_dependencies ORDER BY item_id, position"
            )
        )
        item_artifacts = tuple(
            _decode_row(row, ItemArtifactLink)
            for row in self._rows(
                "SELECT item_id, artifact_ref_id, role, position FROM item_artifacts ORDER BY item_id, role, position"
            )
        )
        attempts = tuple(
            _decode_row(row, StoredAttempt)
            for row in self._rows(
                """
                SELECT attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                       result_artifact_ref_id, blocker_artifact_ref_id, candidate_revision, candidate_recorded_at,
                       accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
                FROM attempts
                ORDER BY attempt_id
                """
            )
        )
        return LifecycleRecords(self._project(), items, scopes, dependencies, item_artifacts, attempts)

    def _proposals(self) -> ProposalRecords:
        proposals = tuple(
            _decode_row(row, StoredProposal)
            for row in self._rows(
                """
                SELECT proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                       relation_kind AS relation, relation_item_id, effect, unlock, urgency_evidence, disposition,
                       disposition_target_item_id, disposition_reason, subject_revision, disposition_recorded_at
                FROM proposals
                ORDER BY proposal_id
                """
            )
        )
        evidence = tuple(
            _decode_row(row, ProposalEvidence)
            for row in self._rows(
                "SELECT proposal_id, position, selector FROM proposal_evidence ORDER BY proposal_id, position"
            )
        )
        freshness = tuple(
            _decode_row(row, ProposalFreshness)
            for row in self._rows(
                "SELECT proposal_id, position, assumption FROM proposal_freshness ORDER BY proposal_id, position"
            )
        )
        return ProposalRecords(proposals, evidence, freshness)

    def _artifacts(self) -> tuple[ArtifactReference, ...]:
        return tuple(
            _decode_row(row, ArtifactReference)
            for row in self._rows(
                """
                SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                       relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
                FROM artifact_refs
                ORDER BY artifact_ref_id
                """
            )
        )

    def _authority(self) -> AuthorityRecords:
        coordination_rows = self._rows(
            """
            SELECT lease_id, task_id, host_id, generation, acquired_at, expires_at, status AS state
            FROM coordination_lease
            ORDER BY singleton
            """
        )
        if len(coordination_rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple coordination leases.")
        coordination = _decode_row(coordination_rows[0], StoredCoordinationLease) if coordination_rows else None
        counters = tuple(
            _decode_row(row, AttemptLeaseCounter)
            for row in self._rows(
                "SELECT attempt_id, generation_high_water FROM attempt_lease_counters ORDER BY attempt_id"
            )
        )
        generations = tuple(
            _decode_row(row, AttemptLeaseGeneration)
            for row in self._rows(
                """
                SELECT attempt_id, generation, lease_id, task_id, host_id
                FROM attempt_lease_generations
                ORDER BY attempt_id, generation
                """
            )
        )
        leases = tuple(
            _decode_row(row, StoredAttemptLease)
            for row in self._rows(
                """
                SELECT attempt_id, generation, acquired_at, expires_at, status AS state
                FROM attempt_leases
                ORDER BY attempt_id
                """
            )
        )
        return AuthorityRecords(coordination, counters, generations, leases)

    def _history(self) -> tuple[StoredTransitionReceipt, ...]:
        return tuple(
            _decode_row(row, _StoredTransitionRow).receipt()
            for row in self._rows(
                """
                SELECT history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                       authorization_kind AS authorization, actor_task_id, actor_host_id, input_schema,
                       input_json, outcome_schema, outcome_json, committed_at
                FROM transition_history
                ORDER BY history_id
                """
            )
        )

    def _focus(self) -> StoredFocus:
        rows = self._rows(
            "SELECT item_id, attempt_id, next_action, subject_revision FROM current_focus ORDER BY singleton"
        )
        if len(rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple focus records.")
        if not rows:
            return StoredFocus(None, None, "select", 0)
        return _decode_row(rows[0], StoredFocus)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json_text(value: CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


class _StoredStateWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_initial(self, state: StoredWorkState) -> None:
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
            for row in self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
        )
        try:
            occupied = sum(msgspec.convert(tuple(occupied_rows), type=tuple[int, ...], strict=True))
        except msgspec.ValidationError as error:
            raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error
        current_revision = self._connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
        if occupied != 0 or current_revision is None or current_revision[0] != 0:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Initial state requires a new empty database.")
        self._connection.execute("PRAGMA defer_foreign_keys = ON")
        self._artifacts(state.artifact_references)
        self._lifecycle(state.lifecycle)
        self._proposals(state.proposals)
        self._authority(state.authority)
        self._focus(state.focus)
        self._history(state.transition_receipts)
        self._connection.execute(
            """
            UPDATE project_meta
            SET revision = ?, host_epoch = ?, created_at = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (project.revision, project.host_epoch, project.created_at.isoformat(), project.updated_at.isoformat()),
        )

    def replace_current(self, state: StoredWorkState) -> None:
        self._connection.execute("PRAGMA defer_foreign_keys = ON")
        for table in (
            "transition_history",
            "current_focus",
            "attempt_leases",
            "attempt_lease_generations",
            "attempt_lease_counters",
            "coordination_lease",
            "proposal_evidence",
            "proposal_freshness",
            "proposals",
            "attempts",
            "item_artifacts",
            "item_dependencies",
            "item_scope_revisions",
            "work_items",
            "artifact_refs",
        ):
            self._connection.execute(f"DELETE FROM {table}")
        self._connection.execute("UPDATE project_meta SET revision = 0")
        self.insert_initial(state)

    def _artifacts(self, records: tuple[ArtifactReference, ...]) -> None:
        self._connection.executemany(
            """
            INSERT INTO artifact_refs (
                artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
                size_bytes, accepted_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.artifact_ref_id,
                    value.key,
                    value.revision,
                    value.kind.value,
                    value.selector,
                    value.content_sha256,
                    value.size_bytes,
                    value.accepted_revision,
                    value.created_at.isoformat(),
                )
                for value in records
            ),
        )

    def _lifecycle(self, records: LifecycleRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO work_items (
                item_id, user_label, state, timing, source, trigger, why_it_matters,
                effect, unlock, outcome_evidence, next_action, notes, scope_revision, scope_digest,
                subject_revision, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.item_id,
                    value.user_label,
                    value.state.value,
                    None if value.timing is None else value.timing.value,
                    value.source,
                    value.trigger,
                    value.why_it_matters,
                    value.effect,
                    value.unlock,
                    value.outcome_evidence,
                    value.next_action,
                    value.notes,
                    value.scope_revision,
                    value.scope_digest,
                    value.subject_revision,
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.work_items
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO item_scope_revisions (
                item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.item_id,
                    value.revision,
                    value.digest,
                    value.accepted_project_revision,
                    value.accepted_at.isoformat(),
                )
                for value in records.scope_revisions
            ),
        )
        self._connection.executemany(
            "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
            tuple((value.item_id, value.dependency_id, value.position) for value in records.dependencies),
        )
        self._connection.executemany(
            "INSERT INTO item_artifacts (item_id, artifact_ref_id, role, position) VALUES (?, ?, ?, ?)",
            tuple(
                (value.item_id, value.artifact_ref_id, value.role.value, value.position)
                for value in records.item_artifacts
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO attempts (
                attempt_id, item_id, state, branch, base_revision, provenance,
                brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                blocker_artifact_ref_id, blocker_artifact_kind, candidate_revision, candidate_recorded_at,
                accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'brief', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.attempt_id,
                    value.item_id,
                    value.state.value,
                    value.branch,
                    value.base_revision,
                    value.provenance,
                    value.brief_artifact_ref_id,
                    value.result_artifact_ref_id,
                    None if value.result_artifact_ref_id is None else "result",
                    value.blocker_artifact_ref_id,
                    None if value.blocker_artifact_ref_id is None else "blocker",
                    value.candidate_revision,
                    _timestamp(value.candidate_recorded_at),
                    value.accepted_scope_revision,
                    value.accepted_scope_digest,
                    value.subject_revision,
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.attempts
            ),
        )

    def _proposals(self, records: ProposalRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO proposals (
                proposal_id, created_at, recorded_at, source_task_id, user_label,
                trigger, why_it_matters, relation_kind, relation_item_id, effect, unlock,
                urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
                subject_revision, disposition_recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.proposal_id,
                    value.created_at.isoformat(),
                    value.recorded_at.isoformat(),
                    value.source_task_id,
                    value.user_label,
                    value.trigger,
                    value.why_it_matters,
                    value.relation.value,
                    value.relation_item_id,
                    value.effect,
                    value.unlock,
                    value.urgency_evidence,
                    None if value.disposition is None else value.disposition.value,
                    value.disposition_target_item_id,
                    value.disposition_reason,
                    value.subject_revision,
                    _timestamp(value.disposition_recorded_at),
                )
                for value in records.proposals
            ),
        )
        self._connection.executemany(
            "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
            tuple((value.proposal_id, value.position, value.selector) for value in records.evidence),
        )
        self._connection.executemany(
            "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
            tuple((value.proposal_id, value.position, value.assumption) for value in records.freshness),
        )

    def _authority(self, records: AuthorityRecords) -> None:
        if records.coordination is not None:
            value = records.coordination
            self._connection.execute(
                """
                INSERT INTO coordination_lease (
                    singleton, lease_id, task_id, host_id, generation, acquired_at, expires_at, status
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.lease_id,
                    value.task_id,
                    value.host_id,
                    value.generation,
                    value.acquired_at.isoformat(),
                    value.expires_at.isoformat(),
                    value.state.value,
                ),
            )
        self._connection.executemany(
            "INSERT INTO attempt_lease_counters (attempt_id, generation_high_water) VALUES (?, ?)",
            tuple((value.attempt_id, value.generation_high_water) for value in records.attempt_counters),
        )
        self._connection.executemany(
            """
            INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (value.attempt_id, value.generation, value.lease_id, value.task_id, value.host_id)
                for value in records.attempt_generations
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.attempt_id,
                    value.generation,
                    value.acquired_at.isoformat(),
                    value.expires_at.isoformat(),
                    value.state.value,
                )
                for value in records.attempt_leases
            ),
        )

    def _focus(self, focus: StoredFocus) -> None:
        self._connection.execute(
            """
            INSERT INTO current_focus (singleton, item_id, attempt_id, next_action, subject_revision)
            VALUES (1, ?, ?, ?, ?)
            """,
            (focus.item_id, focus.attempt_id, focus.next_action, focus.subject_revision),
        )

    def _history(self, records: tuple[StoredTransitionReceipt, ...]) -> None:
        self._connection.executemany(
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


class _SQLiteWorkTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def snapshot(self) -> StoredWorkState:
        return _StoredStateReader(self._connection).read()

    def commit(self, mutation: StoredStateMutation) -> TransitionReceipt:
        current = _StoredStateReader(self._connection).read()
        if current != mutation.before:
            raise StorageError(
                StorageErrorCode.STALE_WRITE,
                "The stored work state changed; rediscover the mutation before retrying.",
            )
        try:
            expected = expected_stored_state(mutation)
        except MutationContractError as error:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, str(error)) from error
        if mutation.after != expected:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "The supplied stored state does not match the accepted mutation's exact relational delta.",
            )
        _StoredStateWriter(self._connection).replace_current(expected)
        if _StoredStateReader(self._connection).read() != expected:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The stored mutation did not round-trip exactly.")
        match mutation:
            case (
                TransitionMutation()
                | ProposalCreationMutation()
                | CoordinationAuthorityMutation()
                | AttemptAuthorityMutation()
            ):
                return mutation.receipt.transition
            case _ as unreachable:
                assert_never(unreachable)


class SQLiteWorkStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def snapshot(self) -> StoredWorkState:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return _StoredStateReader(connection).read()
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Generator[_SQLiteWorkTransaction]:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                yield _SQLiteWorkTransaction(connection)
        finally:
            connection.close()

    def initialize_state(self, state: StoredWorkState) -> None:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                _StoredStateWriter(connection).insert_initial(state)
        finally:
            connection.close()

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
        *,
        item_id: ItemId | None = None,
        role: ArtifactRole | None = None,
    ) -> ArtifactReference:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                before = _StoredStateReader(connection).read()
                verify_reference(work_root, published)
                if (item_id is None) != (role is None):
                    raise StorageError(
                        StorageErrorCode.INVARIANT_VIOLATION,
                        "An artifact relationship requires both an item and role.",
                    )
                existing = next(
                    (
                        value
                        for value in before.artifact_references
                        if (value.kind, value.key, value.revision)
                        == (published.kind, published.key, published.revision)
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.selector,
                        existing.content_sha256,
                        existing.size_bytes,
                    ) != (published.selector, published.content_sha256, published.size_bytes):
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "An accepted artifact identity already names different bytes.",
                        )
                    reference = existing
                else:
                    reference = ArtifactReference(
                        ArtifactRefId(
                            1 + max((int(value.artifact_ref_id) for value in before.artifact_references), default=0)
                        ),
                        published.key,
                        published.revision,
                        published.kind,
                        published.selector,
                        published.content_sha256,
                        published.size_bytes,
                        before.lifecycle.project.revision + 1,
                        accepted_at,
                    )
                relationship_exists = item_id is not None and any(
                    value.item_id == item_id
                    and value.artifact_ref_id == reference.artifact_ref_id
                    and value.role == role
                    for value in before.lifecycle.item_artifacts
                )
                if existing is not None and (item_id is None or relationship_exists):
                    return existing
                revision = before.lifecycle.project.revision + 1
                lifecycle = before.lifecycle
                if item_id is not None and role is not None:
                    item_index = next(
                        (index for index, value in enumerate(lifecycle.work_items) if value.item_id == item_id),
                        None,
                    )
                    if item_index is None or published.kind.value != role.value:
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "Artifact relationship does not match a current item and compatible role.",
                        )
                    items = list(lifecycle.work_items)
                    items[item_index] = replace(
                        items[item_index],
                        subject_revision=revision,
                        updated_at=accepted_at,
                    )
                    position = sum(
                        1 for value in lifecycle.item_artifacts if value.item_id == item_id and value.role == role
                    )
                    item_artifact = ItemArtifactLink(item_id, reference.artifact_ref_id, role, position)
                    item_artifact_order = (str(item_artifact.item_id), item_artifact.role.value, item_artifact.position)
                    insertion_index = len(lifecycle.item_artifacts)
                    for index, existing_link in enumerate(lifecycle.item_artifacts):
                        existing_order = (str(existing_link.item_id), existing_link.role.value, existing_link.position)
                        if item_artifact_order < existing_order:
                            insertion_index = index
                            break
                    item_artifacts = (
                        *lifecycle.item_artifacts[:insertion_index],
                        item_artifact,
                        *lifecycle.item_artifacts[insertion_index:],
                    )
                    lifecycle = replace(
                        lifecycle,
                        work_items=tuple(items),
                        item_artifacts=item_artifacts,
                    )
                after = replace(
                    before,
                    lifecycle=replace(
                        lifecycle,
                        project=replace(
                            lifecycle.project,
                            revision=revision,
                            updated_at=accepted_at,
                        ),
                    ),
                    artifact_references=(
                        before.artifact_references if existing is not None else (*before.artifact_references, reference)
                    ),
                )
                _StoredStateWriter(connection).replace_current(after)
                if _StoredStateReader(connection).read() != after:
                    raise StorageError(
                        StorageErrorCode.INVARIANT_VIOLATION,
                        "The accepted artifact did not round-trip exactly.",
                    )
                return reference
        finally:
            connection.close()
