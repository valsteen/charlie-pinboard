from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.stored_state import (
    HistoryRecords,
    LifecycleRecords,
    PlanningObligationState,
    PlanningRecords,
    ResourceInstanceLocator,
    ResourceMutationIntent,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredReservationCounter,
    StoredResourceInstance,
    StoredResourceReservation,
    StoredResourceUseLease,
    StoredTransitionReceipt,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.decisions import Decision, TransitionReceipt
from charlie_pinboard.domain.history import encode_transition_receipt_outcome
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import (
    CanonicalJson,
    MutationIntent,
    MutationReservation,
    MutationUseLease,
    PlanningDisposition,
    PlanningImpact,
    ReservationState,
    ResourceObservation,
    ResourceReservation,
    ResourceReservationCounter,
)
from charlie_pinboard.domain.planning_decisions import PlanningResolutionDecision
from charlie_pinboard.domain.resource_decisions import ResourceDecision, ResourceIntentDecision


class MutationContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class DependencyEditMutation:
    """Persists an authorized dependency edit without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceRequirementEditMutation:
    """Persists an authorized resource-requirement edit without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Persists an authorized coordination change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class ReservationTaskUseMutation:
    """Persists an authorized reservation or task-use change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class PlanningImpactMutation:
    impact: PlanningImpact
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class PlanningResolutionMutation:
    decision: PlanningResolutionDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceMutation:
    decision: ResourceDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceIntentMutation:
    decision: ResourceIntentDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt
    stored_receipt: StoredTransitionReceipt


type AcceptedMutation = (
    Decision
    | ProposalCreationMutation
    | DependencyEditMutation
    | ResourceRequirementEditMutation
    | CoordinationAuthorityMutation
    | AttemptAuthorityMutation
    | ReservationTaskUseMutation
    | PlanningImpactMutation
    | PlanningResolutionMutation
    | ResourceMutation
    | ResourceIntentMutation
)


type StoredStateMutation = (
    ProposalCreationMutation
    | DependencyEditMutation
    | ResourceRequirementEditMutation
    | CoordinationAuthorityMutation
    | AttemptAuthorityMutation
    | ReservationTaskUseMutation
    | PlanningImpactMutation
    | PlanningResolutionMutation
    | ResourceMutation
    | ResourceIntentMutation
)


def _common_after(mutation: StoredStateMutation) -> StoredWorkState:
    before = mutation.before
    project = before.lifecycle.project
    stored_receipt = mutation.stored_receipt
    expected_outcome = CanonicalJson(
        encode_transition_receipt_outcome(
            evidence=mutation.receipt.evidence,
            outcome=mutation.receipt.outcome,
        )
    )
    next_history_id = 1 + max((int(value.history_id) for value in before.history.receipts), default=0)
    if (
        int(stored_receipt.history_id) != next_history_id
        or stored_receipt.project_revision != project.revision + 1
        or stored_receipt.action_id != mutation.receipt.action_id
        or stored_receipt.committed_at != mutation.receipt.decided_at
        or stored_receipt.outcome_payload != expected_outcome
    ):
        raise MutationContractError("The accepted stored receipt does not identify the mutation exactly.")
    return replace(
        before,
        lifecycle=replace(
            before.lifecycle,
            project=replace(project, revision=project.revision + 1, updated_at=mutation.receipt.decided_at),
        ),
        history=HistoryRecords((*before.history.receipts, stored_receipt)),
    )


def _validate_proposal_creation(common: StoredWorkState, supplied: StoredWorkState) -> None:
    if (
        len(supplied.proposals.proposals) <= len(common.proposals.proposals)
        or supplied.proposals.proposals[: len(common.proposals.proposals)] != common.proposals.proposals
        or supplied.proposals.evidence[: len(common.proposals.evidence)] != common.proposals.evidence
        or supplied.proposals.freshness[: len(common.proposals.freshness)] != common.proposals.freshness
    ):
        raise MutationContractError("Proposal creation may only append exact proposal records.")


def _validate_dependency_edit(common: StoredWorkState, supplied: StoredWorkState) -> None:
    changed_items = _validate_scope_edit_items(common, supplied)
    if (
        not changed_items
        or supplied.lifecycle.dependencies == common.lifecycle.dependencies
        or tuple(value for value in supplied.lifecycle.dependencies if value.item_id not in changed_items)
        != tuple(value for value in common.lifecycle.dependencies if value.item_id not in changed_items)
    ):
        raise MutationContractError("A dependency edit may only replace dependencies for scope-edited items.")


def _validate_requirement_edit(common: StoredWorkState, supplied: StoredWorkState) -> None:
    changed_items = _validate_scope_edit_items(common, supplied)
    retained_definitions = set(common.resources.definitions)
    if (
        not changed_items
        or (
            supplied.resources.definitions == common.resources.definitions
            and supplied.resources.requirements == common.resources.requirements
        )
        or not retained_definitions.issubset(set(supplied.resources.definitions))
        or tuple(value for value in supplied.resources.requirements if value.item_id not in changed_items)
        != tuple(value for value in common.resources.requirements if value.item_id not in changed_items)
    ):
        raise MutationContractError(
            "A resource-requirement edit may only add definitions and replace requirements for scope-edited items."
        )


def _carrier_after(mutation: StoredStateMutation, common: StoredWorkState) -> StoredWorkState:
    supplied = mutation.after
    match mutation:
        case ProposalCreationMutation():
            _validate_proposal_creation(common, supplied)
            return replace(common, proposals=supplied.proposals)
        case DependencyEditMutation():
            _validate_dependency_edit(common, supplied)
            return replace(
                common,
                lifecycle=replace(
                    common.lifecycle,
                    work_items=supplied.lifecycle.work_items,
                    scope_revisions=supplied.lifecycle.scope_revisions,
                    dependencies=supplied.lifecycle.dependencies,
                ),
            )
        case ResourceRequirementEditMutation():
            _validate_requirement_edit(common, supplied)
            return replace(
                common,
                lifecycle=replace(
                    common.lifecycle,
                    work_items=supplied.lifecycle.work_items,
                    scope_revisions=supplied.lifecycle.scope_revisions,
                ),
                resources=replace(
                    common.resources,
                    definitions=supplied.resources.definitions,
                    requirements=supplied.resources.requirements,
                ),
            )
        case CoordinationAuthorityMutation():
            if supplied.authority.coordination == common.authority.coordination:
                raise MutationContractError("A coordination-authority carrier must change the coordination lease.")
            return replace(common, authority=replace(common.authority, coordination=supplied.authority.coordination))
        case AttemptAuthorityMutation():
            if (
                supplied.authority.attempt_counters,
                supplied.authority.attempt_generations,
                supplied.authority.attempt_leases,
            ) == (
                common.authority.attempt_counters,
                common.authority.attempt_generations,
                common.authority.attempt_leases,
            ):
                raise MutationContractError("An attempt-authority carrier must change attempt lease records.")
            return replace(
                common,
                authority=replace(
                    common.authority,
                    attempt_counters=supplied.authority.attempt_counters,
                    attempt_generations=supplied.authority.attempt_generations,
                    attempt_leases=supplied.authority.attempt_leases,
                ),
            )
        case ReservationTaskUseMutation():
            if (
                supplied.resources.reservation_counters,
                supplied.resources.reservations,
                supplied.resources.use_leases,
            ) == (
                common.resources.reservation_counters,
                common.resources.reservations,
                common.resources.use_leases,
            ):
                raise MutationContractError("A reservation/task-use carrier must change owned resource authority.")
            return replace(
                common,
                resources=replace(
                    common.resources,
                    reservation_counters=supplied.resources.reservation_counters,
                    reservations=supplied.resources.reservations,
                    use_leases=supplied.resources.use_leases,
                ),
            )
        case PlanningImpactMutation() | PlanningResolutionMutation() | ResourceMutation() | ResourceIntentMutation():
            raise MutationContractError("A pure decision cannot be committed as a carrier mutation.")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_scope_edit_items(common: StoredWorkState, supplied: StoredWorkState) -> set[ItemId]:
    before_items = common.lifecycle.work_items
    after_items = supplied.lifecycle.work_items
    if len(before_items) != len(after_items):
        raise MutationContractError("A scope edit cannot create or remove work items.")
    changed_items = set()
    for before, after in zip(before_items, after_items, strict=True):
        if before.item_id != after.item_id:
            raise MutationContractError("A scope edit cannot reorder work items.")
        if before == after:
            continue
        if (
            replace(
                after,
                scope_revision=before.scope_revision,
                scope_digest=before.scope_digest,
                subject_revision=before.subject_revision,
                origin_updated_at=before.origin_updated_at,
                updated_at=before.updated_at,
            )
            != before
        ):
            raise MutationContractError("A scope edit changed unrelated work-item facts.")
        changed_items.add(before.item_id)
    before_scopes = set(common.lifecycle.scope_revisions)
    after_scopes = set(supplied.lifecycle.scope_revisions)
    if (
        supplied.lifecycle.scope_revisions[: len(common.lifecycle.scope_revisions)] != common.lifecycle.scope_revisions
        or not before_scopes.issubset(after_scopes)
        or any(value.item_id not in changed_items for value in after_scopes - before_scopes)
    ):
        raise MutationContractError("A scope edit may only append scope anchors for changed items.")
    return changed_items


def _planning_impact_after(mutation: PlanningImpactMutation, common: StoredWorkState) -> StoredWorkState:
    impact = mutation.impact
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
        lifecycle=_planning_lifecycle_after(mutation, common),
        planning=PlanningRecords(
            common.planning.impacts,
            obligations,
            tuple(replacements),
        ),
    )
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


def _resource_after(mutation: ResourceMutation, common: StoredWorkState) -> StoredWorkState:
    if not mutation.decision.changes and not mutation.decision.counter_changes:
        raise MutationContractError("A resource decision must change a reservation or generation counter.")
    resources = common.resources
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
                    mutation.receipt.decided_at,
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
                else mutation.receipt.decided_at
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
    return replace(
        common,
        resources=replace(resources, reservations=tuple(reservations), reservation_counters=tuple(counters)),
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
            instances[index], subject_revision=change.after, updated_at=mutation.receipt.decided_at
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
            use_leases.append(_mutation_use(change.after, mutation.receipt.decided_at))
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
        reservations[index] = _mutation_reservation(change.after, reservations[index], mutation.receipt.decided_at)
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
            ProposalCreationMutation()
            | DependencyEditMutation()
            | ResourceRequirementEditMutation()
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
