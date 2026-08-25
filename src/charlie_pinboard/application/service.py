from dataclasses import replace
from datetime import datetime
from typing import Annotated, assert_never

import msgspec

from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.mutations import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    DependencyEditMutation,
    MutationReceipt,
    PlanningImpactMutation,
    PlanningMutationReceipt,
    PlanningResolutionMutation,
    ProposalCreationMutation,
    ReservationTaskUseMutation,
    ResourceDefinitionEditMutation,
    ResourceIntentMutation,
    ResourceMutation,
    ResourceRequirementEditMutation,
    expected_stored_state,
    project_transition_mutation,
)
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    CoordinationLeaseState,
    StoredCoordinationLease,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AcquireTaskUseAuthority,
    AttemptAuthorityOperation,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    CoordinationAuthorityOperation,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    ReleaseTaskUseAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RenewTaskUseAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    RevokeTaskUseAuthority,
    TaskUseAuthorityOperation,
    TransferAttemptAuthority,
    decide_attempt_authority,
    decide_coordination_authority,
    decide_task_use_authority,
)
from charlie_pinboard.domain.decisions import (
    Action,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
    TransitionReceipt,
    command_action,
    decide,
    rediscover_action,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import planning_impact_outcome, planning_resolution_outcome
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    LeaseId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AttemptState,
    CanonicalJson,
    CommandAttemptAuthority,
    LedgerSnapshot,
    MutationIntentState,
    MutationUseLease,
    ReservationState,
)
from charlie_pinboard.domain.planning_decisions import (
    InterruptedPlanningAttemptAuthority,
    LivePlanningAttemptAuthority,
    NoAttemptPlanningAuthority,
    PlanningTargetAuthority,
    RecordPlanningImpactOperation,
    ResolvePlanningObligationOperation,
    decide_planning_impact,
    decide_planning_obligation_operation,
)
from charlie_pinboard.domain.proposal_decisions import (
    CreateProposalOperation,
    SQLiteLocalIntakeAuthority,
    decide_proposal_creation,
)
from charlie_pinboard.domain.resource_decisions import (
    AbandonmentForm,
    AbandonMutationIntentInput,
    AdvanceResourceObservationInput,
    AssignReservationOperation,
    ClaimResourceOperation,
    PreserveResourceStateInput,
    ReallocateReservationOperation,
    ReconcileInterruptedObservationInput,
    RegisterMutationIntentInput,
    ReleaseReservationOperation,
    ReservationOperation,
    ResolveFencedIntentInput,
    ResourceIntentDecision,
    RevokeReservationOperation,
    decide_claim_resource,
    decide_reservation_operation,
)
from charlie_pinboard.domain.resource_decisions import (
    abandon_mutation_intent as decide_abandonment,
)
from charlie_pinboard.domain.resource_decisions import (
    advance_resource_observation as decide_resource_observation,
)
from charlie_pinboard.domain.resource_decisions import (
    preserve_resource_state as decide_preserve_resource_state,
)
from charlie_pinboard.domain.resource_decisions import (
    reconcile_interrupted_observation as decide_reconcile_interrupted_observation,
)
from charlie_pinboard.domain.resource_decisions import (
    register_mutation_intent as decide_mutation_intent,
)
from charlie_pinboard.domain.resource_decisions import (
    resolve_fenced_resource_intent as decide_resolve_fenced_resource_intent,
)
from charlie_pinboard.domain.resource_definition_decisions import (
    ResourceDefinitionEditOperation,
    ResourceDefinitionUnchanged,
    decide_resource_definition_edit,
)
from charlie_pinboard.domain.scope_decisions import (
    ItemScopeEditOperation,
    ReplaceDependenciesOperation,
    ReplaceResourceRequirementsOperation,
    decide_item_scope_edit,
)

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type NonNegativeInteger = Annotated[int, msgspec.Meta(ge=0)]
type PositiveInteger = Annotated[int, msgspec.Meta(ge=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]

ABANDON_MUTATION_INTENT_INPUT_SCHEMA = "abandon-mutation-intent/v1"


class AbandonMutationIntentHistoryInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    accepted_observation_digest: Sha256
    accepted_observation_generation: PositiveInteger
    deciding_task_id: NonEmptyString
    discovery_fingerprint: NonEmptyString
    form: AbandonmentForm
    intent_id: NonEmptyString
    locator: msgspec.Raw
    locator_schema: NonEmptyString
    observation_digest: Sha256
    observation_host_id: NonEmptyString
    observed_at: datetime
    prior_attempt_lease_generation: PositiveInteger
    prior_attempt_lease_id: NonEmptyString
    prior_task_id: NonEmptyString
    prior_task_use_generation: PositiveInteger
    prior_task_use_lease_id: NonEmptyString
    reason: NonEmptyString
    resource_instance_id: NonEmptyString
    resource_kind: NonEmptyString
    start_instance_subject_revision: NonNegativeInteger
    start_observation_digest: Sha256
    start_observation_generation: PositiveInteger


def change_coordination_authority(
    store: WorkStore,
    operation: CoordinationAuthorityOperation,
) -> TransitionReceipt | DecisionFailure:
    """Decide and persist one exact coordination-authority mutation."""

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        result = decide_coordination_authority(snapshot.coordination_lease, operation)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        after_authority = decision.after
        stored_after = StoredCoordinationLease(
            after_authority.lease_id,
            after_authority.task_id,
            after_authority.host_id,
            after_authority.generation,
            after_authority.acquired_at,
            after_authority.expires_at,
            CoordinationLeaseState(after_authority.state.value),
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
        transition = TransitionReceipt(
            ActionId(f"continue:coordination-authority:{after_authority.generation}"),
            None,
            outcome,
            None,
            decided_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId("ledger"),
            None,
            authorization,
            after_authority.task_id,
            after_authority.host_id,
            "coordination-authority/v1",
            CanonicalJson(b"{}"),
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
        AttemptLeaseStatus(lease.state.value),
    )


def change_attempt_authority(
    store: WorkStore,
    operation: AttemptAuthorityOperation,
) -> TransitionReceipt | DecisionFailure:
    """Decide and persist one exact attempt-authority mutation and all task-use fences."""

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
        result = decide_attempt_authority(
            retained,
            counter,
            snapshot.mutation_use_leases,
            operation,
            snapshot.coordination_lease,
            tuple(
                value.attempt_id for value in snapshot.mutation_intents if value.state == MutationIntentState.PLANNED
            ),
            live_attempt=(
                (attempt_id, attempt.item)
                if (attempt := snapshot.attempt(attempt_id)) is not None and attempt.state == AttemptState.ACTIVE
                else None
            ),
            transferable_attempt=(
                (attempt_id, attempt.item)
                if (attempt := snapshot.attempt(attempt_id)) is not None
                and attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
                else None
            ),
            project_host_epoch=snapshot.host_epoch,
            recovery_pending_attempts=tuple(
                value.attempt_id
                for value in snapshot.mutation_reservations
                if value.state == ReservationState.REVOKED_PENDING_RECOVERY
            ),
        )
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        after = decision.current_after
        transition = TransitionReceipt(
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
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(attempt_id),
            None,
            authorization,
            after.task_id,
            after.host_id,
            "attempt-authority/v1",
            CanonicalJson(b"{}"),
        )
        draft = AttemptAuthorityMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def _task_use_generation(value: MutationUseLease) -> int:
    return value.generation


def change_task_use_authority(
    store: WorkStore,
    operation: TaskUseAuthorityOperation,
) -> TransitionReceipt | DecisionFailure:
    """Decide and persist one exact task-use generation without changing its reservation."""

    match operation:
        case AcquireTaskUseAuthority(requested=requested, acquired_at=decided_at):
            reservation_id = requested.reservation_id
            outcome = "acquire-task-use-authority"
            authorization = TransitionHistoryAuthorizationKind.ATTEMPT
        case RenewTaskUseAuthority(current=current, renewed_at=decided_at):
            reservation_id = current.reservation_id
            outcome = "renew-task-use-authority"
            authorization = TransitionHistoryAuthorizationKind.ATTEMPT
        case ReleaseTaskUseAuthority(current=current, released_at=decided_at):
            reservation_id = current.reservation_id
            outcome = "release-task-use-authority"
            authorization = TransitionHistoryAuthorizationKind.ATTEMPT
        case RevokeTaskUseAuthority(current=current, revoked_at=decided_at):
            reservation_id = current.reservation_id
            outcome = "revoke-task-use-authority"
            authorization = TransitionHistoryAuthorizationKind.COORDINATION
        case _ as unreachable:
            assert_never(unreachable)
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        retained = max(
            (value for value in snapshot.mutation_use_leases if value.reservation_id == reservation_id),
            key=_task_use_generation,
            default=None,
        )
        reservation = next(
            (value for value in snapshot.mutation_reservations if value.reservation_id == reservation_id),
            None,
        )
        attempt = None if reservation is None else _retained_attempt_authority(before, reservation.attempt_id)
        result = decide_task_use_authority(
            retained,
            operation,
            reservation,
            attempt,
            snapshot.coordination_lease,
            tuple(
                str(value.reservation_id)
                for value in snapshot.mutation_intents
                if value.state == MutationIntentState.PLANNED
            ),
        )
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        after = decision.after
        transition = TransitionReceipt(
            ActionId(f"continue:task-use:{reservation_id}:{after.generation}"),
            None if reservation is None else reservation.item_id,
            outcome,
            None,
            decided_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(reservation_id),
            None,
            authorization,
            after.task_id,
            after.host_id,
            "task-use-authority/v1",
            CanonicalJson(b"{}"),
        )
        draft = ReservationTaskUseMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def create_proposal(
    store: WorkStore,
    operation: CreateProposalOperation,
    now: datetime,
) -> TransitionReceipt | DecisionFailure:
    """Persist one immutable proposal under authority selected from the locked local store."""

    with store.write() as transaction:
        before = transaction.snapshot()
        project = before.lifecycle.project
        authority = SQLiteLocalIntakeAuthority(project.revision, project.host_epoch)
        result = decide_proposal_creation(
            authority,
            project.revision,
            project.host_epoch,
            tuple(value.proposal_id for value in before.proposals.proposals),
            tuple(value.item_id for value in before.lifecycle.work_items),
            operation,
        )
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        intake = decision.proposal
        transition = TransitionReceipt(
            ActionId(f"inspect:proposal:{intake.proposal_id}"),
            None,
            "create-proposal",
            intake.urgency_evidence,
            now,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            project.revision + 1,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(intake.proposal_id),
            None,
            TransitionHistoryAuthorizationKind.COORDINATOR,
            intake.source_task_id,
            None,
            "proposal-intake/v1",
            CanonicalJson(b"{}"),
        )
        draft = ProposalCreationMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def edit_item_scope(
    store: WorkStore,
    operation: ItemScopeEditOperation,
) -> TransitionReceipt | DecisionFailure:
    """Replace one ordered item-scope component under exact coordination authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_item_scope_edit(project_decision_snapshot(before), operation)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        transition = TransitionReceipt(
            ActionId(f"continue:item-scope:{decision.item}:{decision.after_scope.revision}"),
            decision.item,
            "edit-item-scope",
            None,
            operation.changed_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(decision.item),
            None,
            TransitionHistoryAuthorizationKind.COORDINATION,
            operation.authority.task_id,
            operation.authority.host_id,
            "item-scope-edit/v1",
            CanonicalJson(b"{}"),
        )
        match operation:
            case ReplaceDependenciesOperation():
                draft = DependencyEditMutation(before, before, receipt, decision)
            case ReplaceResourceRequirementsOperation():
                draft = ResourceRequirementEditMutation(before, before, receipt, decision)
            case _ as unreachable:
                assert_never(unreachable)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def edit_resource_definition(
    store: WorkStore,
    operation: ResourceDefinitionEditOperation,
) -> TransitionReceipt | ResourceDefinitionUnchanged | DecisionFailure:
    """Create or edit one portable resource definition under exact coordination authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_resource_definition_edit(project_decision_snapshot(before), operation)
        match result:
            case DecisionFailure() | ResourceDefinitionUnchanged():
                return result
            case decision:
                pass
        transition = TransitionReceipt(
            ActionId(
                f"continue:resource-definition:{decision.after_definition.resource_id}:"
                f"{decision.definition_revision_after}"
            ),
            None,
            "edit-resource-definition",
            None,
            operation.changed_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(decision.after_definition.resource_id),
            None,
            TransitionHistoryAuthorizationKind.COORDINATION,
            operation.authority.task_id,
            operation.authority.host_id,
            "resource-definition-edit/v1",
            CanonicalJson(b"{}"),
        )
        draft = ResourceDefinitionEditMutation(decision, before, before, receipt)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def change_reservation(
    store: WorkStore,
    operation: ReservationOperation,
) -> TransitionReceipt | DecisionFailure:
    """Persist one exact reservation lifecycle decision and its current task-use effects."""

    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_reservation_operation(project_decision_snapshot(before), operation)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        match operation:
            case AssignReservationOperation(reservation_id=reservation_id):
                outcome = "assign-reservation"
            case ReleaseReservationOperation(reservation_id=reservation_id):
                outcome = "release-reservation"
            case ReallocateReservationOperation(replacement_id=reservation_id):
                outcome = "reallocate-reservation"
            case RevokeReservationOperation(reservation_id=reservation_id):
                outcome = "revoke-reservation"
            case _ as unreachable:
                assert_never(unreachable)
        transition = TransitionReceipt(
            ActionId(f"continue:reservation:{reservation_id}"),
            None,
            outcome,
            None,
            operation.changed_at,
        )
        reservation_authority = operation.authority
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(reservation_id),
            None,
            (
                TransitionHistoryAuthorizationKind.ATTEMPT
                if isinstance(reservation_authority, CommandAttemptAuthority)
                else TransitionHistoryAuthorizationKind.COORDINATION
            ),
            reservation_authority.task_id,
            reservation_authority.host_id,
            "reservation-authority/v1",
            CanonicalJson(b"{}"),
        )
        observation = operation.observation
        draft = ResourceMutation(
            decision,
            before,
            before,
            observation.locator_schema,
            observation.locator,
            observation.generation,
            observation.digest,
            observation.observed_at,
            receipt,
        )
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def claim_resource(
    store: WorkStore,
    operation: ClaimResourceOperation,
) -> TransitionReceipt | DecisionFailure:
    """Atomically assign an optional reservation and acquire its first task-use grant."""

    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_claim_resource(project_decision_snapshot(before), operation)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        transition = TransitionReceipt(
            ActionId(f"continue:resource-claim:{operation.requested_use_lease.reservation_id}"),
            operation.attempt_authority.item,
            "claim-resource",
            None,
            operation.acquired_at,
        )
        receipt = MutationReceipt(
            transition,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.CONTINUE,
            HistorySubjectId(operation.requested_use_lease.reservation_id),
            None,
            TransitionHistoryAuthorizationKind.ATTEMPT,
            operation.attempt_authority.task_id,
            operation.attempt_authority.host_id,
            "resource-claim/v1",
            CanonicalJson(b"{}"),
        )
        draft = ReservationTaskUseMutation(before, before, receipt, decision)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def _actor_for(
    snapshot: LedgerSnapshot,
    action: Action,
    now: datetime,
) -> ActorAuthority | DecisionFailure:
    match action.authorization:
        case AuthorizationKind.COORDINATOR:
            return ActorAuthority(Role.COORDINATOR, action.authorization, action.coordinator_generation)
        case AuthorizationKind.COORDINATION:
            authority = snapshot.coordination_authority
            if (
                authority is None
                or action.lease_id != authority.lease_id
                or action.coordinator_generation != authority.generation
                or authority.expires_at <= now
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "The supplied coordination authority is no longer current.",
                )
            return ActorAuthority(
                Role.COORDINATOR,
                action.authorization,
                action.coordinator_generation,
                action.lease_id,
            )
        case AuthorizationKind.ATTEMPT:
            authority = action.command_authority
            if (
                authority is None
                or action.lease_id != authority.lease_id
                or action.coordinator_generation != authority.generation
                or authority.expires_at <= now
                or authority not in snapshot.command_attempt_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "The supplied attempt authority is no longer current.",
                )
            return ActorAuthority(
                Role.WORKER,
                action.authorization,
                action.coordinator_generation,
                action.lease_id,
                (AttemptId(action.subject),),
                False,
            )
        case AuthorizationKind.OBSERVER:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                "Observer actions cannot mutate repository work.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def execute(
    store: WorkStore,
    command: TransitionCommand,
    now: datetime,
) -> TransitionReceipt | DecisionFailure:
    """Rediscover, decide, and persist one lifecycle mutation under one write lock."""

    supplied = command_action(command)
    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        result = _actor_for(snapshot, supplied, now)
        match result:
            case DecisionFailure():
                return result
            case actor:
                pass
        result = rediscover_action(snapshot, actor, supplied)
        match result:
            case DecisionFailure() as unavailable:
                semantic_result = decide(snapshot, command, now)
                match semantic_result:
                    case DecisionFailure(code=DecisionFailureCode.PLANNING_IMPACT_UNRESOLVED):
                        return semantic_result
                    case DecisionFailure() | Decision():
                        return unavailable
            case Action():
                pass
        result = decide(snapshot, command, now)
        match result:
            case DecisionFailure():
                return result
            case decision:
                return transaction.commit(project_transition_mutation(before, decision))


def record_planning_impact(
    store: WorkStore,
    operation: RecordPlanningImpactOperation,
) -> PlanningMutationReceipt | DecisionFailure:
    """Record one validated planning impact under its exact live attempt authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        result = decide_planning_impact(snapshot, operation)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        authority = decision.source_authority
        authorization = (
            TransitionHistoryAuthorizationKind.ATTEMPT
            if isinstance(authority, CommandAttemptAuthority)
            else TransitionHistoryAuthorizationKind.COORDINATION
        )
        impact = decision.impact
        result = planning_impact_outcome(impact)
        match result:
            case DecisionFailure():
                return result
            case outcome:
                pass
        receipt = PlanningMutationReceipt(
            ActionId(f"inspect:{impact.impact_id}"),
            operation.recorded_at,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(impact.source_item),
            None,
            authorization,
            authority.task_id,
            authority.host_id,
            outcome.outcome_schema,
            CanonicalJson(outcome.payload),
        )
        draft = PlanningImpactMutation(decision, before, before, receipt)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def resolve_planning_obligation(
    store: WorkStore,
    operation: ResolvePlanningObligationOperation,
) -> PlanningMutationReceipt | DecisionFailure:
    """Resolve one current planning obligation under exact coordination authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        item = snapshot.item(operation.target)
        if item is None:
            return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{operation.target}' does not exist.")
        locked_authority: PlanningTargetAuthority
        if item.attempt is None:
            locked_authority = NoAttemptPlanningAuthority(item.item)
        else:
            live = next(
                (
                    value
                    for value in snapshot.command_attempt_authorities
                    if value.attempt == item.attempt and value.expires_at > operation.resolved_at
                ),
                None,
            )
            if live is not None:
                locked_authority = LivePlanningAttemptAuthority(live)
            else:
                projected = project_inactive_attempt_authority(before, item.attempt, operation.resolved_at)
                if isinstance(projected, DecisionFailure):
                    return projected
                locked_authority = InterruptedPlanningAttemptAuthority(projected)
        if operation.target_authority != locked_authority:
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                "Planning resolution authority changed before the operation was decided.",
            )
        selected_operation = replace(operation, target_authority=locked_authority)
        result = decide_planning_obligation_operation(
            snapshot,
            selected_operation,
            expected_target_authority=locked_authority,
        )
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        result = planning_resolution_outcome(decision.impact, selected_operation.target)
        match result:
            case DecisionFailure():
                return result
            case outcome:
                pass
        receipt = PlanningMutationReceipt(
            ActionId(f"inspect:{selected_operation.impact_id}:{selected_operation.target}"),
            selected_operation.resolved_at,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(selected_operation.target),
            None,
            TransitionHistoryAuthorizationKind.COORDINATION,
            selected_operation.coordination_authority.task_id,
            selected_operation.coordination_authority.host_id,
            outcome.outcome_schema,
            CanonicalJson(outcome.payload),
        )
        draft = PlanningResolutionMutation(
            decision,
            selected_operation.target,
            before,
            before,
            receipt,
            selected_operation.target_authority,
        )
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def _resource_intent_mutation(
    before: StoredWorkState,
    decision: ResourceIntentDecision,
    *,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    decided_at: datetime,
    input_schema: str,
    input_payload: CanonicalJson,
    evidence: str,
    authorization: TransitionHistoryAuthorizationKind = TransitionHistoryAuthorizationKind.ATTEMPT,
) -> ResourceIntentMutation:
    intent = decision.intent_change.after
    item = project_decision_snapshot(before).item_for_attempt(intent.attempt_id)
    transition = TransitionReceipt(
        ActionId(f"inspect:{decision.kind.value}:{intent.intent_id}"),
        None if item is None else item.item,
        decision.kind.value,
        evidence,
        decided_at,
    )
    receipt = MutationReceipt(
        transition,
        HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
        before.lifecycle.project.revision + 1,
        TransitionHistoryActionKind.INSPECT,
        HistorySubjectId(intent.intent_id),
        None,
        authorization,
        actor_task_id,
        actor_host_id,
        input_schema,
        input_payload,
    )
    draft = ResourceIntentMutation(decision, before, before, receipt)
    return replace(draft, after=expected_stored_state(draft))


def register_mutation_intent(
    store: WorkStore,
    value: RegisterMutationIntentInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_mutation_intent(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=decision.intent_change.after.task_id,
            actor_host_id=decision.intent_change.after.host_id,
            decided_at=value.recorded_at,
            input_schema=value.policy_schema,
            input_payload=value.policy,
            evidence=value.policy_digest,
        )
        return transaction.commit(mutation)


def advance_resource_observation(
    store: WorkStore,
    value: AdvanceResourceObservationInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_resource_observation(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=decision.intent_change.after.task_id,
            actor_host_id=decision.intent_change.after.host_id,
            decided_at=value.resolved_at,
            input_schema=value.evidence_schema,
            input_payload=value.evidence,
            evidence=value.evidence_digest,
        )
        return transaction.commit(mutation)


def abandon_mutation_intent(
    store: WorkStore,
    value: AbandonMutationIntentInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_abandonment(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        intent = decision.intent_change.after
        capability = value.intent.resource
        history_input = AbandonMutationIntentHistoryInput(
            capability.locator_observation_digest,
            capability.locator_observation_generation,
            str(value.attempt_authority.task_id),
            value.observation.discovery_fingerprint,
            value.form,
            str(intent.intent_id),
            msgspec.Raw(value.observation.locator),
            value.observation.locator_schema,
            value.observation.digest,
            str(value.observation.host_id),
            value.observation.observed_at,
            intent.attempt_lease_generation,
            str(intent.attempt_lease_id),
            str(intent.task_id),
            intent.resource_use_generation,
            str(intent.resource_use_lease_id),
            value.reason,
            str(value.observation.instance_id),
            value.observation.resource_kind,
            intent.start_instance_subject_revision,
            intent.start_observation_digest,
            intent.start_observation_generation,
        )
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=value.attempt_authority.task_id,
            actor_host_id=value.attempt_authority.host_id,
            decided_at=value.decided_at,
            input_schema=ABANDON_MUTATION_INTENT_INPUT_SCHEMA,
            input_payload=CanonicalJson(msgspec.json.encode(history_input, order="sorted")),
            evidence=value.reason,
        )
        return transaction.commit(mutation)


def reconcile_interrupted_observation(
    store: WorkStore,
    value: ReconcileInterruptedObservationInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_reconcile_interrupted_observation(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=value.attempt_authority.task_id,
            actor_host_id=value.attempt_authority.host_id,
            decided_at=value.resolved_at,
            input_schema=value.evidence_schema,
            input_payload=value.evidence,
            evidence=value.evidence_digest,
        )
        return transaction.commit(mutation)


def preserve_resource_state(
    store: WorkStore,
    value: PreserveResourceStateInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_preserve_resource_state(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=value.coordination_authority.task_id,
            actor_host_id=value.coordination_authority.host_id,
            decided_at=value.resolved_at,
            input_schema=value.evidence_schema or "preserve-resource-state/v1",
            input_payload=value.evidence or CanonicalJson(b"{}"),
            evidence=value.reason,
            authorization=TransitionHistoryAuthorizationKind.COORDINATION,
        )
        return transaction.commit(mutation)


def resolve_fenced_resource_intent(
    store: WorkStore,
    value: ResolveFencedIntentInput,
) -> TransitionReceipt | DecisionFailure:
    with store.write() as transaction:
        before = transaction.snapshot()
        result = decide_resolve_fenced_resource_intent(project_decision_snapshot(before), value)
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        mutation = _resource_intent_mutation(
            before,
            decision,
            actor_task_id=value.coordination_authority.task_id,
            actor_host_id=value.coordination_authority.host_id,
            decided_at=value.resolved_at,
            input_schema=value.evidence_schema or "resolve-fenced-resource-intent/v1",
            input_payload=value.evidence or CanonicalJson(b"{}"),
            evidence=value.reason,
            authorization=TransitionHistoryAuthorizationKind.COORDINATION,
        )
        return transaction.commit(mutation)
