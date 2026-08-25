from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import NewType

from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
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

CanonicalJson = NewType("CanonicalJson", bytes)


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


class ProposalRelationKind(Enum):
    INDEPENDENT = "independent"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow-up"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"


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
class ResumeInput:
    brief_artifact_ref_id: ArtifactRefId | None = None


@dataclass(frozen=True, slots=True)
class ActivateInput:
    attempt: AttemptId
    branch: str
    base_revision: str
    owner: str
    brief_artifact_ref_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class SubmitReviewInput:
    candidate: CandidateId


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
    resource_requirements: tuple[ResourceId, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeProposalInput:
    target: ItemId


@dataclass(frozen=True, slots=True)
class TransferCoordinatorInput:
    task_id: TaskId
    host_id: HostId


type TransitionInput = (
    EmptyInput
    | ResumeInput
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
    | SubmitReviewInput
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_ref_id: ArtifactRefId
    kind: str


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


class CoordinationLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"


class UseLeaseGenerationKind(Enum):
    GRANT = "grant"
    FENCE = "fence"


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: ReservationId
    resource_id: ResourceId
    instance_id: ResourceInstanceId
    attempt: AttemptId
    generation: int
    state: ReservationState


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
class CommandAttemptAuthority:
    host_epoch: int
    item: ItemId
    item_subject_revision: str
    attempt: AttemptId
    attempt_subject_revision: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CoordinationCommandAuthority:
    host_epoch: int
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CoordinationLeaseAuthority:
    host_epoch: int
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: CoordinationLeaseStatus


class MutationIntentState(Enum):
    PLANNED = "planned"
    ACCEPTED = "accepted"
    RECONCILED = "reconciled"
    HUMAN_PRESERVED = "human-preserved"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class AttemptTaskUse:
    reservation_id: ReservationId
    attempt_id: AttemptId
    generation: int
    generation_kind: UseLeaseGenerationKind
    state: UseLeaseState


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
    created_at: datetime | None = None
    source_task_id: TaskId | None = None
    user_label: str | None = None
    trigger: str | None = None
    why_it_matters: str | None = None
    relation: ProposalRelationKind | None = None
    relation_item_id: ItemId | None = None
    effect: str | None = None
    unlock: str | None = None
    urgency_evidence: str | None = None
    evidence: tuple[str, ...] = ()
    freshness: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: AttemptId
    item: ItemId
    state: AttemptState
    accepted_scope_revision: int | None = None
    accepted_scope_digest: str | None = None
    protected_candidate_revision: CandidateId | None = None
    brief_artifact_ref_id: ArtifactRefId | None = None


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
    artifacts: tuple[ArtifactRecord, ...] = ()
    proposals: tuple[ProposalRecord, ...] = ()
    subject_revisions: tuple[SubjectRevision, ...] = ()
    attempt_authorities: tuple[AttemptAuthority, ...] = ()
    command_attempt_authorities: tuple[CommandAttemptAuthority, ...] = ()
    coordination_authority: CoordinationCommandAuthority | None = None
    history_items: tuple[ItemId, ...] = ()
    scopes: tuple[ScopeAnchor, ...] = ()
    planning_impacts: tuple[PlanningImpact, ...] = ()
    declared_resources: tuple[ResourceId, ...] = ()
    resource_reservations: tuple[ResourceReservation, ...] = ()
    resource_use_leases: tuple[ResourceUseLease, ...] = ()
    attempt_task_uses: tuple[AttemptTaskUse, ...] = ()
    planned_mutation_attempts: tuple[AttemptId, ...] = ()
    host_epoch: int = 0
    focus_item: ItemId | None = None
    focus_attempt: AttemptId | None = None
    can_transfer_coordinator: bool = False
    coordination_lease: CoordinationLeaseAuthority | None = None

    def items_by_id(self) -> dict[ItemId, WorkItem]:
        return {item.item: item for item in self.items}

    def item(self, item_id: ItemId) -> WorkItem | None:
        return next((item for item in self.items if item.item == item_id), None)

    def item_for_attempt(self, attempt_id: AttemptId) -> WorkItem | None:
        return next((item for item in self.items if item.attempt == attempt_id), None)

    def attempts_by_id(self) -> dict[AttemptId, AttemptRecord]:
        return {attempt.attempt: attempt for attempt in self.attempts}

    def attempt(self, attempt_id: AttemptId) -> AttemptRecord | None:
        return next((attempt for attempt in self.attempts if attempt.attempt == attempt_id), None)

    def proposal(self, proposal_id: ProposalId) -> ProposalRecord | None:
        return next((proposal for proposal in self.proposals if proposal.proposal == proposal_id), None)

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
