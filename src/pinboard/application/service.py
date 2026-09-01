from datetime import datetime
from typing import assert_never

from pinboard.application import stored_state
from pinboard.application.artifact_publication import validate_transition_work_brief
from pinboard.application.artifacts import CheckpointArtifacts, WorkBriefIdentity
from pinboard.application.decision_projection import (
    project_decision_snapshot,
)
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
)
from pinboard.application.mutations import (
    project_checkpoint_acceptance_mutation,
    project_transition_mutation,
)
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_coordination_authority,
    decide_preparation_authority,
)
from pinboard.domain.authority_models import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AcquireInitialPreparationAuthority,
    AttemptAuthorityOperation,
    AttemptLeaseAuthority,
    CoordinationAuthorityOperation,
    PreparationAuthorityOperation,
    PreparationLeaseAuthority,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    ReleasePreparationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RenewPreparationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    RevokePreparationAuthority,
    TransferAttemptAuthority,
    TransferPreparationAuthority,
)
from pinboard.domain.decisions import decide, validate_supplied_action
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_decisions import decide_proposal_creation
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
    LocalIntakeAuthority,
)


def change_coordination_authority(
    store: WorkStore,
    operation: CoordinationAuthorityOperation,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Decide and persist one exact coordination-authority mutation."""

    match operation:
        case AcquireCoordinationAuthority(acquired_at=operation_time):
            pass
        case RenewCoordinationAuthority(renewed_at=operation_time):
            pass
        case ReleaseCoordinationAuthority(released_at=operation_time):
            pass
        case RevokeCoordinationAuthority(revoked_at=operation_time):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before, operation_time)
        decision = decide_coordination_authority(snapshot.coordination_lease, operation)
        if isinstance(decision, DecisionFailure):
            return decision
        after_authority = decision.after
        match operation:
            case AcquireCoordinationAuthority(acquired_at=decided_at):
                outcome = "acquire-coordination-authority"
                authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATOR
            case RenewCoordinationAuthority(renewed_at=decided_at):
                outcome = "renew-coordination-authority"
                authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
            case ReleaseCoordinationAuthority(released_at=decided_at):
                outcome = "release-coordination-authority"
                authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
            case RevokeCoordinationAuthority(revoked_at=decided_at):
                outcome = "revoke-coordination-authority"
                authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATOR
            case _ as unreachable:
                assert_never(unreachable)
        transition = decision_models.TransitionReceipt(
            ActionId(f"continue:coordination-authority:{after_authority.generation}"),
            None,
            outcome,
            None,
            decided_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            stored_state.TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId("ledger"),
            None,
            authorization,
            after_authority.task_id,
            after_authority.host_id,
            "coordination-authority/v1",
            work_models.CanonicalJson(b"{}"),
        )
        return transaction.commit(CoordinationAuthorityMutation(receipt, decision))


def _retained_attempt_authority(
    state: stored_state.StoredWorkState,
    attempt_id: AttemptId,
) -> AttemptLeaseAuthority | None:
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if lease is None or attempt is None:
        return None
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        if lease.generation != 0:
            return None
        lease_id = LeaseId("unclaimed")
        task_id = TaskId("unclaimed")
        host_id = HostId("unclaimed")
    else:
        lease_id = anchor.lease_id
        task_id = anchor.task_id
        host_id = anchor.host_id
    return AttemptLeaseAuthority(
        state.lifecycle.project.host_epoch,
        attempt_id,
        attempt.item_id,
        task_id,
        host_id,
        lease_id,
        lease.generation,
        lease.acquired_at,
        lease.expires_at,
        lease.state,
    )


def change_attempt_authority(
    store: WorkStore,
    operation: AttemptAuthorityOperation,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Decide and persist one exact attempt-authority mutation."""

    match operation:
        case AcquireInitialAttemptAuthority(attempt=attempt_id, acquired_at=decided_at):
            outcome = "acquire-initial-attempt-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATOR
        case TransferAttemptAuthority(current=current, acquired_at=decided_at):
            attempt_id = current.attempt
            outcome = "transfer-attempt-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
        case RenewAttemptAuthority(current=current, renewed_at=decided_at):
            attempt_id = current.attempt
            outcome = "renew-attempt-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.ATTEMPT
        case ReleaseAttemptAuthority(current=current, released_at=decided_at):
            attempt_id = current.attempt
            outcome = "release-attempt-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.ATTEMPT
        case RevokeAttemptAuthority(attempt=attempt_id, revoked_at=decided_at):
            outcome = "revoke-attempt-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before, decided_at)
        counter = next(
            (
                value.generation_high_water
                for value in before.authority.attempt_counters
                if value.attempt_id == attempt_id
            ),
            0,
        )
        retained = _retained_attempt_authority(before, attempt_id)
        decision = decide_attempt_authority(
            retained,
            counter,
            operation,
            snapshot.coordination_lease,
            live_attempt=(
                (attempt_id, attempt.item)
                if (attempt := snapshot.attempt(attempt_id)) is not None
                and attempt.state == work_models.AttemptState.ACTIVE
                else None
            ),
            transferable_attempt=(
                (attempt_id, attempt.item)
                if (attempt := snapshot.attempt(attempt_id)) is not None
                and attempt.state != work_models.AttemptState.DONE
                else None
            ),
            project_host_epoch=snapshot.host_epoch,
        )
        if isinstance(decision, DecisionFailure):
            return decision
        after = decision.current_after
        transition = decision_models.TransitionReceipt(
            ActionId(f"continue:attempt-authority:{attempt_id}:{after.generation}"),
            next(
                (value.item_id for value in before.lifecycle.attempts if value.attempt_id == attempt_id),
                None,
            ),
            outcome,
            None,
            decided_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            stored_state.TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(attempt_id),
            None,
            authorization,
            after.task_id,
            after.host_id,
            "attempt-authority/v1",
            work_models.CanonicalJson(b"{}"),
        )
        return transaction.commit(AttemptAuthorityMutation(receipt, decision))


def _retained_preparation_authority(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
) -> PreparationLeaseAuthority | None:
    lease = next((value for value in state.authority.preparation_leases if value.item_id == item_id), None)
    if lease is None:
        return None
    anchor = next(
        (
            value
            for value in state.authority.preparation_generations
            if value.item_id == item_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        return None
    return PreparationLeaseAuthority(
        state.lifecycle.project.host_epoch,
        item_id,
        lease.definition_revision,
        lease.definition_digest,
        anchor.task_id,
        anchor.host_id,
        anchor.lease_id,
        lease.generation,
        lease.acquired_at,
        lease.expires_at,
        lease.state,
    )


def change_preparation_authority(
    store: WorkStore,
    operation: PreparationAuthorityOperation,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Decide and persist one exact ready-item preparation mutation."""

    match operation:
        case AcquireInitialPreparationAuthority(item=item_id, acquired_at=decided_at):
            outcome = "acquire-initial-preparation-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATOR
        case TransferPreparationAuthority(current=current, acquired_at=decided_at):
            item_id = current.item
            outcome = "transfer-preparation-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
        case RenewPreparationAuthority(current=current, renewed_at=decided_at):
            item_id = current.item
            outcome = "renew-preparation-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.PREPARATION
        case ReleasePreparationAuthority(current=current, released_at=decided_at):
            item_id = current.item
            outcome = "release-preparation-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.PREPARATION
        case RevokePreparationAuthority(item=item_id, revoked_at=decided_at):
            outcome = "revoke-preparation-authority"
            authorization = stored_state.TransitionHistoryAuthorizationKind.COORDINATION
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before, decided_at)
        counter = next(
            (
                value.generation_high_water
                for value in before.authority.preparation_counters
                if value.item_id == item_id
            ),
            0,
        )
        decision = decide_preparation_authority(
            _retained_preparation_authority(before, item_id),
            counter,
            operation,
            snapshot,
            decided_at,
        )
        if isinstance(decision, DecisionFailure):
            return decision
        after = decision.current_after
        transition = decision_models.TransitionReceipt(
            ActionId(f"continue:preparation-authority:{item_id}:{after.generation}"),
            item_id,
            outcome,
            None,
            decided_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            stored_state.TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(item_id),
            None,
            authorization,
            after.task_id,
            after.host_id,
            "preparation-authority/v1",
            work_models.CanonicalJson(b"{}"),
        )
        return transaction.commit(PreparationAuthorityMutation(receipt, decision))


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Persist immutable proposal facts and their intake item from one locked snapshot."""

    with store.write() as transaction:
        before = transaction.snapshot()
        project = before.lifecycle.project
        authority = LocalIntakeAuthority(project.revision, project.host_epoch)
        decision = decide_proposal_creation(
            authority,
            project.revision,
            project.host_epoch,
            project_decision_snapshot(before, now),
            operation,
        )
        if isinstance(decision, DecisionFailure):
            return decision
        intake = decision.proposal
        transition = decision_models.TransitionReceipt(
            ActionId(f"inspect:proposal:{intake.proposal_id}"),
            None,
            "create-proposal",
            intake.urgency_evidence,
            now,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            project.revision + 1,
            stored_state.TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(intake.proposal_id),
            None,
            stored_state.TransitionHistoryAuthorizationKind.COORDINATOR,
            intake.source_task_id,
            None,
            "proposal-intake/v1",
            work_models.CanonicalJson(b"{}"),
        )
        return transaction.commit(ProposalCreationMutation(receipt, decision))


def _actor_for(
    snapshot: LedgerSnapshot,
    action: decision_models.TransitionAction,
    now: datetime,
) -> DecisionResult[decision_models.ActorAuthority]:
    capability = action.capability
    match capability.authorization:
        case decision_models.AuthorizationKind.COORDINATOR:
            return decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, capability.authorization, capability.coordinator_generation
            )
        case decision_models.AuthorizationKind.COORDINATION:
            authority = snapshot.coordination_authority
            if (
                authority is None
                or capability.lease_id != authority.lease_id
                or capability.coordinator_generation != authority.generation
                or authority.expires_at <= now
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "The supplied coordination authority is no longer current.",
                )
            return decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR,
                capability.authorization,
                capability.coordinator_generation,
                capability.lease_id,
            )
        case decision_models.AuthorizationKind.ATTEMPT:
            authority = capability.command_authority
            if (
                authority is None
                or capability.lease_id != authority.lease_id
                or capability.coordinator_generation != authority.generation
                or authority.expires_at <= now
                or authority not in snapshot.command_attempt_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "The supplied attempt authority is no longer current.",
                )
            return decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                capability.authorization,
                capability.coordinator_generation,
                capability.lease_id,
                (authority.attempt,),
                False,
            )
        case decision_models.AuthorizationKind.PREPARATION:
            authority = capability.preparation_authority
            if (
                authority is None
                or capability.lease_id != authority.lease_id
                or capability.coordinator_generation != authority.generation
                or authority.expires_at <= now
                or authority not in snapshot.command_preparation_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "The supplied preparation authority is no longer current.",
                )
            return decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                capability.authorization,
                capability.coordinator_generation,
                capability.lease_id,
                preparations=(authority.item,),
            )
        case _ as unreachable:
            assert_never(unreachable)


def execute(
    store: WorkStore,
    command: decision_models.NonCheckpointTransitionCommand,
    now: datetime,
    *,
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Validate, decide, and persist one lifecycle mutation under one write lock."""

    supplied = command.action
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before, now)
        actor = _actor_for(snapshot, supplied, now)
        if isinstance(actor, DecisionFailure):
            return actor
        if (failure := validate_supplied_action(snapshot, actor, supplied)) is not None:
            return failure
        if (failure := validate_transition_work_brief(before, command, transition_brief_identity)) is not None:
            return failure
        decision = decide(snapshot, command, now)
        if isinstance(decision, DecisionFailure):
            return decision
        return transaction.commit(project_transition_mutation(before, decision))


def execute_checkpoint_acceptance(
    store: WorkStore,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
    checkpoint_artifacts: CheckpointArtifacts,
    *,
    transition_brief_identity: WorkBriefIdentity | None = None,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Validate, decide, and persist checkpoint acceptance with its required artifacts."""

    supplied = command.action
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before, now)
        actor = _actor_for(snapshot, supplied, now)
        if isinstance(actor, DecisionFailure):
            return actor
        if (failure := validate_supplied_action(snapshot, actor, supplied)) is not None:
            return failure
        if (failure := validate_transition_work_brief(before, command, transition_brief_identity)) is not None:
            return failure
        decision = decide(snapshot, command, now)
        if isinstance(decision, DecisionFailure):
            return decision
        return transaction.commit(project_checkpoint_acceptance_mutation(before, decision, checkpoint_artifacts))
