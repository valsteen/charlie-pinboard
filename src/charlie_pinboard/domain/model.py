from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, Literal, NewType

from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
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
    brief_artifact_ref_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class LegacyActivateInput:
    attempt: AttemptId
    branch: str
    base_revision: str
    owner: str


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
    | ActivateInput
    | LegacyActivateInput
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
    discovery_kind: str = ""
    discovery_fingerprint: str = ""


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
class ResourceObservation:
    instance_id: ResourceInstanceId
    host_id: HostId
    locator_schema: str
    locator: CanonicalJson
    generation: int
    digest: str
    observed_at: datetime


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
class ResourceMutationCapability:
    resource_id: ResourceId
    reservation_id: ReservationId
    reservation_generation: int
    instance_id: ResourceInstanceId
    instance_subject_revision: int
    locator_observation_generation: int
    locator_observation_digest: str
    task_use_lease_id: LeaseId
    task_use_generation: int
    task_id: TaskId
    host_id: HostId
    host_epoch: int
    attempt_lease_id: LeaseId
    attempt_lease_generation: int


class MutationIntentState(Enum):
    PLANNED = "planned"
    ACCEPTED = "accepted"
    RECONCILED = "reconciled"
    HUMAN_PRESERVED = "human-preserved"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class ResourceIntentCapability:
    resource: ResourceMutationCapability
    intent_id: MutationIntentId
    policy_digest: str
    state: MutationIntentState


@dataclass(frozen=True, slots=True)
class MutationReservation:
    reservation_id: ReservationId
    instance_id: ResourceInstanceId
    resource_id: ResourceId
    host_id: HostId
    acquisition_generation: int
    attempt_id: AttemptId
    item_id: ItemId
    state: ReservationState
    subject_revision: int


@dataclass(frozen=True, slots=True)
class MutationUseLease:
    reservation_id: ReservationId
    instance_id: ResourceInstanceId
    reservation_generation: int
    attempt_id: AttemptId
    host_id: HostId
    instance_subject_revision: int
    observation_generation: int
    observation_digest: str
    task_id: TaskId
    attempt_lease_id: LeaseId
    attempt_lease_generation: int
    lease_id: LeaseId
    generation: int
    generation_kind: UseLeaseGenerationKind
    host_epoch: int
    expires_at: datetime
    state: UseLeaseState


@dataclass(frozen=True, slots=True)
class MutationIntent:
    intent_id: MutationIntentId
    reservation_id: ReservationId
    reservation_generation: int
    instance_id: ResourceInstanceId
    attempt_id: AttemptId
    host_id: HostId
    resource_use_generation: int
    resource_use_lease_id: LeaseId
    task_id: TaskId
    attempt_lease_id: LeaseId
    attempt_lease_generation: int
    start_instance_subject_revision: int
    start_observation_generation: int
    start_observation_digest: str
    policy_schema: str
    policy: CanonicalJson
    policy_digest: str
    state: MutationIntentState
    recorded_at: datetime
    resolved_at: datetime | None = None
    result_observation_generation: int | None = None
    result_observation_digest: str | None = None
    evidence_schema: str | None = None
    evidence: CanonicalJson | None = None
    evidence_digest: str | None = None
    disposition_task_id: TaskId | None = None
    disposition_reason: str | None = None


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
    resource_definitions: tuple[ResourceDefinition, ...] = ()
    resource_instances: tuple[ResourceInstance, ...] = ()
    resource_reservation_counters: tuple[ResourceReservationCounter, ...] = ()
    resource_reservations: tuple[ResourceReservation, ...] = ()
    resource_use_leases: tuple[ResourceUseLease, ...] = ()
    resource_observations: tuple[ResourceObservation, ...] = ()
    mutation_reservations: tuple[MutationReservation, ...] = ()
    mutation_use_leases: tuple[MutationUseLease, ...] = ()
    mutation_intents: tuple[MutationIntent, ...] = ()
    host_epoch: int = 0
    focus_item: ItemId | None = None
    focus_attempt: AttemptId | None = None
    can_transfer_coordinator: bool = False

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

    def resource_definition(self, resource_id: ResourceId) -> ResourceDefinition | None:
        return next(
            (definition for definition in self.resource_definitions if definition.resource_id == resource_id), None
        )

    def resource_instance(self, instance_id: ResourceInstanceId) -> ResourceInstance | None:
        return next((instance for instance in self.resource_instances if instance.instance_id == instance_id), None)

    def resource_reservation(self, reservation_id: ReservationId) -> ResourceReservation | None:
        return next(
            (reservation for reservation in self.resource_reservations if reservation.reservation_id == reservation_id),
            None,
        )

    def resource_reservation_counter(self, instance_id: ResourceInstanceId) -> ResourceReservationCounter | None:
        matches = tuple(counter for counter in self.resource_reservation_counters if counter.instance_id == instance_id)
        return matches[0] if len(matches) == 1 else None

    def resource_observation(self, instance_id: ResourceInstanceId) -> ResourceObservation | None:
        return next(
            (observation for observation in self.resource_observations if observation.instance_id == instance_id),
            None,
        )

    def mutation_reservation(self, reservation_id: ReservationId) -> MutationReservation | None:
        return next(
            (reservation for reservation in self.mutation_reservations if reservation.reservation_id == reservation_id),
            None,
        )

    def mutation_use_lease(self, lease_id: LeaseId, generation: int) -> MutationUseLease | None:
        return next(
            (
                use_lease
                for use_lease in self.mutation_use_leases
                if use_lease.lease_id == lease_id and use_lease.generation == generation
            ),
            None,
        )

    def mutation_intent(self, intent_id: MutationIntentId) -> MutationIntent | None:
        return next((intent for intent in self.mutation_intents if intent.intent_id == intent_id), None)

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
