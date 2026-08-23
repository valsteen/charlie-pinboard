from dataclasses import replace
from datetime import datetime
from typing import Annotated, assert_never

import msgspec

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.mutations import (
    MutationReceipt,
    PlanningImpactMutation,
    PlanningMutationReceipt,
    PlanningResolutionMutation,
    ResourceIntentMutation,
    expected_stored_state,
)
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
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
    ItemId,
    PlanningImpactId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    CanonicalJson,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    LedgerSnapshot,
    PlanningDisposition,
    PlanningImpact,
)
from charlie_pinboard.domain.planning_decisions import decide_planning_resolution, validate_planning_impact
from charlie_pinboard.domain.resource_decisions import (
    AbandonmentForm,
    AbandonMutationIntentInput,
    AdvanceResourceObservationInput,
    RegisterMutationIntentInput,
    ResourceIntentDecision,
)
from charlie_pinboard.domain.resource_decisions import (
    abandon_mutation_intent as decide_abandonment,
)
from charlie_pinboard.domain.resource_decisions import (
    advance_resource_observation as decide_resource_observation,
)
from charlie_pinboard.domain.resource_decisions import (
    register_mutation_intent as decide_mutation_intent,
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
        snapshot = project_decision_snapshot(transaction.snapshot())
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
                return transaction.commit(decision)


def record_planning_impact(
    store: WorkStore,
    authority: CommandAttemptAuthority,
    impact: PlanningImpact,
    now: datetime,
) -> PlanningMutationReceipt | DecisionFailure:
    """Record one validated planning impact under its exact live attempt authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        if (failure := validate_planning_impact(snapshot, impact)) is not None:
            return failure
        if (
            authority not in snapshot.command_attempt_authorities
            or authority.expires_at <= now
            or impact.source_attempt != authority.attempt
            or impact.source_item != authority.item
        ):
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                "Planning impact recording requires the exact live source-attempt authority.",
            )
        result = planning_impact_outcome(impact)
        match result:
            case DecisionFailure():
                return result
            case outcome:
                pass
        receipt = PlanningMutationReceipt(
            ActionId(f"inspect:{impact.impact_id}"),
            now,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(impact.source_item),
            None,
            TransitionHistoryAuthorizationKind.ATTEMPT,
            authority.task_id,
            authority.host_id,
            outcome.outcome_schema,
            CanonicalJson(outcome.payload),
        )
        draft = PlanningImpactMutation(impact, before, before, receipt)
        mutation = replace(draft, after=expected_stored_state(draft))
        return transaction.commit(mutation)


def resolve_planning_obligation(
    store: WorkStore,
    authority: CoordinationCommandAuthority,
    impact_id: PlanningImpactId,
    target: ItemId,
    disposition: PlanningDisposition,
    *,
    reason: str,
    resulting_scope_revision: int | None = None,
    resulting_scope_digest: str | None = None,
    replacements: tuple[ItemId, ...] = (),
    outcome_evidence: str | None = None,
    now: datetime,
) -> PlanningMutationReceipt | DecisionFailure:
    """Resolve one current planning obligation under exact coordination authority."""

    with store.write() as transaction:
        before = transaction.snapshot()
        snapshot = project_decision_snapshot(before)
        if snapshot.coordination_authority != authority or authority.expires_at <= now:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "Planning resolution requires the exact live coordination authority.",
            )
        impact = next((value for value in snapshot.planning_impacts if value.impact_id == impact_id), None)
        if impact is None:
            return DecisionFailure(
                DecisionFailureCode.PLANNING_IMPACT_INVALID,
                f"Planning impact '{impact_id}' does not exist.",
            )
        result = decide_planning_resolution(
            snapshot,
            impact,
            target,
            disposition,
            reason=reason,
            resulting_scope_revision=resulting_scope_revision,
            resulting_scope_digest=resulting_scope_digest,
            replacements=replacements,
            outcome_evidence=outcome_evidence,
        )
        match result:
            case DecisionFailure():
                return result
            case decision:
                pass
        result = planning_resolution_outcome(decision.impact, target)
        match result:
            case DecisionFailure():
                return result
            case outcome:
                pass
        receipt = PlanningMutationReceipt(
            ActionId(f"inspect:{impact_id}:{target}"),
            now,
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
            before.lifecycle.project.revision + 1,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId(target),
            None,
            TransitionHistoryAuthorizationKind.COORDINATION,
            authority.task_id,
            authority.host_id,
            outcome.outcome_schema,
            CanonicalJson(outcome.payload),
        )
        draft = PlanningResolutionMutation(decision, target, before, before, receipt)
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
        TransitionHistoryAuthorizationKind.ATTEMPT,
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
