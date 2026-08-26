import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from enum import Enum
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
    ArtifactKind,
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
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_models import AttemptLeaseStatus
from charlie_pinboard.domain.decision_models import TransitionReceipt
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.work_models import (
    ArtifactRole,
    AttemptState,
    CanonicalJson,
    CoordinationLeaseStatus,
    ProposalDispositionKind,
    ProposalRelationKind,
    Timing,
)


def _text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be text.")
    return value


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None or isinstance(value, str):
        return value
    raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be text or null.")


def _integer(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be an integer.")
    return value


def _optional_integer(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be an integer or null.")
    return value


def _time(row: sqlite3.Row, key: str) -> datetime:
    try:
        return datetime.fromisoformat(_text(row, key))
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an invalid timestamp.") from error


def _optional_time(row: sqlite3.Row, key: str) -> datetime | None:
    value = _optional_text(row, key)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an invalid timestamp.") from error


def _stored_json(row: sqlite3.Row, key: str) -> CanonicalJson:
    """Return JSON bytes whose canonical whitespace is owned by the stored history schema."""

    encoded = _text(row, key).encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has invalid JSON.") from error
    return CanonicalJson(encoded)


def _enum_value[EnumValue: Enum](constructor: type[EnumValue], row: sqlite3.Row, key: str) -> EnumValue:
    try:
        return constructor(_text(row, key))
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an unsupported value.") from error


def _expect(row: sqlite3.Row, key: str, expected: str) -> None:
    if _text(row, key) != expected:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be {expected!r}.")


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
        rows = self._rows("SELECT * FROM project_meta ORDER BY singleton")
        if len(rows) != 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
        row = rows[0]
        _expect(row, "application", APPLICATION)
        if _integer(row, "schema_version") != SCHEMA_VERSION:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The opened database schema changed unexpectedly.")
        return ProjectRecord(
            APPLICATION,
            1,
            _integer(row, "revision"),
            _integer(row, "host_epoch"),
            _time(row, "created_at"),
            _time(row, "updated_at"),
        )

    def _lifecycle(self) -> LifecycleRecords:
        items = tuple(
            StoredWorkItem(
                ItemId(_text(row, "item_id")),
                _text(row, "user_label"),
                _enum_value(StoredWorkItemState, row, "state"),
                None if (timing := _optional_text(row, "timing")) is None else Timing(timing),
                _optional_text(row, "source"),
                _optional_text(row, "trigger"),
                _optional_text(row, "why_it_matters"),
                _optional_text(row, "effect"),
                _optional_text(row, "unlock"),
                _optional_text(row, "outcome_evidence"),
                _optional_text(row, "next_action"),
                _optional_text(row, "notes"),
                _integer(row, "scope_revision"),
                _text(row, "scope_digest"),
                _integer(row, "subject_revision"),
                _time(row, "recorded_at"),
                _time(row, "updated_at"),
            )
            for row in self._rows("SELECT * FROM work_items ORDER BY item_id")
        )
        scopes = tuple(
            ItemScopeRevision(
                ItemId(_text(row, "item_id")),
                _integer(row, "scope_revision"),
                _text(row, "scope_digest"),
                _integer(row, "accepted_project_revision"),
                _time(row, "accepted_at"),
            )
            for row in self._rows("SELECT * FROM item_scope_revisions ORDER BY item_id, scope_revision")
        )
        dependencies = tuple(
            ItemDependency(
                ItemId(_text(row, "item_id")),
                ItemId(_text(row, "dependency_id")),
                _integer(row, "position"),
            )
            for row in self._rows("SELECT * FROM item_dependencies ORDER BY item_id, position")
        )
        item_artifacts = tuple(
            ItemArtifactLink(
                ItemId(_text(row, "item_id")),
                ArtifactRefId(_integer(row, "artifact_ref_id")),
                _enum_value(ArtifactRole, row, "role"),
                _integer(row, "position"),
            )
            for row in self._rows("SELECT * FROM item_artifacts ORDER BY item_id, role, position")
        )
        attempts: list[StoredAttempt] = []
        for row in self._rows("SELECT * FROM attempts ORDER BY attempt_id"):
            _expect(row, "brief_artifact_kind", "brief")
            if _optional_integer(row, "result_artifact_ref_id") is not None:
                _expect(row, "result_artifact_kind", "result")
            if _optional_integer(row, "blocker_artifact_ref_id") is not None:
                _expect(row, "blocker_artifact_kind", "blocker")
            attempts.append(
                StoredAttempt(
                    AttemptId(_text(row, "attempt_id")),
                    ItemId(_text(row, "item_id")),
                    _enum_value(AttemptState, row, "state"),
                    _text(row, "branch"),
                    _text(row, "base_revision"),
                    _text(row, "provenance"),
                    ArtifactRefId(_integer(row, "brief_artifact_ref_id")),
                    None
                    if (result := _optional_integer(row, "result_artifact_ref_id")) is None
                    else ArtifactRefId(result),
                    None
                    if (blocker := _optional_integer(row, "blocker_artifact_ref_id")) is None
                    else ArtifactRefId(blocker),
                    _optional_text(row, "candidate_revision"),
                    _optional_time(row, "candidate_recorded_at"),
                    _integer(row, "accepted_scope_revision"),
                    _text(row, "accepted_scope_digest"),
                    _integer(row, "subject_revision"),
                    _time(row, "recorded_at"),
                    _time(row, "updated_at"),
                )
            )
        return LifecycleRecords(self._project(), items, scopes, dependencies, item_artifacts, tuple(attempts))

    def _proposals(self) -> ProposalRecords:
        proposals = tuple(
            StoredProposal(
                ProposalId(_text(row, "proposal_id")),
                _time(row, "created_at"),
                _time(row, "recorded_at"),
                TaskId(_text(row, "source_task_id")),
                _text(row, "user_label"),
                _text(row, "trigger"),
                _text(row, "why_it_matters"),
                _enum_value(ProposalRelationKind, row, "relation_kind"),
                None if (relation := _optional_text(row, "relation_item_id")) is None else ItemId(relation),
                _text(row, "effect"),
                _text(row, "unlock"),
                _text(row, "urgency_evidence"),
                None
                if (disposition := _optional_text(row, "disposition")) is None
                else ProposalDispositionKind(disposition),
                None if (target := _optional_text(row, "disposition_target_item_id")) is None else ItemId(target),
                _optional_text(row, "disposition_reason"),
                _integer(row, "subject_revision"),
                _optional_time(row, "disposition_recorded_at"),
            )
            for row in self._rows("SELECT * FROM proposals ORDER BY proposal_id")
        )
        evidence = tuple(
            ProposalEvidence(ProposalId(_text(row, "proposal_id")), _integer(row, "position"), _text(row, "selector"))
            for row in self._rows("SELECT * FROM proposal_evidence ORDER BY proposal_id, position")
        )
        freshness = tuple(
            ProposalFreshness(
                ProposalId(_text(row, "proposal_id")), _integer(row, "position"), _text(row, "assumption")
            )
            for row in self._rows("SELECT * FROM proposal_freshness ORDER BY proposal_id, position")
        )
        return ProposalRecords(proposals, evidence, freshness)

    def _artifacts(self) -> tuple[ArtifactReference, ...]:
        return tuple(
            ArtifactReference(
                ArtifactRefId(_integer(row, "artifact_ref_id")),
                _text(row, "artifact_key"),
                _integer(row, "artifact_revision"),
                _enum_value(ArtifactKind, row, "kind"),
                _text(row, "relative_path"),
                _text(row, "content_sha256"),
                _integer(row, "size_bytes"),
                _integer(row, "accepted_revision"),
                _time(row, "created_at"),
            )
            for row in self._rows("SELECT * FROM artifact_refs ORDER BY artifact_ref_id")
        )

    def _authority(self) -> AuthorityRecords:
        coordination_rows = self._rows("SELECT * FROM coordination_lease ORDER BY singleton")
        if len(coordination_rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple coordination leases.")
        coordination = None
        if coordination_rows:
            row = coordination_rows[0]
            coordination = StoredCoordinationLease(
                LeaseId(_text(row, "lease_id")),
                TaskId(_text(row, "task_id")),
                HostId(_text(row, "host_id")),
                _integer(row, "generation"),
                _time(row, "acquired_at"),
                _time(row, "expires_at"),
                _enum_value(CoordinationLeaseStatus, row, "status"),
            )
        counters = tuple(
            AttemptLeaseCounter(AttemptId(_text(row, "attempt_id")), _integer(row, "generation_high_water"))
            for row in self._rows("SELECT * FROM attempt_lease_counters ORDER BY attempt_id")
        )
        generations = tuple(
            AttemptLeaseGeneration(
                AttemptId(_text(row, "attempt_id")),
                _integer(row, "generation"),
                LeaseId(_text(row, "lease_id")),
                TaskId(_text(row, "task_id")),
                HostId(_text(row, "host_id")),
            )
            for row in self._rows("SELECT * FROM attempt_lease_generations ORDER BY attempt_id, generation")
        )
        leases = tuple(
            StoredAttemptLease(
                AttemptId(_text(row, "attempt_id")),
                _integer(row, "generation"),
                _time(row, "acquired_at"),
                _time(row, "expires_at"),
                _enum_value(AttemptLeaseStatus, row, "status"),
            )
            for row in self._rows("SELECT * FROM attempt_leases ORDER BY attempt_id")
        )
        return AuthorityRecords(coordination, counters, generations, leases)

    def _history(self) -> tuple[StoredTransitionReceipt, ...]:
        receipts: list[StoredTransitionReceipt] = []
        for row in self._rows("SELECT * FROM transition_history ORDER BY history_id"):
            artifact_ref = _optional_integer(row, "artifact_ref_id")
            if artifact_ref is not None:
                _expect(row, "artifact_kind", "evidence")
            elif _optional_text(row, "artifact_kind") is not None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "History artifact kind requires a reference.")
            receipts.append(
                StoredTransitionReceipt(
                    HistoryId(_integer(row, "history_id")),
                    _integer(row, "project_revision"),
                    ActionId(_text(row, "action_id")),
                    _enum_value(TransitionHistoryActionKind, row, "action_kind"),
                    HistorySubjectId(_text(row, "subject_id")),
                    None if artifact_ref is None else ArtifactRefId(artifact_ref),
                    _enum_value(TransitionHistoryAuthorizationKind, row, "authorization_kind"),
                    None if (task := _optional_text(row, "actor_task_id")) is None else TaskId(task),
                    None if (host := _optional_text(row, "actor_host_id")) is None else HostId(host),
                    _text(row, "input_schema"),
                    _stored_json(row, "input_json"),
                    _text(row, "outcome_schema"),
                    _stored_json(row, "outcome_json"),
                    _time(row, "committed_at"),
                )
            )
        return tuple(receipts)

    def _focus(self) -> StoredFocus:
        rows = self._rows("SELECT * FROM current_focus ORDER BY singleton")
        if len(rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple focus records.")
        if not rows:
            return StoredFocus(None, None, "select", 0)
        row = rows[0]
        return StoredFocus(
            None if (item := _optional_text(row, "item_id")) is None else ItemId(item),
            None if (attempt := _optional_text(row, "attempt_id")) is None else AttemptId(attempt),
            _text(row, "next_action"),
            _integer(row, "subject_revision"),
        )


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
        occupied = sum(
            _integer(row, "count")
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
