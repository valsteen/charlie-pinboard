from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
)
from charlie_pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
)
from charlie_pinboard.application.mutations import (
    expected_stored_state,
    project_transition_mutation,
)
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    StoredCoordinationLease,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.authority_decisions import (
    decide_attempt_authority,
    decide_coordination_authority,
)
from charlie_pinboard.domain.authority_models import (
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
from charlie_pinboard.domain.decisions import decide, rediscover_action
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    LeaseId,
    TaskId,
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.proposal_decisions import decide_proposal_creation
from charlie_pinboard.domain.proposal_models import (
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
        stored_after = StoredCoordinationLease(
            after_authority.lease_id,
            after_authority.task_id,
            after_authority.host_id,
            after_authority.generation,
            after_authority.acquired_at,
            after_authority.expires_at,
            after_authority.state,
        )
        match operation:
            case AcquireCoordinationAuthority(acquired_at=decided_at):
                outcome = "acquire-coordination-authority"
                authorization = TransitionHistoryAuthorizationKind.COORDINATOR
            case RenewCoordinationAuthority(renewed_at=decided_at):
                outcome = "renew-coordination-authority"
                authorization = TransitionHistoryAuthorizationKind.COORDINATION
            case ReleaseCoordinationAuthority(released_at=decided_at):
                outcome = "release-coordination-authority"
                authorization = TransitionHistoryAuthorizationKind.COORDINATION
            case RevokeCoordinationAuthority(revoked_at=decided_at):
                outcome = "revoke-coordination-authority"
                authorization = TransitionHistoryAuthorizationKind.COORDINATOR
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
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId("ledger"),
            None,
            authorization,
            after_authority.task_id,
            after_authority.host_id,
            "coordination-authority/v1",
            work_models.CanonicalJson(b"{}"),
        )
        supplied_after = replace(
            before,
            authority=replace(before.authority, coordination=stored_after),
        )
        draft = CoordinationAuthorityMutation(before, supplied_after, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def _retained_attempt_authority(
    state: StoredWorkState,
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
            authorization = TransitionHistoryAuthorizationKind.COORDINATOR
        case TransferAttemptAuthority(current=current, acquired_at=decided_at):
            attempt_id = current.attempt
            outcome = "transfer-attempt-authority"
            authorization = TransitionHistoryAuthorizationKind.COORDINATION
        case RenewAttemptAuthority(current=current, renewed_at=decided_at):
            attempt_id = current.attempt
            outcome = "renew-attempt-authority"
            authorization = TransitionHistoryAuthorizationKind.ATTEMPT
        case ReleaseAttemptAuthority(current=current, released_at=decided_at):
            attempt_id = current.attempt
            outcome = "release-attempt-authority"
            authorization = TransitionHistoryAuthorizationKind.ATTEMPT
        case RevokeAttemptAuthority(attempt=attempt_id, revoked_at=decided_at):
            outcome = "revoke-attempt-authority"
            authorization = TransitionHistoryAuthorizationKind.COORDINATION
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
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(attempt_id),
            None,
            authorization,
            after.task_id,
            after.host_id,
            "attempt-authority/v1",
            work_models.CanonicalJson(b"{}"),
        )
        draft = AttemptAuthorityMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


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
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(intake.proposal_id),
            None,
            TransitionHistoryAuthorizationKind.COORDINATOR,
            intake.source_task_id,
            None,
            "proposal-intake/v1",
            work_models.CanonicalJson(b"{}"),
        )
        draft = ProposalCreationMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


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
    transition_guard: Callable[[StoredWorkState, decision_models.TransitionCommand], DecisionFailure | None]
    | None = None,
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
        return transaction.commit(project_transition_mutation(before, decision))
