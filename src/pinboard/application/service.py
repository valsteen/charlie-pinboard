from collections.abc import Callable
from datetime import datetime
from typing import assert_never

from pinboard.application import stored_state
from pinboard.application.artifacts import CheckpointArtifacts
from pinboard.application.decision_projection import (
    project_decision_snapshot,
)
from pinboard.application.errors import MutationContractError
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
)
from pinboard.application.mutations import project_transition_mutation
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_coordination_authority,
)
from pinboard.domain.authority_models import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AttemptAuthorityOperation,
    AttemptLeaseAuthority,
    CoordinationAuthorityOperation,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    TransferAttemptAuthority,
)
from pinboard.domain.decisions import decide, rediscover_action
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
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

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
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
        snapshot = project_decision_snapshot(before)
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


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Persist immutable proposal facts and their visible intake item from one locked snapshot."""

    with store.write() as transaction:
        before = transaction.snapshot()
        project = before.lifecycle.project
        authority = LocalIntakeAuthority(project.revision, project.host_epoch)
        decision = decide_proposal_creation(
            authority,
            project.revision,
            project.host_epoch,
            project_decision_snapshot(before),
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
    action: decision_models.Action,
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
        case decision_models.AuthorizationKind.OBSERVER:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                "Observer actions cannot mutate repository work.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def execute(
    store: WorkStore,
    command: decision_models.TransitionCommand,
    now: datetime,
    transition_guard: Callable[
        [stored_state.StoredWorkState, decision_models.TransitionCommand], DecisionFailure | None
    ]
    | None = None,
    *,
    checkpoint_artifacts: CheckpointArtifacts | None = None,
) -> DecisionResult[decision_models.TransitionReceipt]:
    """Rediscover, decide, and persist one lifecycle mutation under one write lock."""

    supplied = command.action
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = _actor_for(snapshot, supplied, now)
        if isinstance(actor, DecisionFailure):
            return actor
        rediscovered = rediscover_action(snapshot, actor, supplied)
        if isinstance(rediscovered, DecisionFailure):
            return rediscovered
        if transition_guard is not None and (failure := transition_guard(before, command)) is not None:
            return failure
        decision = decide(snapshot, command, now)
        if isinstance(decision, DecisionFailure):
            return decision
        try:
            mutation = project_transition_mutation(before, decision, checkpoint_artifacts)
        except MutationContractError as error:
            return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error))
        return transaction.commit(mutation)
