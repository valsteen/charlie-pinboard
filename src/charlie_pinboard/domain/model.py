from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from charlie_pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    LeaseId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)

SCHEMA_V1: Final = "repo-work/v1"
SCHEMA_V2: Final = "repo-work/v2"
type SchemaV1 = Literal["repo-work/v1"]


class WorkState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"


class AttemptState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CLOSED = "closed"


class AcceptedProposalState(Enum):
    INTAKE = "intake"
    READY = "ready"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class CloseOutcome(Enum):
    DONE = "done"
    DROPPED = "dropped"


class Timing(Enum):
    MUST_NOW = "must-now"
    CHEAPER_NOW = "cheaper-now"
    SAFE_TO_DEFER = "safe-to-defer"


TERMINAL_STATES: Final = frozenset({"done", "superseded", "dropped"})


@dataclass(frozen=True, slots=True)
class WorkItem:
    item: ItemId
    state: WorkState
    timing: str | None
    depends_on: tuple[ItemId, ...]
    attempt: AttemptId | None
    source: str
    next_action: str | None
    notes: str
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class EmptyInput:
    pass


@dataclass(frozen=True, slots=True)
class ActivateInput:
    attempt: AttemptId
    branch: str
    base_revision: str
    owner: str


@dataclass(frozen=True, slots=True)
class ReasonInput:
    reason: str


@dataclass(frozen=True, slots=True)
class BlockInput:
    reason: str
    depends_on: tuple[ItemId, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    evidence: str


@dataclass(frozen=True, slots=True)
class AcceptCheckpointInput:
    checkpoint: CheckpointId
    candidate: CandidateId
    evidence: str


@dataclass(frozen=True, slots=True)
class CloseInput:
    outcome: CloseOutcome
    reason: str


@dataclass(frozen=True, slots=True)
class DeferInput:
    timing: Timing
    reopen_condition: str


@dataclass(frozen=True, slots=True)
class AcceptProposalInput:
    item: ItemId
    state: AcceptedProposalState
    next_action: str
    timing: Timing | None = None
    depends_on: tuple[ItemId, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeProposalInput:
    target: ItemId


@dataclass(frozen=True, slots=True)
class TransferCoordinatorInput:
    task_id: TaskId
    host_id: HostId


type TransitionInput = (
    EmptyInput
    | ActivateInput
    | ReasonInput
    | BlockInput
    | EvidenceInput
    | AcceptCheckpointInput
    | CloseInput
    | DeferInput
    | AcceptProposalInput
    | MergeProposalInput
    | TransferCoordinatorInput
)


class ArtifactRole(Enum):
    REQUIREMENTS = "requirements"
    PLAN = "plan"
    DESIGN = "design"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class ScopeArtifact:
    role: ArtifactRole
    position: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ScopeDependency:
    position: int
    dependency_id: ItemId


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    position: int
    resource_id: ResourceId


@dataclass(frozen=True, slots=True)
class ItemScope:
    item_id: ItemId
    user_label: str
    trigger: str | None
    why_it_matters: str | None
    effect: str | None
    unlock: str | None
    dependencies: tuple[ScopeDependency, ...] = ()
    resource_requirements: tuple[ResourceRequirement, ...] = ()
    artifacts: tuple[ScopeArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeAnchor:
    item: ItemId
    revision: int
    digest: str
    scope: ItemScope


class PlanningDisposition(Enum):
    UNCHANGED = "unchanged"
    REVISED = "revised"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    DROPPED = "dropped"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class PlanningObligation:
    target: ItemId
    position: int
    observed_scope_revision: int
    observed_scope_digest: str = ""
    disposition: PlanningDisposition | None = None
    evaluated_scope_revision: int | None = None
    evaluated_scope_digest: str | None = None
    resulting_scope_revision: int | None = None
    resulting_scope_digest: str | None = None
    replacements: tuple[ItemId, ...] = ()
    outcome_evidence: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningImpact:
    impact_id: PlanningImpactId
    source_item: ItemId
    source_attempt: AttemptId | None
    source_scope_revision: int
    source_scope_digest: str
    summary: str
    evidence: str
    obligations: tuple[PlanningObligation, ...]


class ReservationState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    REVOKED_PENDING_RECOVERY = "revoked-pending-recovery"


class UseLeaseState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class UseLeaseGenerationKind(Enum):
    GRANT = "grant"
    FENCE = "fence"


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    resource_id: ResourceId
    kind: str


@dataclass(frozen=True, slots=True)
class ResourceInstance:
    instance_id: ResourceInstanceId
    resource_id: ResourceId
    host_id: HostId
    subject_revision: int


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: ReservationId
    resource_id: ResourceId
    instance_id: ResourceInstanceId
    attempt: AttemptId
    generation: int
    state: ReservationState


@dataclass(frozen=True, slots=True)
class ResourceReservationCounter:
    instance_id: ResourceInstanceId
    generation_high_water: int


@dataclass(frozen=True, slots=True)
class ResourceUseLease:
    lease_id: LeaseId
    reservation_id: ReservationId
    attempt_lease_id: LeaseId
    attempt_generation: int
    generation: int
    state: UseLeaseState
    generation_kind: UseLeaseGenerationKind = UseLeaseGenerationKind.GRANT


@dataclass(frozen=True, slots=True)
class ResourceAuthority:
    resource_id: ResourceId
    host_id: HostId
    lease_id: LeaseId
    generation: int


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    attempt: AttemptId
    item: ItemId
    lease_id: LeaseId | None
    generation: int
    resources: tuple[ResourceAuthority, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal: ProposalId
    revision: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: AttemptId
    item: ItemId
    state: AttemptState
    accepted_scope_revision: int | None = None
    accepted_scope_digest: str | None = None
    protected_candidate_revision: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectRevision:
    subject: ItemId | AttemptId | ProposalId
    revision: str


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    revision: str
    generation: int
    items: tuple[WorkItem, ...]
    attempts: tuple[AttemptRecord, ...] = ()
    proposals: tuple[ProposalRecord, ...] = ()
    subject_revisions: tuple[SubjectRevision, ...] = ()
    attempt_authorities: tuple[AttemptAuthority, ...] = ()
    history_items: tuple[ItemId, ...] = ()
    scopes: tuple[ScopeAnchor, ...] = ()
    planning_impacts: tuple[PlanningImpact, ...] = ()
    resource_definitions: tuple[ResourceDefinition, ...] = ()
    resource_instances: tuple[ResourceInstance, ...] = ()
    resource_reservation_counters: tuple[ResourceReservationCounter, ...] = ()
    resource_reservations: tuple[ResourceReservation, ...] = ()
    resource_use_leases: tuple[ResourceUseLease, ...] = ()
    host_epoch: int = 0
    focus_item: ItemId | None = None
    focus_attempt: AttemptId | None = None
    can_transfer_coordinator: bool = False

    def items_by_id(self) -> dict[ItemId, WorkItem]:
        return {item.item: item for item in self.items}

    def attempts_by_id(self) -> dict[AttemptId, AttemptRecord]:
        return {attempt.attempt: attempt for attempt in self.attempts}

    def proposal_revisions(self) -> dict[ProposalId, str]:
        return {proposal.proposal: proposal.revision for proposal in self.proposals}

    def subject_revision(self, subject: ItemId | AttemptId | ProposalId) -> str | None:
        return next((value.revision for value in self.subject_revisions if value.subject == subject), None)

    def authority_for(self, attempt: AttemptId, lease_id: LeaseId | None, generation: int) -> AttemptAuthority | None:
        return next(
            (
                authority
                for authority in self.attempt_authorities
                if authority.attempt == attempt
                and authority.lease_id == lease_id
                and authority.generation == generation
            ),
            None,
        )
