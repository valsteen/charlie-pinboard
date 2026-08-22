from dataclasses import dataclass

from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.decisions import Decision, TransitionReceipt
from charlie_pinboard.domain.model import PlanningImpact
from charlie_pinboard.domain.planning_decisions import PlanningResolutionDecision
from charlie_pinboard.domain.resource_decisions import ResourceDecision, ResourceIntentDecision


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Carrier constructed by Task 3 after proposal intake legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class DependencyEditMutation:
    """Carrier constructed by Task 3 after dependency-edit legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceRequirementEditMutation:
    """Carrier constructed by Task 3 after requirement-edit legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Carrier constructed by Task 3 after coordination authority legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Carrier constructed by Task 3 after attempt authority legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class ReservationTaskUseMutation:
    """Carrier constructed by Task 3 after reservation or task-use legality accepts exact records."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class PlanningImpactMutation:
    impact: PlanningImpact
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class PlanningResolutionMutation:
    decision: PlanningResolutionDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceMutation:
    decision: ResourceDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


@dataclass(frozen=True, slots=True)
class ResourceIntentMutation:
    decision: ResourceIntentDecision
    before: StoredWorkState
    after: StoredWorkState
    receipt: TransitionReceipt


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
