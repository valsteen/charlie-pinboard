"""Read, initialize, and change proposal records on a supplied connection.

This module never commits, rolls back, closes the connection, calls callbacks,
reads the filesystem, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row, stale_write
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import item, make_queue_space, replace_dependencies
from pinboard.application import stored_state
from pinboard.application.mutation_models import ProposalCreationMutation
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId


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
        return stored_state.StoredProposal(
            self.proposal_id,
            self.created_at,
            self.recorded_at,
            self.source_task_id,
            self.user_label,
            self.trigger,
            self.why_it_matters,
            _stored_proposal_relation(self.relation_kind, self.relation_item_id),
            self.effect,
            self.unlock,
            self.urgency_evidence,
            _stored_proposal_disposition(
                self.disposition,
                self.disposition_target_item_id,
                self.disposition_reason,
                self.disposition_recorded_at,
            ),
            self.subject_revision,
        )


def _required_relation_item(kind: work_models.ProposalRelationKind, value: ItemId | None) -> ItemId:
    if value is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has no related item.")
    return value


def _forbidden_relation_item(kind: work_models.ProposalRelationKind, value: ItemId | None) -> None:
    if value is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"{kind.value} proposal has a related item.")


def _stored_proposal_relation(
    kind: work_models.ProposalRelationKind,
    value: ItemId | None,
) -> work_models.ProposalRelation:
    match kind:
        case work_models.ProposalRelationKind.INDEPENDENT:
            _forbidden_relation_item(kind, value)
            return work_models.IndependentProposalRelation()
        case work_models.ProposalRelationKind.PREREQUISITE:
            return work_models.PrerequisiteProposalRelation(_required_relation_item(kind, value))
        case work_models.ProposalRelationKind.FOLLOW_UP:
            return work_models.FollowUpProposalRelation(_required_relation_item(kind, value))
        case work_models.ProposalRelationKind.DUPLICATE:
            return work_models.DuplicateProposalRelation(_required_relation_item(kind, value))
        case work_models.ProposalRelationKind.CONTRADICTION:
            return work_models.ContradictionProposalRelation(_required_relation_item(kind, value))
        case work_models.ProposalRelationKind.CLARIFICATION:
            _forbidden_relation_item(kind, value)
            return work_models.ClarificationProposalRelation()
        case _ as unreachable:
            assert_never(unreachable)


def _required_disposition_target(
    kind: work_models.ProposalDispositionKind,
    target: ItemId | None,
) -> ItemId:
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


def _forbid_disposition_value(
    kind: work_models.ProposalDispositionKind,
    name: str,
    value: ItemId | str | None,
) -> None:
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


def read_proposals(connection: sqlite3.Connection) -> stored_state.ProposalRecords:
    proposals = tuple(
        decode_row(row, _StoredProposalRow).proposal()
        for row in connection.execute(
            """
            SELECT proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                   relation_kind, relation_item_id, effect, unlock, urgency_evidence, disposition,
                   disposition_target_item_id, disposition_reason, subject_revision, disposition_recorded_at
            FROM proposals
            ORDER BY proposal_id
            """
        ).fetchall()
    )
    evidence = tuple(
        decode_row(row, stored_state.ProposalEvidence)
        for row in connection.execute(
            "SELECT proposal_id, position, selector FROM proposal_evidence ORDER BY proposal_id, position"
        ).fetchall()
    )
    freshness = tuple(
        decode_row(row, stored_state.ProposalFreshness)
        for row in connection.execute(
            "SELECT proposal_id, position, assumption FROM proposal_freshness ORDER BY proposal_id, position"
        ).fetchall()
    )
    return stored_state.ProposalRecords(proposals, evidence, freshness)


def insert_proposals(connection: sqlite3.Connection, records: stored_state.ProposalRecords) -> None:
    connection.executemany(
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
    connection.executemany(
        "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
        tuple((value.proposal_id, value.position, value.selector) for value in records.evidence),
    )
    connection.executemany(
        "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
        tuple((value.proposal_id, value.position, value.assumption) for value in records.freshness),
    )


def set_proposal_disposition(
    connection: sqlite3.Connection,
    proposal_id: ProposalId,
    disposition: work_models.ProposalDisposition,
    revision: int,
) -> DecisionFailure | None:
    kind, target, reason, disposed_at = _proposal_disposition_columns(disposition)
    return require_one_changed_row(
        connection.execute(
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


def accept_proposal(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    change: decision_models.AcceptedProposalChange,
    revision: int,
    now: datetime,
) -> DecisionFailure | None:
    accepted = change.accepted_item
    current = item(state, accepted.item)
    scope_revision = current.scope_revision
    if current.scope_digest != accepted.scope_digest:
        scope_revision += 1
        connection.execute(
            """
            INSERT INTO item_scope_revisions (
                item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (accepted.item, scope_revision, accepted.scope_digest, revision, now.isoformat()),
        )
    if (
        failure := require_one_changed_row(
            connection.execute(
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
    ) is not None:
        return failure
    replace_dependencies(connection, accepted.item, accepted.dependencies)
    return set_proposal_disposition(
        connection,
        change.proposal,
        work_models.AcceptedProposalDisposition(accepted.item, change.disposed_at),
        revision,
    )


def create_proposal(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    mutation: ProposalCreationMutation,
) -> DecisionFailure | None:
    decision = mutation.decision
    intake = decision.proposal
    visible = decision.visible_item
    revision = mutation.receipt.project_revision
    now = mutation.receipt.transition.decided_at
    if (failure := make_queue_space(connection, state, visible.position)) is not None:
        return failure
    relation = intake.relation
    connection.execute(
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
    connection.executemany(
        "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
        tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.evidence)),
    )
    connection.executemany(
        "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
        tuple((intake.proposal_id, position, value) for position, value in enumerate(decision.freshness)),
    )
    connection.execute(
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
    connection.execute(
        """
        INSERT INTO item_scope_revisions (
            item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
        ) VALUES (?, 1, ?, ?, ?)
        """,
        (visible.item_id, visible.scope_digest, revision, now.isoformat()),
    )
    replace_dependencies(connection, visible.item_id, visible.dependencies)
    prerequisite = decision.prerequisite_change
    if prerequisite is None:
        return None
    target = item(state, prerequisite.item_id)
    if target.scope_revision != prerequisite.scope_revision or target.scope_digest != prerequisite.scope_digest_before:
        return stale_write("The prerequisite target scope is stale.")
    next_scope_revision = target.scope_revision + 1
    connection.execute(
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
    if (
        failure := require_one_changed_row(
            connection.execute(
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
    ) is not None:
        return failure
    connection.execute(
        "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
        (prerequisite.item_id, prerequisite.dependency_id, prerequisite.position),
    )
    return None
