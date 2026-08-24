from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.stored_state import (
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    CoordinationLeaseState,
    HistoryRecords,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    LifecycleRecords,
    OriginKind,
    PlanningObligationState,
    PlanningRecords,
    ProposalDisposition,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRelation,
    ResourceInstanceLocator,
    ResourceMutationIntent,
    StoredAttempt,
    StoredAttemptLease,
    StoredFocus,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredReservationCounter,
    StoredResourceDefinition,
    StoredResourceInstance,
    StoredResourceReservation,
    StoredResourceUseLease,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import (
    AttemptAuthorityDecision,
    CoordinationAuthorityDecision,
    TaskUseAuthorityDecision,
)
from charlie_pinboard.domain.decisions import ActionKind, AuthorizationKind, Decision, TransitionReceipt
from charlie_pinboard.domain.errors import DecisionFailure
from charlie_pinboard.domain.history import (
    HistoryOutcome,
    encode_transition_receipt_outcome,
    planning_impact_outcome,
    planning_resolution_outcome,
)
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    CanonicalJson,
    MutationIntent,
    MutationReservation,
    MutationUseLease,
    PlanningDisposition,
    ReservationState,
    ResourceObservation,
    ResourceReservation,
    ResourceReservationCounter,
    UseLeaseGenerationKind,
    UseLeaseState,
)
from charlie_pinboard.domain.planning_decisions import (
    PlanningImpactDecision,
    PlanningResolutionDecision,
    PlanningTargetAuthority,
)
from charlie_pinboard.domain.proposal_decisions import ProposalCreationDecision
from charlie_pinboard.domain.resource_decisions import ClaimResourceDecision, ResourceDecision, ResourceIntentDecision
from charlie_pinboard.domain.resource_definition_decisions import ResourceDefinitionEditDecision
from charlie_pinboard.domain.scope_decisions import ItemScopeEditDecision


class MutationContractError(ValueError):
    pass


def _work_item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _scope_revision_key(value: ItemScopeRevision) -> tuple[str, int]:
    return str(value.item_id), value.revision


def _dependency_key(value: ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _requirement_key(value: ItemResourceRequirement) -> tuple[str, int]:
    return str(value.item_id), value.position


def _resource_definition_key(value: StoredResourceDefinition) -> str:
    return str(value.resource_id)


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _attempt_generation_key(value: AttemptLeaseGeneration) -> tuple[str, int]:
    return str(value.attempt_id), value.generation


def _attempt_counter_key(value: AttemptLeaseCounter) -> str:
    return str(value.attempt_id)


def _stored_attempt_lease_key(value: StoredAttemptLease) -> str:
    return str(value.attempt_id)


def _reservation_counter_key(value: StoredReservationCounter) -> str:
    return str(value.instance_id)


def _reservation_key(value: StoredResourceReservation) -> str:
    return str(value.reservation_id)


def _use_lease_key(value: StoredResourceUseLease) -> tuple[str, int]:
    return str(value.reservation_id), value.generation


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Stored-history identity for an ordinary transition receipt."""

    transition: TransitionReceipt
    history_id: HistoryId
    project_revision: int
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: CanonicalJson


@dataclass(frozen=True, slots=True)
class PlanningMutationReceipt:
    """Stored-history identity without an independently claimable generic outcome."""

    action_id: ActionId
    decided_at: datetime
    history_id: HistoryId
    project_revision: int
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: CanonicalJson


type CommitReceipt = TransitionReceipt | PlanningMutationReceipt


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class TransitionMutation:
    """Persists one accepted closed lifecycle decision as an exact relational delta."""

    decision: Decision
    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class DependencyEditMutation:
    """Persists an authorized dependency edit without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: ItemScopeEditDecision


@dataclass(frozen=True, slots=True)
class ResourceRequirementEditMutation:
    """Persists an authorized resource-requirement edit without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: ItemScopeEditDecision


@dataclass(frozen=True, slots=True)
class ResourceDefinitionEditMutation:
    """Persists one exact portable resource-definition decision."""

    decision: ResourceDefinitionEditDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Persists an authorized coordination change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: CoordinationAuthorityDecision


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: AttemptAuthorityDecision


@dataclass(frozen=True, slots=True)
class ReservationTaskUseMutation:
    """Persists an authorized reservation or task-use change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: TaskUseAuthorityDecision | ClaimResourceDecision


@dataclass(frozen=True, slots=True)
class PlanningImpactMutation:
    decision: PlanningImpactDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: PlanningMutationReceipt


@dataclass(frozen=True, slots=True)
class PlanningResolutionMutation:
    decision: PlanningResolutionDecision
    target: ItemId
    before: StoredWorkState
    after: StoredWorkState
    receipt: PlanningMutationReceipt
    target_authority: PlanningTargetAuthority


@dataclass(frozen=True, slots=True)
class ResourceMutation:
    decision: ResourceDecision
    before: StoredWorkState
    after: StoredWorkState
    locator_schema: str
    locator: CanonicalJson
    observation_generation: int
    observation_digest: str
    observed_at: datetime
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class ResourceIntentMutation:
    decision: ResourceIntentDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt


type TransitionReceiptMutation = (
    TransitionMutation
    | ProposalCreationMutation
    | DependencyEditMutation
    | ResourceRequirementEditMutation
    | ResourceDefinitionEditMutation
    | CoordinationAuthorityMutation
    | AttemptAuthorityMutation
    | ReservationTaskUseMutation
    | ResourceMutation
    | ResourceIntentMutation
)

type PlanningMutation = PlanningImpactMutation | PlanningResolutionMutation

type AcceptedMutation = TransitionReceiptMutation | PlanningMutation


type StoredStateMutation = TransitionReceiptMutation | PlanningMutation


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case PlanningImpactMutation(decision=decision):
            result = planning_impact_outcome(decision.impact)
        case PlanningResolutionMutation(decision=decision, target=target):
            result = planning_resolution_outcome(decision.impact, target)
        case TransitionMutation(decision=decision):
            checkpoint = None
            candidate = None
            if decision.checkpoint_acceptance_change is not None:
                checkpoint = str(decision.checkpoint_acceptance_change.checkpoint)
                candidate = str(decision.checkpoint_acceptance_change.candidate)
            if decision.attempt_change is not None and decision.attempt_change.protected_candidate_after is not None:
                candidate = str(decision.attempt_change.protected_candidate_after)
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(
                    evidence=decision.receipt.evidence,
                    outcome=decision.receipt.outcome,
                    candidate=candidate,
                    checkpoint=checkpoint,
                ),
            )
        case (
            TransitionMutation()
            | ProposalCreationMutation()
            | DependencyEditMutation()
            | ResourceRequirementEditMutation()
            | ResourceDefinitionEditMutation()
            | CoordinationAuthorityMutation()
            | AttemptAuthorityMutation()
            | ReservationTaskUseMutation()
            | ResourceMutation()
            | ResourceIntentMutation()
        ):
            transition = mutation.receipt.transition
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(evidence=transition.evidence, outcome=transition.outcome),
            )
        case _ as unreachable:
            assert_never(unreachable)
    match result:
        case DecisionFailure(message=message):
            raise MutationContractError(message)
        case HistoryOutcome():
            return result


def _stored_receipt(mutation: StoredStateMutation) -> StoredTransitionReceipt:
    outcome = _history_outcome(mutation)
    match mutation:
        case PlanningImpactMutation(receipt=receipt) | PlanningResolutionMutation(receipt=receipt):
            action_id = receipt.action_id
            decided_at = receipt.decided_at
        case (
            TransitionMutation(receipt=receipt)
            | ProposalCreationMutation(receipt=receipt)
            | DependencyEditMutation(receipt=receipt)
            | ResourceRequirementEditMutation(receipt=receipt)
            | ResourceDefinitionEditMutation(receipt=receipt)
            | CoordinationAuthorityMutation(receipt=receipt)
            | AttemptAuthorityMutation(receipt=receipt)
            | ReservationTaskUseMutation(receipt=receipt)
            | ResourceMutation(receipt=receipt)
            | ResourceIntentMutation(receipt=receipt)
        ):
            action_id = receipt.transition.action_id
            decided_at = receipt.transition.decided_at
        case _ as unreachable:
            assert_never(unreachable)
    return StoredTransitionReceipt(
        receipt.history_id,
        receipt.project_revision,
        action_id,
        receipt.action_kind,
        receipt.subject_id,
        receipt.artifact_ref_id,
        receipt.authorization,
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.input_schema,
        receipt.input_payload,
        outcome.outcome_schema,
        CanonicalJson(outcome.payload),
        decided_at,
    )


def _common_after(mutation: StoredStateMutation) -> StoredWorkState:
    before = mutation.before
    project = before.lifecycle.project
    receipt = mutation.receipt
    next_history_id = 1 + max((int(value.history_id) for value in before.history.receipts), default=0)
    if int(receipt.history_id) != next_history_id or receipt.project_revision != project.revision + 1:
        raise MutationContractError("The accepted stored receipt does not identify the mutation exactly.")
    stored_receipt = _stored_receipt(mutation)
    return replace(
        before,
        lifecycle=replace(
            before.lifecycle,
            project=replace(project, revision=project.revision + 1, updated_at=stored_receipt.committed_at),
        ),
        history=HistoryRecords((*before.history.receipts, stored_receipt)),
    )


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _proposal_evidence_key(value: ProposalEvidence) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_freshness_key(value: ProposalFreshness) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_creation_after(
    mutation: ProposalCreationMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    intake = decision.proposal
    proposal = StoredProposal(
        intake.proposal_id,
        OriginKind.NATIVE,
        intake.created_at,
        mutation.receipt.transition.decided_at,
        intake.source_task_id,
        intake.user_label,
        intake.trigger,
        intake.why_it_matters,
        ProposalRelation(intake.relation.value),
        intake.relation_item,
        intake.effect,
        intake.unlock,
        intake.urgency_evidence,
        None,
        None,
        None,
        mutation.receipt.project_revision,
        None,
        None,
    )
    if any(value.proposal_id == intake.proposal_id for value in common.proposals.proposals):
        raise MutationContractError("Proposal creation identity already exists.")
    return replace(
        common,
        proposals=replace(
            common.proposals,
            proposals=tuple(
                sorted(
                    (*common.proposals.proposals, proposal),
                    key=_proposal_key,
                )
            ),
            evidence=tuple(
                sorted(
                    (
                        *common.proposals.evidence,
                        *(
                            ProposalEvidence(intake.proposal_id, position, value)
                            for position, value in enumerate(decision.evidence)
                        ),
                    ),
                    key=_proposal_evidence_key,
                )
            ),
            freshness=tuple(
                sorted(
                    (
                        *common.proposals.freshness,
                        *(
                            ProposalFreshness(intake.proposal_id, position, value)
                            for position, value in enumerate(decision.freshness)
                        ),
                    ),
                    key=_proposal_freshness_key,
                )
            ),
        ),
    )


def _scope_edit_after(
    common: StoredWorkState,
    decision: ItemScopeEditDecision,
    decided_at: datetime,
    *,
    dependencies: bool,
) -> StoredWorkState:
    items = list(common.lifecycle.work_items)
    index = next((position for position, value in enumerate(items) if value.item_id == decision.item), None)
    if index is None or (items[index].scope_revision, items[index].scope_digest) != (
        decision.before_scope.revision,
        decision.before_scope.digest,
    ):
        raise MutationContractError("The item scope edit is stale.")
    items[index] = replace(
        items[index],
        scope_revision=decision.after_scope.revision,
        scope_digest=decision.after_scope.digest,
        subject_revision=common.lifecycle.project.revision,
        origin_updated_at=decided_at,
        updated_at=decided_at,
    )
    lifecycle = replace(
        common.lifecycle,
        work_items=tuple(items),
        scope_revisions=(
            *common.lifecycle.scope_revisions,
            ItemScopeRevision(
                decision.item,
                decision.after_scope.revision,
                decision.after_scope.digest,
                common.lifecycle.project.revision,
                decided_at,
            ),
        ),
    )
    resources = common.resources
    if dependencies:
        lifecycle = replace(
            lifecycle,
            dependencies=(
                *(value for value in common.lifecycle.dependencies if value.item_id != decision.item),
                *(
                    ItemDependency(decision.item, value.dependency_id, value.position)
                    for value in decision.dependencies
                ),
            ),
        )
    else:
        resources = replace(
            resources,
            requirements=(
                *(value for value in resources.requirements if value.item_id != decision.item),
                *(
                    ItemResourceRequirement(decision.item, value.resource_id, value.position)
                    for value in decision.resource_requirements
                ),
            ),
        )
    return replace(common, lifecycle=lifecycle, resources=resources)


def _transition_item_state(decision: Decision) -> StoredWorkItemState:
    if decision.action.kind == ActionKind.COMPLETE:
        return StoredWorkItemState.DONE
    try:
        state = StoredWorkItemState(decision.receipt.outcome)
    except ValueError as error:
        raise MutationContractError("A terminal transition must name its exact stored item state.") from error
    if state not in {StoredWorkItemState.DONE, StoredWorkItemState.SUPERSEDED, StoredWorkItemState.DROPPED}:
        raise MutationContractError("A terminal transition must name a terminal stored item state.")
    return state


def _transition_item_after(
    mutation: TransitionMutation,
    lifecycle: LifecycleRecords,
) -> LifecycleRecords:
    decision = mutation.decision
    change = decision.item_change
    if change is None:
        return lifecycle
    revision = mutation.receipt.project_revision
    now = decision.receipt.decided_at
    items = list(lifecycle.work_items)
    if change.before is None:
        proposal_change = decision.proposal_change
        accepted = None if proposal_change is None else proposal_change.accepted_item
        if accepted is None or change.after != accepted.state:
            raise MutationContractError("Item creation requires one complete accepted-proposal decision.")
        items.append(
            StoredWorkItem(
                accepted.item,
                OriginKind.NATIVE,
                accepted.user_label,
                StoredWorkItemState(accepted.state.value),
                accepted.timing,
                accepted.source,
                accepted.trigger,
                accepted.why_it_matters,
                accepted.effect,
                accepted.unlock,
                None,
                accepted.next_action,
                accepted.notes,
                1,
                accepted.scope_digest,
                revision,
                now,
                now,
                now,
                now,
            )
        )
        return replace(
            lifecycle,
            work_items=tuple(sorted(items, key=_work_item_key)),
            scope_revisions=tuple(
                sorted(
                    (
                        *lifecycle.scope_revisions,
                        ItemScopeRevision(accepted.item, 1, accepted.scope_digest, revision, now),
                    ),
                    key=_scope_revision_key,
                )
            ),
            dependencies=tuple(
                sorted(
                    (
                        *lifecycle.dependencies,
                        *(
                            ItemDependency(accepted.item, dependency, position)
                            for position, dependency in enumerate(accepted.dependencies)
                        ),
                    ),
                    key=_dependency_key,
                )
            ),
        )
    index = next((position for position, item in enumerate(items) if item.item_id == change.item), None)
    if index is None or items[index].state.value != change.before.value:
        raise MutationContractError("The transition item change is stale.")
    terminal = change.after is None
    items[index] = replace(
        items[index],
        state=_transition_item_state(decision) if change.after is None else StoredWorkItemState(change.after.value),
        outcome_evidence=change.outcome_evidence if terminal else None,
        subject_revision=revision,
        origin_updated_at=now if items[index].origin == OriginKind.NATIVE else items[index].origin_updated_at,
        updated_at=now,
    )
    return replace(lifecycle, work_items=tuple(items))


def _transition_attempt_after(
    mutation: TransitionMutation,
    lifecycle: LifecycleRecords,
) -> LifecycleRecords:
    change = mutation.decision.attempt_change
    if change is None:
        return lifecycle
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    attempts = list(lifecycle.attempts)
    if change.before is None:
        if (
            change.after is None
            or change.brief_artifact_ref_id is None
            or change.branch is None
            or change.base_revision is None
            or change.owner is None
            or mutation.decision.item_change is None
        ):
            raise MutationContractError("Attempt creation facts are incomplete.")
        item = next(
            (value for value in lifecycle.work_items if value.item_id == mutation.decision.item_change.item),
            None,
        )
        if item is None:
            raise MutationContractError("Attempt creation requires its current item.")
        attempts.append(
            StoredAttempt(
                change.attempt,
                item.item_id,
                OriginKind.NATIVE,
                change.after,
                change.branch,
                change.base_revision,
                change.owner,
                change.brief_artifact_ref_id,
                None,
                None,
                None,
                None,
                item.scope_revision,
                item.scope_digest,
                revision,
                now,
                now,
                now,
                now,
            )
        )
        return replace(lifecycle, attempts=tuple(sorted(attempts, key=_attempt_key)))
    index = next((position for position, attempt in enumerate(attempts) if attempt.attempt_id == change.attempt), None)
    if index is None or attempts[index].state != change.before or change.after is None:
        raise MutationContractError("The transition attempt change is stale or incomplete.")
    clears_candidate = change.after.value in {"active", "paused", "blocked"}
    records_candidate = change.after.value == "review"
    if records_candidate and (change.protected_candidate_after is None or change.candidate_observed_at is None):
        raise MutationContractError("Review submission requires exact protected candidate provenance.")
    attempts[index] = replace(
        attempts[index],
        state=change.after,
        brief_artifact_ref_id=(
            change.brief_artifact_ref_id
            if change.brief_artifact_ref_id is not None
            else attempts[index].brief_artifact_ref_id
        ),
        candidate_revision=(
            None
            if clears_candidate
            else str(change.protected_candidate_after)
            if records_candidate
            else attempts[index].candidate_revision
        ),
        candidate_recorded_at=(
            None
            if clears_candidate
            else change.candidate_observed_at
            if records_candidate
            else attempts[index].candidate_recorded_at
        ),
        subject_revision=revision,
        origin_updated_at=now if attempts[index].origin == OriginKind.NATIVE else attempts[index].origin_updated_at,
        updated_at=now,
    )
    return replace(lifecycle, attempts=tuple(attempts))


def _transition_proposals_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.proposal_change
    if change is None:
        return common
    disposition = ProposalDisposition(change.disposition.value)
    proposals = list(common.proposals.proposals)
    index = next(
        (position for position, proposal in enumerate(proposals) if proposal.proposal_id == change.proposal), None
    )
    if index is None or proposals[index].disposition is not None:
        raise MutationContractError("The transition proposal change is stale.")
    proposals[index] = replace(
        proposals[index],
        disposition=disposition,
        disposition_target_item_id=change.target_item,
        disposition_reason=change.reason,
        subject_revision=mutation.receipt.project_revision,
        origin_disposed_at=change.disposed_at,
        disposition_recorded_at=change.disposed_at,
    )
    return replace(common, proposals=replace(common.proposals, proposals=tuple(proposals)))


def _transition_attempt_authority_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.attempt_authority_change
    if change is None:
        return common
    authority = common.authority
    counters = list(authority.attempt_counters)
    counter_index = next(
        (index for index, value in enumerate(counters) if value.attempt_id == change.before.attempt), None
    )
    leases = list(authority.attempt_leases)
    lease_index = next((index for index, value in enumerate(leases) if value.attempt_id == change.before.attempt), None)
    anchor = next(
        (
            value
            for value in authority.attempt_generations
            if value.attempt_id == change.before.attempt and value.generation == change.before.generation
        ),
        None,
    )
    if counter_index is None or lease_index is None or anchor is None:
        raise MutationContractError("Attempt-authority fencing requires its exact retained generation.")
    if change.after.lease_id is not None or change.after.generation != change.before.generation + 1:
        raise MutationContractError("Attempt-authority fencing must allocate one revoked generation.")
    counters[counter_index] = replace(counters[counter_index], generation_high_water=change.after.generation)
    leases[lease_index] = replace(
        leases[lease_index],
        generation=change.after.generation,
        expires_at=mutation.decision.receipt.decided_at,
        state=AttemptLeaseState.REVOKED,
    )
    generations = (
        *authority.attempt_generations,
        AttemptLeaseGeneration(
            change.before.attempt, change.after.generation, anchor.lease_id, anchor.task_id, anchor.host_id
        ),
    )
    return replace(
        common,
        authority=replace(
            authority,
            attempt_counters=tuple(counters),
            attempt_generations=tuple(sorted(generations, key=_attempt_generation_key)),
            attempt_leases=tuple(leases),
        ),
    )


def _transition_coordinator_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.coordinator_authority_change
    if change is None:
        return common
    retained = common.authority.coordination
    if retained is None or (
        retained.lease_id,
        retained.task_id,
        retained.host_id,
        retained.generation,
        retained.acquired_at,
        retained.expires_at,
        retained.state.value,
    ) != (
        change.before.lease_id,
        change.before.task_id,
        change.before.host_id,
        change.before.generation,
        change.before.acquired_at,
        change.before.expires_at,
        change.before.state.value,
    ):
        raise MutationContractError("The coordinator transfer is stale.")
    after = change.after
    coordination = replace(
        retained,
        lease_id=after.lease_id,
        task_id=after.task_id,
        host_id=after.host_id,
        generation=after.generation,
        acquired_at=after.acquired_at,
        expires_at=after.expires_at,
        state=CoordinationLeaseState(after.state.value),
    )
    return replace(common, authority=replace(common.authority, coordination=coordination))


def _attempt_authority_carrier_after(
    mutation: AttemptAuthorityMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    authority = common.authority
    counters = list(authority.attempt_counters)
    counter_index = next(
        (index for index, value in enumerate(counters) if value.attempt_id == decision.attempt),
        None,
    )
    counter = AttemptLeaseCounter(decision.attempt, decision.counter_after)
    if counter_index is None:
        if decision.counter_before != 0:
            raise MutationContractError("Attempt authority requires its retained counter.")
        counters.append(counter)
    else:
        if counters[counter_index].generation_high_water != decision.counter_before:
            raise MutationContractError("The attempt-authority counter is stale.")
        counters[counter_index] = counter
    leases = list(authority.attempt_leases)
    lease_index = next(
        (index for index, value in enumerate(leases) if value.attempt_id == decision.attempt),
        None,
    )
    after = decision.current_after
    stored_lease = StoredAttemptLease(
        after.attempt,
        after.generation,
        after.acquired_at,
        after.expires_at,
        AttemptLeaseState(after.state.value),
    )
    if lease_index is None:
        if decision.current_before is not None:
            raise MutationContractError("Attempt authority expected a retained lease row.")
        leases.append(stored_lease)
    else:
        leases[lease_index] = stored_lease
    generations = authority.attempt_generations
    if not any(value.attempt_id == after.attempt and value.generation == after.generation for value in generations):
        generations = (
            *generations,
            AttemptLeaseGeneration(after.attempt, after.generation, after.lease_id, after.task_id, after.host_id),
        )
    use_leases = list(common.resources.use_leases)
    for fenced in decision.fenced_task_uses:
        index = next(
            (
                position
                for position, value in enumerate(use_leases)
                if value.reservation_id == fenced.reservation_id
                and value.generation == fenced.generation
                and value.generation_kind == fenced.generation_kind
                and value.state == UseLeaseState.ACTIVE
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The attempt-authority task-use fence is stale.")
        retained = use_leases[index]
        use_leases[index] = replace(
            retained,
            expires_at=mutation.receipt.transition.decided_at,
            state=UseLeaseState.REVOKED,
        )
        use_leases.append(
            replace(
                retained,
                generation=retained.generation + 1,
                generation_kind=UseLeaseGenerationKind.FENCE,
                acquired_at=mutation.receipt.transition.decided_at,
                expires_at=mutation.receipt.transition.decided_at,
                state=UseLeaseState.REVOKED,
            )
        )
    return replace(
        common,
        authority=replace(
            authority,
            attempt_counters=tuple(sorted(counters, key=_attempt_counter_key)),
            attempt_generations=tuple(sorted(generations, key=_attempt_generation_key)),
            attempt_leases=tuple(sorted(leases, key=_stored_attempt_lease_key)),
        ),
        resources=replace(common.resources, use_leases=tuple(sorted(use_leases, key=_use_lease_key))),
    )


def _task_use_carrier_after(
    mutation: ReservationTaskUseMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    if isinstance(decision, ClaimResourceDecision):
        result = common
        if decision.reservation is not None:
            use = decision.task_use.after
            locator = next(
                (
                    value
                    for value in common.resources.locators
                    if value.instance_id == use.instance_id
                    and value.observation_generation == use.observation_generation
                    and value.observation_digest == use.observation_digest
                ),
                None,
            )
            if locator is None:
                raise MutationContractError("Atomic resource claim requires its exact retained locator observation.")
            resource_mutation = ResourceMutation(
                decision.reservation,
                mutation.before,
                result,
                locator.locator_schema,
                locator.locator,
                locator.observation_generation,
                locator.observation_digest,
                locator.observed_at,
                mutation.receipt,
            )
            result = _resource_after(resource_mutation, result)
        task_use_mutation = ReservationTaskUseMutation(
            mutation.before,
            result,
            mutation.receipt,
            decision.task_use,
        )
        return _task_use_carrier_after(task_use_mutation, result)
    use_leases = list(common.resources.use_leases)
    before_use = decision.before
    if before_use is None:
        if any(
            value.reservation_id == decision.after.reservation_id and value.generation == decision.after.generation
            for value in use_leases
        ):
            raise MutationContractError("Task-use acquisition generation already exists.")
        use_leases.append(_mutation_use(decision.after, decision.changed_at))
    else:
        index = next(
            (
                position
                for position, value in enumerate(use_leases)
                if _mutation_use(before_use, value.acquired_at) == value
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The task-use authority change is stale.")
        acquired_at = use_leases[index].acquired_at
        use_leases[index] = _mutation_use(decision.after, acquired_at)
    if decision.fence is not None:
        if any(
            value.reservation_id == decision.fence.reservation_id and value.generation == decision.fence.generation
            for value in use_leases
        ):
            raise MutationContractError("Task-use fence generation already exists.")
        use_leases.append(_mutation_use(decision.fence, decision.changed_at))
    return replace(
        common,
        resources=replace(
            common.resources,
            use_leases=tuple(sorted(use_leases, key=_use_lease_key)),
        ),
    )


def _transition_resources_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    decision = mutation.decision
    resources = common.resources
    counters = list(resources.reservation_counters)
    for change in decision.reservation_counter_changes:
        index = next(
            (position for position, value in enumerate(counters) if value.instance_id == change.before.instance_id),
            None,
        )
        if index is None or counters[index].generation_high_water != change.before.generation_high_water:
            raise MutationContractError("The reservation counter change is stale.")
        counters[index] = replace(counters[index], generation_high_water=change.after.generation_high_water)
    reservations = list(resources.reservations)
    for change in decision.reservation_changes:
        if change.before is None:
            instance = next(
                (value for value in resources.instances if value.instance_id == change.after.instance_id), None
            )
            attempt = next(
                (value for value in common.lifecycle.attempts if value.attempt_id == change.after.attempt), None
            )
            if instance is None or attempt is None or instance.resource_id != change.after.resource_id:
                raise MutationContractError("Reservation assignment requires its current instance and attempt.")
            reservations.append(
                StoredResourceReservation(
                    change.after.reservation_id,
                    change.after.instance_id,
                    change.after.resource_id,
                    instance.host_id,
                    change.after.generation,
                    change.after.attempt,
                    attempt.item_id,
                    change.after.state,
                    mutation.receipt.project_revision,
                    decision.receipt.decided_at,
                    None,
                )
            )
            continue
        index = next(
            (
                position
                for position, value in enumerate(reservations)
                if value.reservation_id == change.before.reservation_id
                and value.acquisition_generation == change.before.generation
                and value.state == change.before.state
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The reservation change is stale.")
        ended_at = (
            None
            if change.after.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
            else decision.receipt.decided_at
        )
        reservations[index] = replace(
            reservations[index],
            state=change.after.state,
            subject_revision=mutation.receipt.project_revision,
            ended_at=ended_at,
        )
    use_leases = list(resources.use_leases)
    for change in decision.resource_use_lease_changes:
        index = next(
            (
                position
                for position, value in enumerate(use_leases)
                if value.reservation_id == change.before.reservation_id
                and value.generation == change.before.generation
                and value.generation_kind == change.before.generation_kind
                and value.state == change.before.state
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The task-use change is stale.")
        before = use_leases[index]
        use_leases[index] = replace(before, state=change.after.state, expires_at=decision.receipt.decided_at)
        if change.before.state == UseLeaseState.ACTIVE and change.after.state == UseLeaseState.REVOKED:
            use_leases.append(
                replace(
                    before,
                    generation=before.generation + 1,
                    generation_kind=UseLeaseGenerationKind.FENCE,
                    acquired_at=decision.receipt.decided_at,
                    expires_at=decision.receipt.decided_at,
                    state=UseLeaseState.REVOKED,
                )
            )
    return replace(
        common,
        resources=replace(
            resources,
            reservation_counters=tuple(sorted(counters, key=_reservation_counter_key)),
            reservations=tuple(sorted(reservations, key=_reservation_key)),
            use_leases=tuple(sorted(use_leases, key=_use_lease_key)),
        ),
    )


def _transition_focus_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.item_change
    if change is None:
        return common
    terminal = change.after is None
    kind = mutation.decision.action.kind
    if terminal:
        next_action = "select"
    elif kind in {ActionKind.PAUSE, ActionKind.BLOCK, ActionKind.BLOCK_ITEM}:
        next_action = "resume"
    elif kind == ActionKind.SUBMIT_REVIEW:
        next_action = "review"
    elif kind in {ActionKind.RETURN_FOR_CORRECTION, ActionKind.RESUME, ActionKind.REOPEN, ActionKind.MARK_READY}:
        next_action = "continue"
    elif kind == ActionKind.DEFER:
        next_action = "reopen"
    else:
        next_action = kind.value
    return replace(
        common,
        focus=StoredFocus(
            None if terminal else change.item,
            None if terminal else change.attempt,
            next_action,
            mutation.receipt.project_revision,
        ),
    )


def _transition_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    lifecycle = _transition_item_after(mutation, common.lifecycle)
    lifecycle = _transition_attempt_after(mutation, lifecycle)
    result = replace(common, lifecycle=lifecycle)
    proposal_change = mutation.decision.proposal_change
    accepted = None if proposal_change is None else proposal_change.accepted_item
    if accepted is not None:
        requirements = (
            *result.resources.requirements,
            *(
                ItemResourceRequirement(accepted.item, resource_id, position)
                for position, resource_id in enumerate(accepted.resource_requirements)
            ),
        )
        result = replace(
            result,
            resources=replace(
                result.resources,
                requirements=tuple(sorted(requirements, key=_requirement_key)),
            ),
        )
    result = _transition_proposals_after(mutation, result)
    result = _transition_coordinator_after(mutation, result)
    result = _transition_attempt_authority_after(mutation, result)
    result = _transition_resources_after(mutation, result)
    return _transition_focus_after(mutation, result)


def _resource_definition_after(
    mutation: ResourceDefinitionEditMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    definitions = list(common.resources.definitions)
    index = next(
        (
            position
            for position, value in enumerate(definitions)
            if value.resource_id == decision.after_definition.resource_id
        ),
        None,
    )
    changed_at = mutation.receipt.transition.decided_at
    if decision.before_definition is None:
        if index is not None or decision.definition_revision_before is not None:
            raise MutationContractError("Resource-definition creation expected an absent definition.")
        definitions.append(
            StoredResourceDefinition(
                decision.after_definition.resource_id,
                OriginKind.NATIVE,
                decision.after_definition.kind,
                decision.after_definition.description,
                decision.definition_revision_after,
                changed_at,
                changed_at,
                changed_at,
                changed_at,
            )
        )
    else:
        if index is None:
            raise MutationContractError("Resource-definition editing expected a retained definition.")
        retained = definitions[index]
        if (retained.resource_id, retained.kind, retained.description, retained.subject_revision) != (
            decision.before_definition.resource_id,
            decision.before_definition.kind,
            decision.before_definition.description,
            decision.definition_revision_before,
        ):
            raise MutationContractError("The resource-definition decision is stale.")
        definitions[index] = replace(
            retained,
            kind=decision.after_definition.kind,
            description=decision.after_definition.description,
            subject_revision=decision.definition_revision_after,
            origin_updated_at=changed_at,
            updated_at=changed_at,
        )
    items = list(common.lifecycle.work_items)
    for change in decision.affected_item_revisions:
        item_index = next((position for position, value in enumerate(items) if value.item_id == change.item), None)
        if item_index is None or items[item_index].subject_revision != change.before:
            raise MutationContractError("A requiring item subject revision is stale.")
        items[item_index] = replace(items[item_index], subject_revision=change.after, updated_at=changed_at)
    return replace(
        common,
        lifecycle=replace(common.lifecycle, work_items=tuple(items)),
        resources=replace(
            common.resources,
            definitions=tuple(sorted(definitions, key=_resource_definition_key)),
        ),
    )


def _carrier_after(  # noqa: C901, PLR0912
    mutation: StoredStateMutation, common: StoredWorkState
) -> StoredWorkState:
    supplied = mutation.after
    match mutation:
        case ProposalCreationMutation():
            return _proposal_creation_after(mutation, common)
        case TransitionMutation():
            return _transition_after(mutation, common)
        case DependencyEditMutation(decision=decision):
            return _scope_edit_after(
                common,
                decision,
                mutation.receipt.transition.decided_at,
                dependencies=True,
            )
        case ResourceRequirementEditMutation(decision=decision):
            return _scope_edit_after(
                common,
                decision,
                mutation.receipt.transition.decided_at,
                dependencies=False,
            )
        case ResourceDefinitionEditMutation():
            return _resource_definition_after(mutation, common)
        case CoordinationAuthorityMutation():
            if supplied.authority.coordination == common.authority.coordination:
                raise MutationContractError("A coordination-authority carrier must change the coordination lease.")
            decision = mutation.decision
            retained = mutation.before.authority.coordination
            changed = supplied.authority.coordination
            if changed is None or (
                changed.lease_id,
                changed.task_id,
                changed.host_id,
                changed.generation,
                changed.acquired_at,
                changed.expires_at,
                changed.state.value,
            ) != (
                decision.after.lease_id,
                decision.after.task_id,
                decision.after.host_id,
                decision.after.generation,
                decision.after.acquired_at,
                decision.after.expires_at,
                decision.after.state.value,
            ):
                raise MutationContractError("The coordination decision does not match its relational delta.")
            if decision.before is None:
                if retained is not None:
                    raise MutationContractError("Coordination acquisition expected no retained authority.")
            elif retained is None or (
                retained.lease_id,
                retained.task_id,
                retained.host_id,
                retained.generation,
                retained.acquired_at,
                retained.expires_at,
                retained.state.value,
            ) != (
                decision.before.lease_id,
                decision.before.task_id,
                decision.before.host_id,
                decision.before.generation,
                decision.before.acquired_at,
                decision.before.expires_at,
                decision.before.state.value,
            ):
                raise MutationContractError("The coordination decision is stale.")
            return replace(common, authority=replace(common.authority, coordination=supplied.authority.coordination))
        case AttemptAuthorityMutation():
            return _attempt_authority_carrier_after(mutation, common)
        case ReservationTaskUseMutation():
            return _task_use_carrier_after(mutation, common)
        case PlanningImpactMutation() | PlanningResolutionMutation() | ResourceMutation() | ResourceIntentMutation():
            raise MutationContractError("A pure decision cannot be committed as a carrier mutation.")
        case _ as unreachable:
            assert_never(unreachable)


def _planning_impact_after(mutation: PlanningImpactMutation, common: StoredWorkState) -> StoredWorkState:
    impact = mutation.decision.impact
    if any(value.impact_id == impact.impact_id for value in mutation.before.planning.impacts):
        raise MutationContractError("A planning impact must introduce a new impact identity.")
    if not impact.obligations:
        raise MutationContractError("A planning impact must create at least one obligation.")
    stored_impact = StoredPlanningImpact(
        impact.impact_id,
        impact.source_item,
        impact.source_attempt,
        impact.source_scope_revision,
        impact.source_scope_digest,
        impact.obligations[0].target,
        impact.summary,
        impact.evidence,
        common.lifecycle.project.revision,
        mutation.receipt.decided_at,
    )
    obligations = tuple(
        StoredPlanningObligation(
            impact.impact_id,
            obligation.target,
            obligation.position,
            obligation.observed_scope_revision,
            obligation.observed_scope_digest,
            PlanningObligationState.UNRESOLVED,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            mutation.receipt.decided_at,
            None,
        )
        for obligation in impact.obligations
    )
    return replace(
        common,
        planning=replace(
            common.planning,
            impacts=(*common.planning.impacts, stored_impact),
            obligations=(*common.planning.obligations, *obligations),
        ),
    )


def _planning_lifecycle_after(
    mutation: PlanningResolutionMutation,
    common: StoredWorkState,
) -> LifecycleRecords:
    decision = mutation.decision
    impact = decision.impact
    lifecycle = common.lifecycle
    if decision.item_change is not None:
        change = decision.item_change
        items = list(lifecycle.work_items)
        index = next((i for i, value in enumerate(items) if value.item_id == change.item), None)
        if index is None or items[index].state.value != (None if change.before is None else change.before.value):
            raise MutationContractError("The planning item change is stale.")
        disposition = next((value.disposition for value in impact.obligations if value.target == change.item), None)
        if change.after is None:
            if disposition not in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
                raise MutationContractError("A terminal planning item change requires a terminal disposition.")
            state = StoredWorkItemState(disposition.value)
        else:
            state = StoredWorkItemState(change.after.value)
        items[index] = replace(
            items[index],
            state=state,
            outcome_evidence=change.outcome_evidence,
            subject_revision=common.lifecycle.project.revision,
            origin_updated_at=mutation.receipt.decided_at,
            updated_at=mutation.receipt.decided_at,
        )
        lifecycle = replace(lifecycle, work_items=tuple(items))
    if decision.attempt_change is not None:
        change = decision.attempt_change
        attempts = list(lifecycle.attempts)
        index = next((i for i, value in enumerate(attempts) if value.attempt_id == change.attempt), None)
        if index is None or attempts[index].state != change.before or change.after is None:
            raise MutationContractError("The planning attempt change is stale or incomplete.")
        attempts[index] = replace(
            attempts[index],
            state=change.after,
            subject_revision=common.lifecycle.project.revision,
            origin_updated_at=mutation.receipt.decided_at,
            updated_at=mutation.receipt.decided_at,
        )
        lifecycle = replace(lifecycle, attempts=tuple(attempts))
    return lifecycle


def _planning_scope_after(
    mutation: PlanningResolutionMutation,
    lifecycle: LifecycleRecords,
) -> LifecycleRecords:
    scope = mutation.decision.scope_change
    if scope is None:
        return lifecycle
    current = next((value for value in lifecycle.work_items if value.item_id == scope.item), None)
    if current is None or scope.revision != current.scope_revision + 1:
        raise MutationContractError("The revised planning scope is stale.")
    items = tuple(
        replace(
            value,
            user_label=scope.scope.user_label,
            trigger=scope.scope.trigger,
            why_it_matters=scope.scope.why_it_matters,
            effect=scope.scope.effect,
            unlock=scope.scope.unlock,
            scope_revision=scope.revision,
            scope_digest=scope.digest,
            subject_revision=mutation.receipt.project_revision,
            origin_updated_at=mutation.receipt.decided_at,
            updated_at=mutation.receipt.decided_at,
        )
        if value.item_id == scope.item
        else value
        for value in lifecycle.work_items
    )
    dependencies = tuple(value for value in lifecycle.dependencies if value.item_id != scope.item) + tuple(
        ItemDependency(scope.item, value.dependency_id, value.position) for value in scope.scope.dependencies
    )
    anchor = ItemScopeRevision(
        scope.item,
        scope.revision,
        scope.digest,
        mutation.receipt.project_revision,
        mutation.receipt.decided_at,
    )
    return replace(
        lifecycle,
        work_items=items,
        scope_revisions=tuple(sorted((*lifecycle.scope_revisions, anchor), key=_scope_revision_key)),
        dependencies=tuple(sorted(dependencies, key=_dependency_key)),
    )


def _planning_authority_after(
    mutation: PlanningResolutionMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    change = mutation.decision.attempt_authority_change
    if change is None:
        return common
    authority = common.authority
    counters = list(authority.attempt_counters)
    counter_index = next(
        (index for index, value in enumerate(counters) if value.attempt_id == change.before.attempt), None
    )
    leases = list(authority.attempt_leases)
    lease_index = next((index for index, value in enumerate(leases) if value.attempt_id == change.before.attempt), None)
    anchor = next(
        (
            value
            for value in authority.attempt_generations
            if value.attempt_id == change.before.attempt and value.generation == change.before.generation
        ),
        None,
    )
    if counter_index is None or lease_index is None or anchor is None:
        raise MutationContractError("Planning terminal authority fencing requires its exact retained generation.")
    if change.after.lease_id is not None or change.after.generation != change.before.generation + 1:
        raise MutationContractError("Planning terminal authority must allocate one revoked generation.")
    counters[counter_index] = replace(counters[counter_index], generation_high_water=change.after.generation)
    leases[lease_index] = replace(
        leases[lease_index],
        generation=change.after.generation,
        expires_at=mutation.receipt.decided_at,
        state=AttemptLeaseState.REVOKED,
    )
    generations = (
        *authority.attempt_generations,
        AttemptLeaseGeneration(
            change.before.attempt,
            change.after.generation,
            anchor.lease_id,
            anchor.task_id,
            anchor.host_id,
        ),
    )
    return replace(
        common,
        authority=replace(
            authority,
            attempt_counters=tuple(counters),
            attempt_generations=tuple(sorted(generations, key=_attempt_generation_key)),
            attempt_leases=tuple(leases),
        ),
    )


def _planning_resources_after(
    mutation: PlanningResolutionMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    reservations = list(common.resources.reservations)
    for change in mutation.decision.reservation_changes:
        assert change.before is not None
        index = next(
            (
                position
                for position, value in enumerate(reservations)
                if value.reservation_id == change.before.reservation_id
                and value.acquisition_generation == change.before.generation
                and value.state == change.before.state
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The planning reservation change is stale.")
        reservations[index] = replace(
            reservations[index],
            state=change.after.state,
            subject_revision=mutation.receipt.project_revision,
            ended_at=mutation.receipt.decided_at,
        )
    use_leases = list(common.resources.use_leases)
    for change in mutation.decision.resource_use_lease_changes:
        index = next(
            (
                position
                for position, value in enumerate(use_leases)
                if value.reservation_id == change.before.reservation_id
                and value.generation == change.before.generation
                and value.state == change.before.state
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The planning task-use change is stale.")
        before = use_leases[index]
        use_leases[index] = replace(before, state=change.after.state, expires_at=mutation.receipt.decided_at)
        if change.after.state == UseLeaseState.REVOKED:
            use_leases.append(
                replace(
                    before,
                    generation=before.generation + 1,
                    generation_kind=UseLeaseGenerationKind.FENCE,
                    acquired_at=mutation.receipt.decided_at,
                    expires_at=mutation.receipt.decided_at,
                    state=UseLeaseState.REVOKED,
                )
            )
    return replace(
        common,
        resources=replace(
            common.resources,
            reservations=tuple(sorted(reservations, key=_reservation_key)),
            use_leases=tuple(sorted(use_leases, key=_use_lease_key)),
        ),
    )


def _planning_resolution_after(mutation: PlanningResolutionMutation, common: StoredWorkState) -> StoredWorkState:
    decision = mutation.decision
    impact = decision.impact
    existing_impact = next((value for value in common.planning.impacts if value.impact_id == impact.impact_id), None)
    if existing_impact is None:
        raise MutationContractError("The planning resolution impact does not exist.")
    expected_header = (
        existing_impact.source_item_id,
        existing_impact.source_attempt_id,
        existing_impact.source_scope_revision,
        existing_impact.source_scope_digest,
        existing_impact.summary,
        existing_impact.evidence,
    )
    if expected_header != (
        impact.source_item,
        impact.source_attempt,
        impact.source_scope_revision,
        impact.source_scope_digest,
        impact.summary,
        impact.evidence,
    ):
        raise MutationContractError("The planning resolution changed immutable impact facts.")
    before_obligations = {(value.impact_id, value.target_item_id): value for value in common.planning.obligations}
    impact_targets = {
        value.target_item_id for value in common.planning.obligations if value.impact_id == impact.impact_id
    }
    if {value.target for value in impact.obligations} != impact_targets:
        raise MutationContractError("A planning resolution must retain every obligation in its impact.")
    newly_resolved = tuple(
        obligation.target
        for obligation in impact.obligations
        if obligation.disposition is not None
        and (before := before_obligations.get((impact.impact_id, obligation.target))) is not None
        and before.state == PlanningObligationState.UNRESOLVED
    )
    if newly_resolved != (mutation.target,):
        raise MutationContractError("A planning resolution must identify its one newly resolved target exactly.")
    resolved: list[StoredPlanningObligation] = []
    replacements: list[StoredPlanningReplacement] = [
        value for value in common.planning.replacements if value.impact_id != impact.impact_id
    ]
    for obligation in impact.obligations:
        before = before_obligations.get((impact.impact_id, obligation.target))
        if before is None or before.position != obligation.position:
            raise MutationContractError("The planning resolution obligation does not match stored planning state.")
        state = (
            PlanningObligationState.RESOLVED
            if obligation.disposition is not None
            else PlanningObligationState.UNRESOLVED
        )
        resolved_at = before.resolved_at
        resolved_revision = before.resolved_project_revision
        if obligation.disposition is not None and before.state == PlanningObligationState.UNRESOLVED:
            resolved_at = mutation.receipt.decided_at
            resolved_revision = common.lifecycle.project.revision
        primary = obligation.replacements[0] if obligation.replacements else None
        resolved.append(
            replace(
                before,
                state=state,
                disposition=obligation.disposition,
                evaluated_scope_revision=obligation.evaluated_scope_revision,
                evaluated_scope_digest=obligation.evaluated_scope_digest,
                resulting_scope_revision=obligation.resulting_scope_revision,
                resulting_scope_digest=obligation.resulting_scope_digest,
                primary_replacement_item_id=primary,
                outcome_evidence=obligation.outcome_evidence,
                reason=obligation.reason,
                resolved_project_revision=resolved_revision,
                resolved_at=resolved_at,
            )
        )
        replacements.extend(
            StoredPlanningReplacement(impact.impact_id, obligation.target, item_id, position)
            for position, item_id in enumerate(obligation.replacements)
        )
    resolved_by_target = {value.target_item_id: value for value in resolved}
    obligations = tuple(
        resolved_by_target[value.target_item_id] if value.impact_id == impact.impact_id else value
        for value in common.planning.obligations
    )
    expected = replace(
        common,
        lifecycle=_planning_scope_after(mutation, _planning_lifecycle_after(mutation, common)),
        planning=PlanningRecords(
            common.planning.impacts,
            obligations,
            tuple(replacements),
        ),
    )
    scope = decision.scope_change
    if scope is not None:
        requirements = tuple(value for value in expected.resources.requirements if value.item_id != scope.item) + tuple(
            ItemResourceRequirement(scope.item, value.resource_id, value.position)
            for value in scope.scope.resource_requirements
        )
        expected = replace(
            expected,
            resources=replace(expected.resources, requirements=tuple(sorted(requirements, key=_requirement_key))),
        )
    expected = _planning_authority_after(mutation, expected)
    expected = _planning_resources_after(mutation, expected)
    if expected == common:
        raise MutationContractError("A planning resolution must change its owned relational facts.")
    return expected


def _stored_reservation(value: StoredResourceReservation) -> ResourceReservation:
    return ResourceReservation(
        value.reservation_id,
        value.resource_id,
        value.instance_id,
        value.attempt_id,
        value.acquisition_generation,
        value.state,
    )


def _stored_counter(value: StoredReservationCounter) -> ResourceReservationCounter:
    return ResourceReservationCounter(value.instance_id, value.generation_high_water)


def _resource_after(  # noqa: C901, PLR0912
    mutation: ResourceMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    if (
        not mutation.decision.changes
        and not mutation.decision.counter_changes
        and not mutation.decision.use_lease_changes
    ):
        raise MutationContractError("A resource decision must change a reservation or generation counter.")
    resources = common.resources
    if not any(
        (
            value.locator_schema,
            value.locator,
            value.observation_generation,
            value.observation_digest,
            value.observed_at,
        )
        == (
            mutation.locator_schema,
            mutation.locator,
            mutation.observation_generation,
            mutation.observation_digest,
            mutation.observed_at,
        )
        for value in resources.locators
    ):
        raise MutationContractError("A resource mutation requires its exact retained locator observation.")
    reservations = list(resources.reservations)
    for change in mutation.decision.changes:
        index = next(
            (i for i, value in enumerate(reservations) if value.reservation_id == change.after.reservation_id), None
        )
        if change.before is None:
            if index is not None:
                raise MutationContractError("A new resource reservation already exists.")
            instance = next(
                (value for value in resources.instances if value.instance_id == change.after.instance_id), None
            )
            attempt = next(
                (value for value in common.lifecycle.attempts if value.attempt_id == change.after.attempt), None
            )
            if instance is None or attempt is None:
                raise MutationContractError("A resource reservation requires stored instance and attempt owners.")
            reservations.append(
                StoredResourceReservation(
                    change.after.reservation_id,
                    change.after.instance_id,
                    change.after.resource_id,
                    instance.host_id,
                    change.after.generation,
                    change.after.attempt,
                    attempt.item_id,
                    change.after.state,
                    common.lifecycle.project.revision,
                    mutation.receipt.transition.decided_at,
                    None,
                )
            )
        else:
            if index is None or _stored_reservation(reservations[index]) != change.before:
                raise MutationContractError("The resource reservation change is stale.")
            before = reservations[index]
            ended_at = (
                None
                if change.after.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
                else mutation.receipt.transition.decided_at
            )
            reservations[index] = replace(
                before,
                instance_id=change.after.instance_id,
                resource_id=change.after.resource_id,
                acquisition_generation=change.after.generation,
                attempt_id=change.after.attempt,
                state=change.after.state,
                subject_revision=common.lifecycle.project.revision,
                ended_at=ended_at,
            )
    counters = list(resources.reservation_counters)
    for change in mutation.decision.counter_changes:
        index = next((i for i, value in enumerate(counters) if value.instance_id == change.after.instance_id), None)
        if index is None or _stored_counter(counters[index]) != change.before:
            raise MutationContractError("The resource reservation counter change is stale.")
        counters[index] = StoredReservationCounter(change.after.instance_id, change.after.generation_high_water)
    use_leases = list(resources.use_leases)
    for change in mutation.decision.use_lease_changes:
        index = next(
            (
                position
                for position, value in enumerate(use_leases)
                if value.reservation_id == change.before.reservation_id
                and value.generation == change.before.generation
                and value.state == change.before.state
            ),
            None,
        )
        if index is None:
            raise MutationContractError("The resource decision task-use change is stale.")
        before_use = use_leases[index]
        use_leases[index] = replace(
            before_use,
            state=change.after.state,
            expires_at=mutation.receipt.transition.decided_at,
        )
        if change.after.state == UseLeaseState.REVOKED:
            use_leases.append(
                replace(
                    before_use,
                    generation=before_use.generation + 1,
                    generation_kind=UseLeaseGenerationKind.FENCE,
                    acquired_at=mutation.receipt.transition.decided_at,
                    expires_at=mutation.receipt.transition.decided_at,
                    state=UseLeaseState.REVOKED,
                )
            )
    return replace(
        common,
        resources=replace(
            resources,
            reservations=tuple(reservations),
            reservation_counters=tuple(counters),
            use_leases=tuple(sorted(use_leases, key=_use_lease_key)),
        ),
    )


def _intent(value: MutationIntent) -> ResourceMutationIntent:
    return ResourceMutationIntent(
        value.intent_id,
        value.reservation_id,
        value.reservation_generation,
        value.instance_id,
        value.attempt_id,
        value.host_id,
        value.resource_use_generation,
        value.resource_use_lease_id,
        value.task_id,
        value.attempt_lease_id,
        value.attempt_lease_generation,
        value.start_instance_subject_revision,
        value.start_observation_generation,
        value.start_observation_digest,
        value.policy_schema,
        value.policy,
        value.policy_digest,
        value.state,
        value.recorded_at,
        value.resolved_at,
        value.result_observation_generation,
        value.result_observation_digest,
        value.evidence_schema,
        value.evidence,
        value.evidence_digest,
        value.disposition_task_id,
        value.disposition_reason,
    )


def _observation(value: ResourceObservation) -> ResourceInstanceLocator:
    return ResourceInstanceLocator(
        value.instance_id,
        value.host_id,
        value.locator_schema,
        value.locator,
        value.generation,
        value.digest,
        value.observed_at,
    )


def _mutation_reservation(
    value: MutationReservation,
    previous: StoredResourceReservation,
    decided_at: datetime | None,
) -> StoredResourceReservation:
    ended_at = previous.ended_at
    if value.state in {ReservationState.RELEASED, ReservationState.REVOKED} and value.state != previous.state:
        ended_at = decided_at
    return replace(
        previous,
        instance_id=value.instance_id,
        resource_id=value.resource_id,
        host_id=value.host_id,
        acquisition_generation=value.acquisition_generation,
        attempt_id=value.attempt_id,
        item_id=value.item_id,
        state=value.state,
        subject_revision=value.subject_revision,
        ended_at=ended_at,
    )


def _mutation_use(value: MutationUseLease, acquired_at: datetime) -> StoredResourceUseLease:
    return StoredResourceUseLease(
        value.reservation_id,
        value.instance_id,
        value.reservation_generation,
        value.attempt_id,
        value.host_id,
        value.instance_subject_revision,
        value.observation_generation,
        value.observation_digest,
        value.task_id,
        value.attempt_lease_id,
        value.attempt_lease_generation,
        value.lease_id,
        value.generation,
        value.generation_kind,
        value.host_epoch,
        acquired_at,
        value.expires_at,
        value.state,
    )


def _apply_intent_change(
    mutation: ResourceIntentMutation,
    intents: tuple[ResourceMutationIntent, ...],
) -> tuple[ResourceMutationIntent, ...]:
    decision = mutation.decision
    retained = list(intents)
    before_intent = _intent(decision.intent_change.before) if decision.intent_change.before is not None else None
    index = next(
        (i for i, value in enumerate(retained) if value.intent_id == decision.intent_change.after.intent_id), None
    )
    if before_intent is None:
        if index is not None:
            raise MutationContractError("A new resource mutation intent already exists.")
        retained.append(_intent(decision.intent_change.after))
    else:
        if index is None or retained[index] != before_intent:
            raise MutationContractError("The resource mutation intent change is stale.")
        retained[index] = _intent(decision.intent_change.after)
    return tuple(retained)


def _apply_observation_changes(
    mutation: ResourceIntentMutation,
    common: StoredWorkState,
) -> tuple[tuple[ResourceInstanceLocator, ...], tuple[StoredResourceInstance, ...]]:
    decision = mutation.decision
    locators = list(common.resources.locators)
    if decision.observation_change is not None:
        change = decision.observation_change
        index = next((i for i, value in enumerate(locators) if value.instance_id == change.before.instance_id), None)
        if index is None or locators[index] != _observation(change.before):
            raise MutationContractError("The resource observation change is stale.")
        locators[index] = _observation(change.after)
    instances = list(common.resources.instances)
    if decision.instance_revision_change is not None:
        change = decision.instance_revision_change
        index = next((i for i, value in enumerate(instances) if value.instance_id == change.instance_id), None)
        if index is None or instances[index].subject_revision != change.before:
            raise MutationContractError("The resource instance revision change is stale.")
        instances[index] = replace(
            instances[index], subject_revision=change.after, updated_at=mutation.receipt.transition.decided_at
        )
    return tuple(locators), tuple(instances)


def _apply_use_lease_changes(
    mutation: ResourceIntentMutation,
    use_records: tuple[StoredResourceUseLease, ...],
) -> tuple[StoredResourceUseLease, ...]:
    decision = mutation.decision
    use_leases = list(use_records)
    for change in decision.use_lease_changes:
        index = next((i for i, value in enumerate(use_leases) if value.lease_id == change.after.lease_id), None)
        if change.before is None:
            if index is not None:
                raise MutationContractError("A new resource-use lease already exists.")
            use_leases.append(_mutation_use(change.after, mutation.receipt.transition.decided_at))
        else:
            if index is None or use_leases[index] != _mutation_use(change.before, use_leases[index].acquired_at):
                raise MutationContractError("The resource-use lease change is stale.")
            use_leases[index] = _mutation_use(change.after, use_leases[index].acquired_at)
    return tuple(use_leases)


def _apply_intent_reservation_change(
    mutation: ResourceIntentMutation,
    reservation_records: tuple[StoredResourceReservation, ...],
) -> tuple[StoredResourceReservation, ...]:
    decision = mutation.decision
    reservations = list(reservation_records)
    if decision.reservation_change is not None:
        change = decision.reservation_change
        index = next(
            (i for i, value in enumerate(reservations) if value.reservation_id == change.before.reservation_id), None
        )
        if (
            index is None
            or _mutation_reservation(change.before, reservations[index], reservations[index].ended_at)
            != reservations[index]
        ):
            raise MutationContractError("The mutation reservation change is stale.")
        reservations[index] = _mutation_reservation(
            change.after,
            reservations[index],
            mutation.receipt.transition.decided_at,
        )
    return tuple(reservations)


def _resource_intent_after(mutation: ResourceIntentMutation, common: StoredWorkState) -> StoredWorkState:
    resources = common.resources
    locators, instances = _apply_observation_changes(mutation, common)
    expected = replace(
        common,
        resources=replace(
            resources,
            instances=instances,
            locators=locators,
            reservations=_apply_intent_reservation_change(mutation, resources.reservations),
            use_leases=_apply_use_lease_changes(mutation, resources.use_leases),
            mutation_intents=_apply_intent_change(mutation, resources.mutation_intents),
        ),
    )
    if expected == common:
        raise MutationContractError("A resource-intent decision must change its owned relational facts.")
    return expected


def expected_stored_state(mutation: StoredStateMutation) -> StoredWorkState:
    common = _common_after(mutation)
    match mutation:
        case (
            TransitionMutation()
            | ProposalCreationMutation()
            | DependencyEditMutation()
            | ResourceRequirementEditMutation()
            | ResourceDefinitionEditMutation()
            | CoordinationAuthorityMutation()
            | AttemptAuthorityMutation()
            | ReservationTaskUseMutation()
        ):
            expected = _carrier_after(mutation, common)
        case PlanningImpactMutation():
            expected = _planning_impact_after(mutation, common)
        case PlanningResolutionMutation():
            expected = _planning_resolution_after(mutation, common)
        case ResourceMutation():
            expected = _resource_after(mutation, common)
        case ResourceIntentMutation():
            expected = _resource_intent_after(mutation, common)
        case _ as unreachable:
            assert_never(unreachable)
    return expected


def project_transition_mutation(before: StoredWorkState, decision: Decision) -> TransitionMutation:
    """Project one pure lifecycle decision into its exact flat accepted mutation."""

    action = decision.action
    actor_task_id: TaskId | None = None
    actor_host_id: HostId | None = None
    if action.authorization == AuthorizationKind.ATTEMPT and action.lease_id is not None:
        anchor = next(
            (
                value
                for value in before.authority.attempt_generations
                if value.lease_id == action.lease_id and value.generation == action.coordinator_generation
            ),
            None,
        )
        if anchor is not None:
            actor_task_id, actor_host_id = anchor.task_id, anchor.host_id
    elif action.authorization == AuthorizationKind.COORDINATION:
        coordination = before.authority.coordination
        if coordination is not None:
            actor_task_id, actor_host_id = coordination.task_id, coordination.host_id
    receipt = MutationReceipt(
        decision.receipt,
        HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
        before.lifecycle.project.revision + 1,
        TransitionHistoryActionKind(action.kind.value),
        HistorySubjectId(action.subject),
        None,
        TransitionHistoryAuthorizationKind(action.authorization.value),
        actor_task_id,
        actor_host_id,
        "decision/v1",
        CanonicalJson(b"{}"),
    )
    draft = TransitionMutation(decision, before, before, receipt)
    return replace(draft, after=expected_stored_state(draft))
