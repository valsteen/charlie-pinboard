import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files.artifacts import verify_reference
from pinboard.adapters.sqlite.database import (
    APPLICATION,
    SCHEMA_VERSION,
    open_database,
    read_operation,
    write_transaction,
)
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.application.mutations import stored_transition_receipt
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    ProposalId,
    TaskId,
)


def _decode_row[Record](row: sqlite3.Row, record_type: type[Record]) -> Record:
    try:
        return msgspec.convert(dict(row), type=record_type, strict=True)
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error


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


class _StoredProposalRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: ProposalId
    created_at: datetime
    recorded_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation_kind: work_models.ProposalRelationKind
    relation_item_id: ItemId | None
    effect: str
    unlock: str
    urgency_evidence: str
    disposition: work_models.ProposalDispositionKind | None
    disposition_target_item_id: ItemId | None
    disposition_reason: str | None
    subject_revision: int
    disposition_recorded_at: datetime | None

    def proposal(self) -> stored_state.StoredProposal:
        relation = _stored_proposal_relation(self.relation_kind, self.relation_item_id)
        disposition = _stored_proposal_disposition(
            self.disposition,
            self.disposition_target_item_id,
            self.disposition_reason,
            self.disposition_recorded_at,
        )
        return stored_state.StoredProposal(
            self.proposal_id,
            self.created_at,
            self.recorded_at,
            self.source_task_id,
            self.user_label,
            self.trigger,
            self.why_it_matters,
            relation,
            self.effect,
            self.unlock,
            self.urgency_evidence,
            disposition,
            self.subject_revision,
        )


def _required_relation_item(kind: work_models.ProposalRelationKind, item: ItemId | None) -> ItemId:
    if item is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has no related item.")
    return item


def _forbidden_relation_item(kind: work_models.ProposalRelationKind, item: ItemId | None) -> None:
    if item is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has a related item.")


def _stored_proposal_relation(
    kind: work_models.ProposalRelationKind, item: ItemId | None
) -> work_models.ProposalRelation:
    match kind:
        case work_models.ProposalRelationKind.INDEPENDENT:
            _forbidden_relation_item(kind, item)
            return work_models.IndependentProposalRelation()
        case work_models.ProposalRelationKind.PREREQUISITE:
            return work_models.PrerequisiteProposalRelation(_required_relation_item(kind, item))
        case work_models.ProposalRelationKind.FOLLOW_UP:
            return work_models.FollowUpProposalRelation(_required_relation_item(kind, item))
        case work_models.ProposalRelationKind.DUPLICATE:
            return work_models.DuplicateProposalRelation(_required_relation_item(kind, item))
        case work_models.ProposalRelationKind.CONTRADICTION:
            return work_models.ContradictionProposalRelation(_required_relation_item(kind, item))
        case work_models.ProposalRelationKind.CLARIFICATION:
            _forbidden_relation_item(kind, item)
            return work_models.ClarificationProposalRelation()
        case _ as unreachable:
            assert_never(unreachable)


def _required_disposition_target(kind: work_models.ProposalDispositionKind, target: ItemId | None) -> ItemId:
    if target is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no target item.")
    return target


def _required_disposition_reason(kind: work_models.ProposalDispositionKind, reason: str | None) -> str:
    if reason is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no reason.")
    return reason


def _required_disposition_time(kind: work_models.ProposalDispositionKind, value: datetime | None) -> datetime:
    if value is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has no timestamp.")
    return value


def _forbid_disposition_value(kind: work_models.ProposalDispositionKind, name: str, value: ItemId | str | None) -> None:
    if value is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal disposition has a {name}.")


def _stored_proposal_disposition(
    kind: work_models.ProposalDispositionKind | None,
    target: ItemId | None,
    reason: str | None,
    disposed_at: datetime | None,
) -> work_models.ProposalDisposition | None:
    match kind:
        case None:
            if target is not None or reason is not None or disposed_at is not None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "Open proposal has disposition details.")
            return None
        case work_models.ProposalDispositionKind.ACCEPTED:
            _forbid_disposition_value(kind, "reason", reason)
            return work_models.AcceptedProposalDisposition(
                _required_disposition_target(kind, target),
                _required_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.MERGED:
            _forbid_disposition_value(kind, "reason", reason)
            return work_models.MergedProposalDisposition(
                _required_disposition_target(kind, target),
                _required_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.RETURNED:
            _forbid_disposition_value(kind, "target item", target)
            return work_models.ReturnedProposalDisposition(
                _required_disposition_reason(kind, reason),
                _required_disposition_time(kind, disposed_at),
            )
        case work_models.ProposalDispositionKind.REJECTED:
            _forbid_disposition_value(kind, "target item", target)
            return work_models.RejectedProposalDisposition(
                _required_disposition_reason(kind, reason),
                _required_disposition_time(kind, disposed_at),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _proposal_disposition_columns(
    value: work_models.ProposalDisposition | None,
) -> tuple[str | None, ItemId | None, str | None, str | None]:
    match value:
        case None:
            return None, None, None, None
        case (
            work_models.AcceptedProposalDisposition(kind=kind, target=target, disposed_at=disposed_at)
            | work_models.MergedProposalDisposition(kind=kind, target=target, disposed_at=disposed_at)
        ):
            return kind.value, target, None, disposed_at.isoformat()
        case (
            work_models.ReturnedProposalDisposition(kind=kind, reason=reason, disposed_at=disposed_at)
            | work_models.RejectedProposalDisposition(kind=kind, reason=reason, disposed_at=disposed_at)
        ):
            return kind.value, None, reason, disposed_at.isoformat()
        case _ as unreachable:
            assert_never(unreachable)


def _validate_attempt_authority(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    attempt_counters = {value.attempt_id: value.generation_high_water for value in state.authority.attempt_counters}
    for anchor in state.authority.attempt_generations:
        high_water = attempt_counters.get(anchor.attempt_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "An attempt generation exceeds its retained counter.")
    for lease in state.authority.attempt_leases:
        high_water = attempt_counters.get(lease.attempt_id)
        if high_water is None or lease.generation != high_water:
            raise StorageError(error_code, "The current attempt lease does not match its retained counter.")


def _validate_current_state(state: stored_state.StoredWorkState, error_code: StorageErrorCode) -> None:
    _validate_attempt_authority(state, error_code)
    positions = sorted(value.queue_position for value in state.lifecycle.work_items if value.queue_position is not None)
    if positions != list(range(1, len(positions) + 1)):
        raise StorageError(error_code, "Live work-item queue positions must be contiguous and one-based.")


class _StoredStateReader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _rows(self, query: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self._connection.execute(query).fetchall())

    def read(self) -> stored_state.StoredWorkState:
        state = stored_state.StoredWorkState(
            self._lifecycle(),
            self._proposals(),
            self._artifacts(),
            self._authority(),
            self._history(),
            self._focus(),
        )
        _validate_current_state(state, StorageErrorCode.INVALID_STATE)
        return state

    def _project(self) -> stored_state.ProjectRecord:
        rows = self._rows(
            """
            SELECT application, schema_version, revision, host_epoch, created_at, updated_at
            FROM project_meta
            ORDER BY singleton
            """
        )
        if len(rows) != 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
        return _decode_row(rows[0], stored_state.ProjectRecord)

    def _lifecycle(self) -> stored_state.LifecycleRecords:
        items = tuple(
            _decode_row(row, stored_state.StoredWorkItem)
            for row in self._rows(
                """
                SELECT item_id, user_label, state, timing, source, trigger, why_it_matters, effect, unlock,
                       outcome_evidence, next_action, notes, scope_revision, scope_digest, subject_revision,
                       recorded_at, updated_at, queue_position
                FROM work_items
                ORDER BY item_id
                """
            )
        )
        scopes = tuple(
            _decode_row(row, stored_state.ItemScopeRevision)
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
            _decode_row(row, stored_state.ItemDependency)
            for row in self._rows(
                "SELECT item_id, dependency_id, position FROM item_dependencies ORDER BY item_id, position"
            )
        )
        item_artifacts = tuple(
            _decode_row(row, stored_state.ItemArtifactLink)
            for row in self._rows(
                "SELECT item_id, artifact_ref_id, role, position FROM item_artifacts ORDER BY item_id, role, position"
            )
        )
        attempts = tuple(
            _decode_row(row, stored_state.StoredAttempt)
            for row in self._rows(
                """
                SELECT attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                       result_artifact_ref_id, candidate_revision, candidate_recorded_at,
                       accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
                FROM attempts
                ORDER BY attempt_id
                """
            )
        )
        return stored_state.LifecycleRecords(self._project(), items, scopes, dependencies, item_artifacts, attempts)

    def _proposals(self) -> stored_state.ProposalRecords:
        proposals = tuple(
            _decode_row(row, _StoredProposalRow).proposal()
            for row in self._rows(
                """
                SELECT proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                       relation_kind, relation_item_id, effect, unlock, urgency_evidence, disposition,
                       disposition_target_item_id, disposition_reason, subject_revision, disposition_recorded_at
                FROM proposals
                ORDER BY proposal_id
                """
            )
        )
        evidence = tuple(
            _decode_row(row, stored_state.ProposalEvidence)
            for row in self._rows(
                "SELECT proposal_id, position, selector FROM proposal_evidence ORDER BY proposal_id, position"
            )
        )
        freshness = tuple(
            _decode_row(row, stored_state.ProposalFreshness)
            for row in self._rows(
                "SELECT proposal_id, position, assumption FROM proposal_freshness ORDER BY proposal_id, position"
            )
        )
        return stored_state.ProposalRecords(proposals, evidence, freshness)

    def _artifacts(self) -> tuple[stored_state.ArtifactReference, ...]:
        return tuple(
            _decode_row(row, stored_state.ArtifactReference)
            for row in self._rows(
                """
                SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                       relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
                FROM artifact_refs
                ORDER BY artifact_ref_id
                """
            )
        )

    def _authority(self) -> stored_state.AuthorityRecords:
        coordination_rows = self._rows(
            """
            SELECT lease_id, task_id, host_id, generation, acquired_at, expires_at, status AS state
            FROM coordination_lease
            ORDER BY singleton
            """
        )
        if len(coordination_rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple coordination leases.")
        coordination = (
            _decode_row(coordination_rows[0], stored_state.StoredCoordinationLease) if coordination_rows else None
        )
        counters = tuple(
            _decode_row(row, stored_state.AttemptLeaseCounter)
            for row in self._rows(
                "SELECT attempt_id, generation_high_water FROM attempt_lease_counters ORDER BY attempt_id"
            )
        )
        generations = tuple(
            _decode_row(row, stored_state.AttemptLeaseGeneration)
            for row in self._rows(
                """
                SELECT attempt_id, generation, lease_id, task_id, host_id
                FROM attempt_lease_generations
                ORDER BY attempt_id, generation
                """
            )
        )
        leases = tuple(
            _decode_row(row, stored_state.StoredAttemptLease)
            for row in self._rows(
                """
                SELECT attempt_id, generation, acquired_at, expires_at, status AS state
                FROM attempt_leases
                ORDER BY attempt_id
                """
            )
        )
        return stored_state.AuthorityRecords(coordination, counters, generations, leases)

    def _history(self) -> tuple[stored_state.StoredTransitionReceipt, ...]:
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

    def _focus(self) -> stored_state.StoredFocus:
        rows = self._rows(
            "SELECT item_id, attempt_id, next_action, subject_revision FROM current_focus ORDER BY singleton"
        )
        if len(rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple focus records.")
        if not rows:
            return stored_state.StoredFocus(None, None, "select", 0)
        return _decode_row(rows[0], stored_state.StoredFocus)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json_text(value: work_models.CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


class _StoredStateWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_initial(self, state: stored_state.StoredWorkState) -> None:
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

    def _artifacts(self, records: tuple[stored_state.ArtifactReference, ...]) -> None:
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

    def _lifecycle(self, records: stored_state.LifecycleRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO work_items (
                item_id, user_label, state, timing, source, trigger, why_it_matters,
                effect, unlock, outcome_evidence, next_action, notes, scope_revision, scope_digest,
                subject_revision, recorded_at, updated_at, queue_position
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    value.queue_position,
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
                candidate_revision, candidate_recorded_at,
                accepted_scope_revision, accepted_scope_digest, subject_revision, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'brief', ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def _proposals(self, records: stored_state.ProposalRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO proposals (
                proposal_id, created_at, recorded_at, source_task_id, user_label,
                trigger, why_it_matters, relation_kind, relation_item_id, effect, unlock,
                urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
                disposition_recorded_at, subject_revision
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
                    value.relation.kind.value,
                    value.relation.item,
                    value.effect,
                    value.unlock,
                    value.urgency_evidence,
                    *_proposal_disposition_columns(value.disposition),
                    value.subject_revision,
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

    def _authority(self, records: stored_state.AuthorityRecords) -> None:
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

    def _focus(self, focus: stored_state.StoredFocus) -> None:
        self._connection.execute(
            """
            INSERT INTO current_focus (singleton, item_id, attempt_id, next_action, subject_revision)
            VALUES (1, ?, ?, ?, ?)
            """,
            (focus.item_id, focus.attempt_id, focus.next_action, focus.subject_revision),
        )

    def _history(self, records: tuple[stored_state.StoredTransitionReceipt, ...]) -> None:
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

    @staticmethod
    def _require_one(cursor: sqlite3.Cursor, message: str) -> None:
        if cursor.rowcount != 1:
            raise StorageError(StorageErrorCode.STALE_WRITE, message)

    @staticmethod
    def _item(state: stored_state.StoredWorkState, item_id: ItemId) -> stored_state.StoredWorkItem:
        item = next((value for value in state.lifecycle.work_items if value.item_id == item_id), None)
        if item is None:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The focused mutation item is missing.")
        return item

    @staticmethod
    def _attempt(state: stored_state.StoredWorkState, attempt_id: AttemptId) -> stored_state.StoredAttempt:
        attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
        if attempt is None:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The focused mutation attempt is missing.")
        return attempt

    @staticmethod
    def _queue_position(item: stored_state.StoredWorkItem) -> int:
        return item.queue_position or 0

    def _compact_queue(self, state: stored_state.StoredWorkState, removed_position: int) -> None:
        for item in sorted(
            (
                value
                for value in state.lifecycle.work_items
                if value.queue_position is not None and value.queue_position > removed_position
            ),
            key=self._queue_position,
        ):
            position = item.queue_position
            if position is None:  # pragma: no cover - narrowed by the collection filter
                continue
            self._require_one(
                self._connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (position - 1, item.item_id, position),
                ),
                "The live queue changed before terminal persistence.",
            )

    def _make_queue_space(self, state: stored_state.StoredWorkState, position: int) -> None:
        for item in sorted(
            (
                value
                for value in state.lifecycle.work_items
                if value.queue_position is not None and value.queue_position >= position
            ),
            key=self._queue_position,
            reverse=True,
        ):
            current = item.queue_position
            if current is None:  # pragma: no cover - narrowed by the collection filter
                continue
            self._require_one(
                self._connection.execute(
                    "UPDATE work_items SET queue_position = ? WHERE item_id = ? AND queue_position = ?",
                    (current + 1, item.item_id, current),
                ),
                "The live queue changed before proposal persistence.",
            )

    def _set_item_state(
        self,
        state: stored_state.StoredWorkState,
        item_id: ItemId,
        before_state: work_models.WorkState,
        after_state: stored_state.StoredWorkItemState,
        revision: int,
        now: datetime,
        outcome_evidence: str | None = None,
    ) -> None:
        current = self._item(state, item_id)
        terminal = after_state in {
            stored_state.StoredWorkItemState.DONE,
            stored_state.StoredWorkItemState.SUPERSEDED,
            stored_state.StoredWorkItemState.DROPPED,
        }
        self._require_one(
            self._connection.execute(
                """
                UPDATE work_items
                SET state = ?, outcome_evidence = ?, subject_revision = ?, updated_at = ?, queue_position = ?
                WHERE item_id = ? AND state = ? AND subject_revision = ?
                """,
                (
                    after_state.value,
                    outcome_evidence,
                    revision,
                    now.isoformat(),
                    None if terminal else current.queue_position,
                    item_id,
                    before_state.value,
                    current.subject_revision,
                ),
            ),
            "The focused item mutation is stale.",
        )
        if terminal and current.queue_position is not None:
            self._compact_queue(state, current.queue_position)

    def _set_attempt_state(
        self,
        state: stored_state.StoredWorkState,
        attempt_id: AttemptId,
        before_state: work_models.AttemptState,
        after_state: work_models.AttemptState,
        revision: int,
        now: datetime,
        *,
        revised_brief: decision_models.RevisedAttemptBrief | None = None,
        result_artifact_ref_id: ArtifactRefId | None = None,
        candidate_revision: str | None = None,
        candidate_recorded_at: datetime | None = None,
    ) -> None:
        current = self._attempt(state, attempt_id)
        if after_state == work_models.AttemptState.REVIEW:
            stored_candidate = candidate_revision
            stored_candidate_at = _timestamp(candidate_recorded_at)
        elif after_state in {
            work_models.AttemptState.ACTIVE,
            work_models.AttemptState.PAUSED,
            work_models.AttemptState.BLOCKED,
        }:
            stored_candidate = None
            stored_candidate_at = None
        else:
            stored_candidate = current.candidate_revision
            stored_candidate_at = _timestamp(current.candidate_recorded_at)
        self._require_one(
            self._connection.execute(
                """
                UPDATE attempts
                SET state = ?, brief_artifact_ref_id = ?, result_artifact_ref_id = ?,
                    result_artifact_kind = ?, candidate_revision = ?, candidate_recorded_at = ?,
                    accepted_scope_revision = ?, accepted_scope_digest = ?, subject_revision = ?, updated_at = ?
                WHERE attempt_id = ? AND state = ? AND subject_revision = ?
                """,
                (
                    after_state.value,
                    revised_brief.artifact_ref_id if revised_brief is not None else current.brief_artifact_ref_id,
                    result_artifact_ref_id or current.result_artifact_ref_id,
                    "result" if (result_artifact_ref_id or current.result_artifact_ref_id) is not None else None,
                    stored_candidate,
                    stored_candidate_at,
                    revised_brief.accepted_scope_revision
                    if revised_brief is not None
                    else current.accepted_scope_revision,
                    revised_brief.accepted_scope_digest if revised_brief is not None else current.accepted_scope_digest,
                    revision,
                    now.isoformat(),
                    attempt_id,
                    before_state.value,
                    current.subject_revision,
                ),
            ),
            "The focused attempt mutation is stale.",
        )

    def _replace_dependencies(self, item_id: ItemId, dependencies: tuple[ItemId, ...]) -> None:
        self._connection.execute("DELETE FROM item_dependencies WHERE item_id = ?", (item_id,))
        self._connection.executemany(
            "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
            tuple((item_id, dependency, position) for position, dependency in enumerate(dependencies)),
        )

    def _insert_attempt(
        self,
        state: stored_state.StoredWorkState,
        change: decision_models.ActivationChange,
        revision: int,
        now: datetime,
    ) -> None:
        item = self._item(state, change.item)
        try:
            self._connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, item_id, state, branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                    candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                    subject_revision, recorded_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, 'brief', NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    change.attempt,
                    change.item,
                    change.branch,
                    change.base_revision,
                    change.owner,
                    change.brief_artifact_ref_id,
                    item.scope_revision,
                    item.scope_digest,
                    revision,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StorageError(StorageErrorCode.STALE_WRITE, "The activation attempt already exists.") from error

    def _fence_attempt_authority(
        self,
        change: decision_models.AttemptAuthorityChange,
        decided_at: datetime,
    ) -> None:
        before = change.before
        after = change.after
        if after.lease_id is not None or after.generation != before.generation + 1:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "Attempt-authority fencing must allocate one revoked generation.",
            )
        anchor = self._connection.execute(
            """
            SELECT lease_id, task_id, host_id
            FROM attempt_lease_generations
            WHERE attempt_id = ? AND generation = ?
            """,
            (before.attempt, before.generation),
        ).fetchone()
        if anchor is None:
            raise StorageError(StorageErrorCode.STALE_WRITE, "The retained attempt generation is missing.")
        self._require_one(
            self._connection.execute(
                """
                UPDATE attempt_lease_counters
                SET generation_high_water = ?
                WHERE attempt_id = ? AND generation_high_water = ?
                """,
                (after.generation, before.attempt, before.generation),
            ),
            "The attempt-authority counter is stale.",
        )
        self._require_one(
            self._connection.execute(
                """
                UPDATE attempt_leases
                SET generation = ?, expires_at = ?, status = 'revoked'
                WHERE attempt_id = ? AND generation = ?
                """,
                (after.generation, decided_at.isoformat(), before.attempt, before.generation),
            ),
            "The current attempt lease is stale.",
        )
        self._connection.execute(
            """
            INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (before.attempt, after.generation, anchor["lease_id"], anchor["task_id"], anchor["host_id"]),
        )

    def _update_focus(self, before: stored_state.StoredFocus, after: stored_state.StoredFocus) -> None:
        self._require_one(
            self._connection.execute(
                """
                UPDATE current_focus
                SET item_id = ?, attempt_id = ?, next_action = ?, subject_revision = ?
                WHERE singleton = 1 AND subject_revision = ?
                """,
                (after.item_id, after.attempt_id, after.next_action, after.subject_revision, before.subject_revision),
            ),
            "The focused mutation no longer matches current focus.",
        )

    def _set_proposal_disposition(
        self,
        proposal_id: ProposalId,
        disposition: work_models.ProposalDisposition,
        revision: int,
    ) -> None:
        kind, target, reason, disposed_at = _proposal_disposition_columns(disposition)
        self._require_one(
            self._connection.execute(
                """
                UPDATE proposals
                SET disposition = ?, disposition_target_item_id = ?, disposition_reason = ?,
                    disposition_recorded_at = ?, subject_revision = ?
                WHERE proposal_id = ? AND disposition IS NULL
                """,
                (kind, target, reason, disposed_at, revision, proposal_id),
            ),
            "The proposal disposition is stale.",
        )

    def _accept_proposal(
        self,
        state: stored_state.StoredWorkState,
        change: decision_models.AcceptedProposalChange,
        revision: int,
        now: datetime,
    ) -> None:
        accepted = change.accepted_item
        current = self._item(state, accepted.item)
        scope_revision = current.scope_revision
        if current.scope_digest != accepted.scope_digest:
            scope_revision += 1
            self._connection.execute(
                """
                INSERT INTO item_scope_revisions (
                    item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (accepted.item, scope_revision, accepted.scope_digest, revision, now.isoformat()),
            )
        self._require_one(
            self._connection.execute(
                """
                UPDATE work_items
                SET user_label = ?, state = ?, timing = ?, source = ?, trigger = ?, why_it_matters = ?,
                    effect = ?, unlock = ?, next_action = ?, notes = ?, scope_revision = ?, scope_digest = ?,
                    subject_revision = ?, updated_at = ?
                WHERE item_id = ? AND state = 'intake' AND subject_revision = ?
                """,
                (
                    accepted.user_label,
                    accepted.state.value,
                    None if accepted.timing is None else accepted.timing.value,
                    accepted.source,
                    accepted.trigger,
                    accepted.why_it_matters,
                    accepted.effect,
                    accepted.unlock,
                    accepted.next_action,
                    accepted.notes,
                    scope_revision,
                    accepted.scope_digest,
                    revision,
                    now.isoformat(),
                    accepted.item,
                    current.subject_revision,
                ),
            ),
            "The accepted proposal item is stale.",
        )
        self._replace_dependencies(accepted.item, accepted.dependencies)
        self._set_proposal_disposition(
            change.proposal,
            work_models.AcceptedProposalDisposition(accepted.item, change.disposed_at),
            revision,
        )

    def _create_proposal(self, state: stored_state.StoredWorkState, mutation: ProposalCreationMutation) -> None:
        decision = mutation.decision
        intake = decision.proposal
        visible = decision.visible_item
        revision = mutation.receipt.project_revision
        now = mutation.receipt.transition.decided_at
        self._make_queue_space(state, visible.position)
        relation = intake.relation
        self._connection.execute(
            """
            INSERT INTO proposals (
                proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                relation_kind, relation_item_id, effect, unlock, urgency_evidence, disposition,
                disposition_target_item_id, disposition_reason, disposition_recorded_at, subject_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
            """,
            (
                intake.proposal_id,
                intake.created_at.isoformat(),
                now.isoformat(),
                intake.source_task_id,
                intake.user_label,
                intake.trigger,
                intake.why_it_matters,
                relation.kind.value,
                relation.item,
                intake.effect,
                intake.unlock,
                intake.urgency_evidence,
                revision,
            ),
        )
        self._connection.executemany(
            "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
            tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.evidence)),
        )
        self._connection.executemany(
            "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
            tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.freshness)),
        )
        self._connection.execute(
            """
            INSERT INTO work_items (
                item_id, user_label, state, timing, source, trigger, why_it_matters, effect, unlock,
                outcome_evidence, next_action, notes, scope_revision, scope_digest, subject_revision,
                recorded_at, updated_at, queue_position
            ) VALUES (?, ?, 'intake', NULL, ?, ?, ?, ?, ?, NULL, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                visible.item_id,
                intake.user_label,
                f"proposal:{intake.proposal_id}",
                intake.trigger,
                intake.why_it_matters,
                intake.effect,
                intake.unlock,
                intake.unlock,
                intake.urgency_evidence,
                visible.scope_digest,
                revision,
                now.isoformat(),
                now.isoformat(),
                visible.position,
            ),
        )
        self._connection.execute(
            """
            INSERT INTO item_scope_revisions (
                item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
            ) VALUES (?, 1, ?, ?, ?)
            """,
            (visible.item_id, visible.scope_digest, revision, now.isoformat()),
        )
        self._replace_dependencies(visible.item_id, visible.dependencies)
        prerequisite = decision.prerequisite_change
        if prerequisite is not None:
            target = self._item(state, prerequisite.item_id)
            if (
                target.scope_revision != prerequisite.scope_revision
                or target.scope_digest != prerequisite.scope_digest_before
            ):
                raise StorageError(StorageErrorCode.STALE_WRITE, "The prerequisite target scope is stale.")
            next_scope_revision = target.scope_revision + 1
            self._connection.execute(
                """
                INSERT INTO item_scope_revisions (
                    item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    prerequisite.item_id,
                    next_scope_revision,
                    prerequisite.scope_digest_after,
                    revision,
                    now.isoformat(),
                ),
            )
            self._require_one(
                self._connection.execute(
                    """
                    UPDATE work_items
                    SET scope_revision = ?, scope_digest = ?, subject_revision = ?, updated_at = ?
                    WHERE item_id = ? AND scope_revision = ? AND scope_digest = ? AND subject_revision = ?
                    """,
                    (
                        next_scope_revision,
                        prerequisite.scope_digest_after,
                        revision,
                        now.isoformat(),
                        prerequisite.item_id,
                        target.scope_revision,
                        target.scope_digest,
                        target.subject_revision,
                    ),
                ),
                "The prerequisite target changed before persistence.",
            )
            self._connection.execute(
                "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
                (prerequisite.item_id, prerequisite.dependency_id, prerequisite.position),
            )

    def _change_coordination_authority(self, mutation: CoordinationAuthorityMutation) -> None:
        decision = mutation.decision
        after = decision.after
        if decision.before is None:
            self._connection.execute(
                """
                INSERT INTO coordination_lease (
                    singleton, lease_id, task_id, host_id, generation, acquired_at, expires_at, status
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
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
            )
            return
        before = decision.before
        self._require_one(
            self._connection.execute(
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
            "The coordination authority changed before persistence.",
        )

    def _change_attempt_authority(self, mutation: AttemptAuthorityMutation) -> None:
        decision = mutation.decision
        after = decision.current_after
        retained_counter = self._connection.execute(
            "SELECT generation_high_water FROM attempt_lease_counters WHERE attempt_id = ?",
            (decision.attempt,),
        ).fetchone()
        if retained_counter is None:
            if decision.counter_before != 0:
                raise StorageError(StorageErrorCode.STALE_WRITE, "The attempt counter is missing.")
            try:
                self._connection.execute(
                    "INSERT INTO attempt_lease_counters (attempt_id, generation_high_water) VALUES (?, ?)",
                    (decision.attempt, decision.counter_after),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(StorageErrorCode.STALE_WRITE, "The attempt counter already exists.") from error
        else:
            self._require_one(
                self._connection.execute(
                    """
                    UPDATE attempt_lease_counters
                    SET generation_high_water = ?
                    WHERE attempt_id = ? AND generation_high_water = ?
                    """,
                    (decision.counter_after, decision.attempt, decision.counter_before),
                ),
                "The attempt-authority counter is stale.",
            )
        self._connection.execute(
            """
            INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id, generation) DO NOTHING
            """,
            (after.attempt, after.generation, after.lease_id, after.task_id, after.host_id),
        )
        anchor = self._connection.execute(
            """
            SELECT lease_id, task_id, host_id
            FROM attempt_lease_generations
            WHERE attempt_id = ? AND generation = ?
            """,
            (after.attempt, after.generation),
        ).fetchone()
        if anchor is None or tuple(anchor) != (after.lease_id, after.task_id, after.host_id):
            raise StorageError(StorageErrorCode.STALE_WRITE, "The retained attempt generation conflicts.")
        current_before = decision.current_before
        if current_before is None:
            try:
                self._connection.execute(
                    """
                    INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        after.attempt,
                        after.generation,
                        after.acquired_at.isoformat(),
                        after.expires_at.isoformat(),
                        after.state.value,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(StorageErrorCode.STALE_WRITE, "The current attempt lease already exists.") from error
            return
        self._require_one(
            self._connection.execute(
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

    def _transfer_coordinator(self, change: decision_models.CoordinatorAuthorityChange) -> None:
        before = change.before
        after = change.after
        self._require_one(
            self._connection.execute(
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
            "The coordinator transfer is stale.",
        )

    def _accept_checkpoint_artifact(
        self,
        state: stored_state.StoredWorkState,
        published: ArtifactRef,
        expected_kind: stored_state.ArtifactKind,
        expected_id: ArtifactRefId,
        revision: int,
        now: datetime,
    ) -> ArtifactRefId:
        if published.kind != expected_kind:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Checkpoint artifact kind is invalid.")
        existing = next(
            (
                value
                for value in state.artifact_references
                if (value.kind, value.key, value.revision) == (published.kind, published.key, published.revision)
            ),
            None,
        )
        if existing is not None:
            if existing.artifact_ref_id != expected_id or (
                existing.selector,
                existing.content_sha256,
                existing.size_bytes,
            ) != (published.selector, published.content_sha256, published.size_bytes):
                raise StorageError(
                    StorageErrorCode.INVARIANT_VIOLATION,
                    "An accepted checkpoint artifact identity names different bytes.",
                )
            return existing.artifact_ref_id
        self._connection.execute(
            """
            INSERT INTO artifact_refs (
                artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
                content_sha256, size_bytes, accepted_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expected_id,
                published.key,
                published.revision,
                published.kind.value,
                published.selector,
                published.content_sha256,
                published.size_bytes,
                revision,
                now.isoformat(),
            ),
        )
        return expected_id

    def _transition(self, state: stored_state.StoredWorkState, mutation: TransitionMutation) -> None:  # noqa: C901, PLR0912, PLR0915
        change = mutation.decision.change
        revision = mutation.receipt.project_revision
        now = mutation.decision.receipt.decided_at
        match change:
            case decision_models.ItemStateChange(item=item, before=before, after=after):
                self._set_item_state(state, item, before, stored_state.stored_live_work_state(after), revision, now)
            case decision_models.ActivationChange(item=item, item_before=before):
                self._set_item_state(state, item, before, stored_state.StoredWorkItemState.ACTIVE, revision, now)
                self._insert_attempt(state, change, revision, now)
            case decision_models.AttemptStateChange(
                item=item,
                item_before=item_before,
                item_after=item_after,
                attempt=attempt,
                attempt_before=attempt_before,
                attempt_after=attempt_after,
            ):
                self._set_item_state(
                    state, item, item_before, stored_state.stored_live_work_state(item_after), revision, now
                )
                self._set_attempt_state(state, attempt, attempt_before, attempt_after, revision, now)
            case decision_models.BlockAttemptChange(
                item=item,
                item_before=item_before,
                attempt=attempt,
                attempt_before=attempt_before,
                dependencies_after=dependencies,
            ):
                self._set_item_state(state, item, item_before, stored_state.StoredWorkItemState.BLOCKED, revision, now)
                self._set_attempt_state(
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.BLOCKED,
                    revision,
                    now,
                )
                self._replace_dependencies(item, dependencies)
            case decision_models.BlockItemChange(item=item, item_before=item_before, dependencies_after=dependencies):
                self._set_item_state(state, item, item_before, stored_state.StoredWorkItemState.BLOCKED, revision, now)
                self._replace_dependencies(item, dependencies)
            case decision_models.ResumeAttemptChange(
                item=item,
                item_before=item_before,
                attempt=attempt,
                attempt_before=attempt_before,
                revised_brief=revised_brief,
            ):
                self._set_item_state(state, item, item_before, stored_state.StoredWorkItemState.ACTIVE, revision, now)
                self._set_attempt_state(
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.ACTIVE,
                    revision,
                    now,
                    revised_brief=revised_brief,
                )
            case decision_models.ReviewSubmissionChange(
                item=item,
                attempt=attempt,
                protected_candidate_after=candidate,
                candidate_observed_at=observed_at,
            ):
                self._set_item_state(
                    state,
                    item,
                    work_models.WorkState.ACTIVE,
                    stored_state.StoredWorkItemState.REVIEW,
                    revision,
                    now,
                )
                self._set_attempt_state(
                    state,
                    attempt,
                    work_models.AttemptState.ACTIVE,
                    work_models.AttemptState.REVIEW,
                    revision,
                    now,
                    candidate_revision=str(candidate),
                    candidate_recorded_at=observed_at,
                )
            case (
                decision_models.ReviewAcceptanceChange(item=item, attempt=attempt, authority_change=authority)
                | decision_models.ReviewReturnChange(item=item, attempt=attempt, authority_change=authority)
            ):
                self._set_item_state(
                    state,
                    item,
                    work_models.WorkState.REVIEW,
                    stored_state.StoredWorkItemState.ACTIVE,
                    revision,
                    now,
                )
                self._set_attempt_state(
                    state,
                    attempt,
                    work_models.AttemptState.REVIEW,
                    work_models.AttemptState.ACTIVE,
                    revision,
                    now,
                )
                self._fence_attempt_authority(authority, now)
            case decision_models.CompletionChange(
                item=item,
                item_before=item_before,
                attempt=attempt,
                attempt_before=attempt_before,
                evidence=evidence,
                authority_change=authority,
            ):
                self._set_item_state(
                    state,
                    item,
                    item_before,
                    stored_state.StoredWorkItemState.DONE,
                    revision,
                    now,
                    evidence,
                )
                self._set_attempt_state(
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.DONE,
                    revision,
                    now,
                )
                if authority is not None:
                    self._fence_attempt_authority(authority, now)
            case decision_models.ItemClosureChange(
                item=item,
                item_before=item_before,
                terminal_state=terminal_state,
                evidence=evidence,
            ):
                self._set_item_state(
                    state,
                    item,
                    item_before,
                    stored_state.stored_close_outcome(terminal_state),
                    revision,
                    now,
                    evidence,
                )
            case decision_models.AttemptClosureChange(
                item=item,
                item_before=item_before,
                terminal_state=terminal_state,
                evidence=evidence,
                attempt=attempt,
                attempt_before=attempt_before,
                authority_change=authority,
            ):
                self._set_item_state(
                    state,
                    item,
                    item_before,
                    stored_state.stored_close_outcome(terminal_state),
                    revision,
                    now,
                    evidence,
                )
                self._set_attempt_state(
                    state,
                    attempt,
                    attempt_before,
                    work_models.AttemptState.DONE,
                    revision,
                    now,
                )
                if authority is not None:
                    self._fence_attempt_authority(authority, now)
            case decision_models.AcceptedProposalChange():
                self._accept_proposal(state, change, revision, now)
            case decision_models.MergedProposalChange(proposal=proposal, target_item=target, disposed_at=disposed_at):
                self._set_item_state(
                    state,
                    ItemId(proposal),
                    work_models.WorkState.INTAKE,
                    stored_state.StoredWorkItemState.SUPERSEDED,
                    revision,
                    now,
                    f"Merged into {target}.",
                )
                self._set_proposal_disposition(
                    proposal,
                    work_models.MergedProposalDisposition(target, disposed_at),
                    revision,
                )
            case decision_models.ReturnedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
                self._set_proposal_disposition(
                    proposal,
                    work_models.ReturnedProposalDisposition(reason, disposed_at),
                    revision,
                )
            case decision_models.RejectedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
                self._set_item_state(
                    state,
                    ItemId(proposal),
                    work_models.WorkState.INTAKE,
                    stored_state.StoredWorkItemState.DROPPED,
                    revision,
                    now,
                    reason,
                )
                self._set_proposal_disposition(
                    proposal,
                    work_models.RejectedProposalDisposition(reason, disposed_at),
                    revision,
                )
            case decision_models.CheckpointAcceptanceChange(
                item=item,
                attempt=attempt,
                authority_change=authority,
            ):
                artifacts = mutation.checkpoint_artifacts
                if artifacts is None or mutation.receipt.artifact_ref_id != artifacts.review_id:
                    raise StorageError(
                        StorageErrorCode.INVARIANT_VIOLATION,
                        "Checkpoint acceptance requires exact result and review artifacts.",
                    )
                self._accept_checkpoint_artifact(
                    state,
                    artifacts.result,
                    stored_state.ArtifactKind.RESULT,
                    artifacts.result_id,
                    revision,
                    now,
                )
                self._accept_checkpoint_artifact(
                    state,
                    artifacts.review,
                    stored_state.ArtifactKind.EVIDENCE,
                    artifacts.review_id,
                    revision,
                    now,
                )
                self._set_item_state(
                    state,
                    item,
                    work_models.WorkState.REVIEW,
                    stored_state.StoredWorkItemState.PAUSED,
                    revision,
                    now,
                )
                self._set_attempt_state(
                    state,
                    attempt,
                    work_models.AttemptState.REVIEW,
                    work_models.AttemptState.PAUSED,
                    revision,
                    now,
                    result_artifact_ref_id=artifacts.result_id,
                )
                self._fence_attempt_authority(authority, now)
            case decision_models.CoordinatorTransferChange(authority_change=authority):
                self._transfer_coordinator(authority)
            case _ as unreachable:
                assert_never(unreachable)
        if mutation.focus_after is not None:
            self._update_focus(state.focus, mutation.focus_after)

    def persist(self, state: stored_state.StoredWorkState, mutation: StoredStateMutation) -> None:
        """Persist one focused accepted mutation without rebuilding unrelated relations."""

        receipt = stored_transition_receipt(mutation)
        expected_history_id = 1 + max((int(value.history_id) for value in state.transition_receipts), default=0)
        if (
            int(receipt.history_id) != expected_history_id
            or receipt.project_revision != state.lifecycle.project.revision + 1
        ):
            raise StorageError(
                StorageErrorCode.STALE_WRITE,
                "The focused mutation receipt does not identify the next project revision exactly.",
            )
        self._connection.execute("PRAGMA defer_foreign_keys = ON")
        match mutation:
            case TransitionMutation():
                self._transition(state, mutation)
            case ProposalCreationMutation():
                self._create_proposal(state, mutation)
            case CoordinationAuthorityMutation():
                self._change_coordination_authority(mutation)
            case AttemptAuthorityMutation():
                self._change_attempt_authority(mutation)
            case _ as unreachable:
                assert_never(unreachable)
        self._require_one(
            self._connection.execute(
                """
                UPDATE project_meta
                SET revision = ?, updated_at = ?
                WHERE singleton = 1 AND revision = ?
                """,
                (
                    receipt.project_revision,
                    receipt.committed_at.isoformat(),
                    state.lifecycle.project.revision,
                ),
            ),
            "The project revision changed before focused persistence.",
        )
        self._history((receipt,))


class _SQLiteWorkTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def snapshot(self) -> stored_state.StoredWorkState:
        return _StoredStateReader(self._connection).read()

    def commit(self, mutation: StoredStateMutation) -> decision_models.TransitionReceipt:
        current = _StoredStateReader(self._connection).read()
        writer = _StoredStateWriter(self._connection)
        writer.persist(current, mutation)
        _StoredStateReader(self._connection).read()
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

    def snapshot(self) -> stored_state.StoredWorkState:
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

    def initialize_state(self, state: stored_state.StoredWorkState) -> None:
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
        role: work_models.ArtifactRole | None = None,
    ) -> stored_state.ArtifactReference:
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
                    reference = stored_state.ArtifactReference(
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
                writer = _StoredStateWriter(connection)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO artifact_refs (
                            artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
                            content_sha256, size_bytes, accepted_revision, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            reference.artifact_ref_id,
                            reference.key,
                            reference.revision,
                            reference.kind.value,
                            reference.selector,
                            reference.content_sha256,
                            reference.size_bytes,
                            reference.accepted_revision,
                            reference.created_at.isoformat(),
                        ),
                    )
                if item_id is not None and role is not None:
                    item = next(
                        (value for value in before.lifecycle.work_items if value.item_id == item_id),
                        None,
                    )
                    if item is None or published.kind.value != role.value:
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "Artifact relationship does not match a current item and compatible role.",
                        )
                    position = sum(
                        1
                        for value in before.lifecycle.item_artifacts
                        if value.item_id == item_id and value.role == role
                    )
                    writer._require_one(
                        connection.execute(
                            """
                            UPDATE work_items
                            SET subject_revision = ?, updated_at = ?
                            WHERE item_id = ? AND subject_revision = ?
                            """,
                            (revision, accepted_at.isoformat(), item_id, item.subject_revision),
                        ),
                        "The artifact relationship item changed before persistence.",
                    )
                    connection.execute(
                        """
                        INSERT INTO item_artifacts (item_id, artifact_ref_id, role, position)
                        VALUES (?, ?, ?, ?)
                        """,
                        (item_id, reference.artifact_ref_id, role.value, position),
                    )
                writer._require_one(
                    connection.execute(
                        """
                        UPDATE project_meta
                        SET revision = ?, updated_at = ?
                        WHERE singleton = 1 AND revision = ?
                        """,
                        (revision, accepted_at.isoformat(), before.lifecycle.project.revision),
                    ),
                    "The project revision changed before artifact acceptance.",
                )
                _StoredStateReader(connection).read()
                return reference
        finally:
            connection.close()
