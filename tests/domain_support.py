from dataclasses import replace as dataclass_replace
from typing import Any, cast  # noqa: TID251 - fixture corruption intentionally crosses the typed boundary

from charlie_pinboard.domain import history
from charlie_pinboard.domain.decisions import Action as ActionValue
from charlie_pinboard.domain.decisions import ActionKind
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    LedgerId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AttemptState,
    ReservationState,
    ScopeArtifact,
    Timing,
    UseLeaseGenerationKind,
    UseLeaseState,
)
from charlie_pinboard.domain.model import (
    AcceptProposalInput as AcceptProposalInputValue,
)
from charlie_pinboard.domain.model import (
    AttemptAuthority as AttemptAuthorityValue,
)
from charlie_pinboard.domain.model import (
    AttemptRecord as AttemptRecordValue,
)
from charlie_pinboard.domain.model import (
    DeferInput as DeferInputValue,
)
from charlie_pinboard.domain.model import (
    ItemScope as ItemScopeValue,
)
from charlie_pinboard.domain.model import (
    PlanningImpact as PlanningImpactValue,
)
from charlie_pinboard.domain.model import (
    PlanningObligation as PlanningObligationValue,
)
from charlie_pinboard.domain.model import (
    ProposalRecord as ProposalRecordValue,
)
from charlie_pinboard.domain.model import (
    ResourceAuthority as ResourceAuthorityValue,
)
from charlie_pinboard.domain.model import (
    ResourceDefinition as ResourceDefinitionValue,
)
from charlie_pinboard.domain.model import (
    ResourceInstance as ResourceInstanceValue,
)
from charlie_pinboard.domain.model import (
    ResourceRequirement as ResourceRequirementValue,
)
from charlie_pinboard.domain.model import (
    ResourceReservation as ResourceReservationValue,
)
from charlie_pinboard.domain.model import (
    ResourceReservationCounter as ResourceReservationCounterValue,
)
from charlie_pinboard.domain.model import (
    ResourceUseLease as ResourceUseLeaseValue,
)
from charlie_pinboard.domain.model import (
    ScopeAnchor as ScopeAnchorValue,
)
from charlie_pinboard.domain.model import (
    ScopeDependency as ScopeDependencyValue,
)
from charlie_pinboard.domain.model import (
    TransferCoordinatorInput as TransferCoordinatorInputValue,
)
from charlie_pinboard.domain.resource_decisions import ResourceToken as ResourceTokenValue


def replace(instance: Any, **changes: Any) -> Any:  # noqa: ANN401
    """Create valid variants and deliberately malformed values for rejection tests."""
    return dataclass_replace(instance, **changes)


def action(kind: ActionKind, subject: str) -> ActionValue:
    if kind in {
        ActionKind.ACCEPT_CHECKPOINT,
        ActionKind.BLOCK,
        ActionKind.COMPLETE,
        ActionKind.CONTINUE,
        ActionKind.DISPATCH,
        ActionKind.PAUSE,
        ActionKind.REPORT_BLOCKER,
        ActionKind.RETURN_FOR_CORRECTION,
        ActionKind.SUBMIT_REVIEW,
    }:
        subject_id = AttemptId(subject)
    elif kind in {
        ActionKind.ACCEPT_PROPOSAL,
        ActionKind.MERGE_PROPOSAL,
        ActionKind.REJECT_PROPOSAL,
        ActionKind.RETURN_PROPOSAL,
    }:
        subject_id = ProposalId(subject)
    elif kind in {ActionKind.INSPECT, ActionKind.TRANSFER_COORDINATOR}:
        subject_id = LedgerId(subject)
    else:
        subject_id = ItemId(subject)
    return ActionValue(ActionId(f"{kind.value}:{subject}"), kind, subject_id, kind.value, "rev", 1)


def item_scope(
    item_id: str,
    user_label: str,
    trigger: str | None,
    why_it_matters: str | None,
    effect: str | None,
    unlock: str | None,
    dependencies: tuple[ScopeDependencyValue, ...] = (),
    resource_requirements: tuple[ResourceRequirementValue, ...] = (),
    artifacts: tuple[ScopeArtifact, ...] = (),
) -> ItemScopeValue:
    return ItemScopeValue(
        ItemId(item_id),
        user_label,
        trigger,
        why_it_matters,
        effect,
        unlock,
        dependencies,
        resource_requirements,
        artifacts,
    )


def scope_dependency(position: int, dependency_id: str) -> ScopeDependencyValue:
    return ScopeDependencyValue(position, ItemId(dependency_id))


def resource_requirement(position: int, resource_id: str) -> ResourceRequirementValue:
    return ResourceRequirementValue(position, ResourceId(resource_id))


def planning_obligation(
    target: str,
    position: int,
    observed_scope_revision: int,
    observed_scope_digest: str = "",
) -> PlanningObligationValue:
    return PlanningObligationValue(
        ItemId(target),
        position,
        observed_scope_revision,
        observed_scope_digest,
    )


def planning_impact(
    impact_id: str,
    source_item: str,
    source_attempt: str | None,
    source_scope_revision: int,
    source_scope_digest: str,
    summary: str,
    evidence: str,
    obligations: tuple[PlanningObligationValue, ...],
) -> PlanningImpactValue:
    return PlanningImpactValue(
        PlanningImpactId(impact_id),
        ItemId(source_item),
        AttemptId(source_attempt) if source_attempt is not None else None,
        source_scope_revision,
        source_scope_digest,
        summary,
        evidence,
        obligations,
    )


def attempt_record(
    attempt: str,
    item: str,
    state: AttemptState,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    protected_candidate_revision: str | None = None,
) -> AttemptRecordValue:
    return AttemptRecordValue(
        AttemptId(attempt),
        ItemId(item),
        state,
        accepted_scope_revision,
        accepted_scope_digest,
        CandidateId(protected_candidate_revision) if protected_candidate_revision is not None else None,
    )


def scope_anchor(item: str, revision: int, digest: str, scope: ItemScopeValue) -> ScopeAnchorValue:
    return ScopeAnchorValue(ItemId(item), revision, digest, scope)


def proposal_record(proposal: str, revision: str) -> ProposalRecordValue:
    return ProposalRecordValue(ProposalId(proposal), revision)


def resource_authority(resource_id: str, host_id: str, lease_id: str, generation: int) -> ResourceAuthorityValue:
    return ResourceAuthorityValue(ResourceId(resource_id), HostId(host_id), LeaseId(lease_id), generation)


def attempt_authority(
    attempt: str,
    item: str,
    lease_id: str | None,
    generation: int,
    resources: tuple[ResourceAuthorityValue, ...] = (),
) -> AttemptAuthorityValue:
    return AttemptAuthorityValue(
        AttemptId(attempt),
        ItemId(item),
        LeaseId(lease_id) if lease_id is not None else None,
        generation,
        resources,
    )


def resource_definition(resource_id: str, kind: str) -> ResourceDefinitionValue:
    return ResourceDefinitionValue(ResourceId(resource_id), kind)


def resource_instance(instance_id: str, resource_id: str, host_id: str, subject_revision: int) -> ResourceInstanceValue:
    return ResourceInstanceValue(
        ResourceInstanceId(instance_id),
        ResourceId(resource_id),
        HostId(host_id),
        subject_revision,
    )


def resource_reservation(
    reservation_id: str,
    resource_id: str,
    instance_id: str,
    attempt: str,
    generation: int,
    state: ReservationState,
) -> ResourceReservationValue:
    return ResourceReservationValue(
        ReservationId(reservation_id),
        ResourceId(resource_id),
        ResourceInstanceId(instance_id),
        AttemptId(attempt),
        generation,
        state,
    )


def resource_reservation_counter(instance_id: str, generation_high_water: int) -> ResourceReservationCounterValue:
    return ResourceReservationCounterValue(ResourceInstanceId(instance_id), generation_high_water)


def resource_use_lease(
    lease_id: str,
    reservation_id: str,
    attempt_lease_id: str,
    attempt_generation: int,
    generation: int,
    state: UseLeaseState,
    generation_kind: UseLeaseGenerationKind = UseLeaseGenerationKind.GRANT,
) -> ResourceUseLeaseValue:
    return ResourceUseLeaseValue(
        LeaseId(lease_id),
        ReservationId(reservation_id),
        LeaseId(attempt_lease_id),
        attempt_generation,
        generation,
        state,
        generation_kind,
    )


def resource_token(resource_id: str, host_id: str, lease_id: str, generation: int) -> ResourceTokenValue:
    return ResourceTokenValue(ResourceId(resource_id), HostId(host_id), LeaseId(lease_id), generation)


def accept_proposal_input(
    item: str,
    state: AcceptedProposalState,
    next_action: str,
    timing: Timing | None = None,
    depends_on: tuple[str, ...] = (),
) -> AcceptProposalInputValue:
    return AcceptProposalInputValue(
        ItemId(item), state, next_action, timing, tuple(ItemId(value) for value in depends_on)
    )


def defer_input(timing: str, reopen_condition: str) -> DeferInputValue:
    return DeferInputValue(Timing(timing), reopen_condition)


def transfer_coordinator_input(task_id: str, host_id: str) -> TransferCoordinatorInputValue:
    return TransferCoordinatorInputValue(TaskId(task_id), HostId(host_id))


def advance_scope(previous: ScopeAnchorValue | None, item: str, scope: ItemScopeValue) -> ScopeAnchorValue:
    digest = cast(str, history.item_scope_digest(scope))
    if previous is not None and previous.digest == digest:
        return previous
    return ScopeAnchorValue(ItemId(item), 1 if previous is None else previous.revision + 1, digest, scope)
