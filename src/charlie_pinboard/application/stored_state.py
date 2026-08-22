from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
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
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    CanonicalJson,
    MutationIntentState,
    PlanningDisposition,
    ReservationState,
    Timing,
    UseLeaseGenerationKind,
    UseLeaseState,
)


class OriginKind(Enum):
    NATIVE = "native"
    LEGACY_IMPORT = "legacy-import"


class ArtifactKind(Enum):
    REQUIREMENTS = "requirements"
    PLAN = "plan"
    DESIGN = "design"
    BRIEF = "brief"
    RESULT = "result"
    BLOCKER = "blocker"
    EVIDENCE = "evidence"


class StoredWorkItemState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"
    DONE = "done"
    SUPERSEDED = "superseded"
    DROPPED = "dropped"


class PlanningObligationState(Enum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class ProposalRelation(Enum):
    INDEPENDENT = "independent"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow-up"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"


class ProposalDisposition(Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    RETURNED = "returned"
    REJECTED = "rejected"


class ResourceInstanceState(Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class CoordinationLeaseState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"


class AttemptLeaseState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class TransitionHistoryActionKind(Enum):
    ACCEPT_CHECKPOINT = "accept-checkpoint"
    ACCEPT_PROPOSAL = "accept-proposal"
    ACTIVATE = "activate"
    BLOCK = "block"
    BLOCK_ITEM = "block-item"
    COMPLETE = "complete"
    CLOSE = "close"
    CONTINUE = "continue"
    DEFER = "defer"
    DISPATCH = "dispatch"
    INSPECT = "inspect"
    LEGACY_CLEANUP = "legacy-cleanup"
    LEGACY_IMPORT = "legacy-import"
    MARK_READY = "mark-ready"
    MERGE_PROPOSAL = "merge-proposal"
    PAUSE = "pause"
    REJECT_PROPOSAL = "reject-proposal"
    REOPEN = "reopen"
    REPORT_BLOCKER = "report-blocker"
    RESUME = "resume"
    RETURN_FOR_CORRECTION = "return-for-correction"
    RETURN_PROPOSAL = "return-proposal"
    SUBMIT_REVIEW = "submit-review"
    TRANSFER_COORDINATOR = "transfer-coordinator"


class TransitionHistoryAuthorizationKind(Enum):
    COORDINATOR = "coordinator"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    OBSERVER = "observer"
    MIGRATION = "migration"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    application: Literal["charlie-pinboard"]
    schema_version: Literal[1]
    revision: int
    host_epoch: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_ref_id: ArtifactRefId
    key: str
    revision: int
    kind: ArtifactKind
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredWorkItem:
    item_id: ItemId
    origin: OriginKind
    user_label: str
    state: StoredWorkItemState
    timing: Timing | None
    source: str | None
    trigger: str | None
    why_it_matters: str | None
    effect: str | None
    unlock: str | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str | None
    scope_revision: int
    scope_digest: str
    subject_revision: int
    origin_created_at: datetime | None
    origin_updated_at: datetime | None
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ItemScopeRevision:
    item_id: ItemId
    revision: int
    digest: str
    accepted_project_revision: int
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class ItemDependency:
    item_id: ItemId
    dependency_id: ItemId
    position: int


@dataclass(frozen=True, slots=True)
class ItemArtifactLink:
    item_id: ItemId
    artifact_ref_id: ArtifactRefId
    role: ArtifactRole
    position: int


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    attempt_id: AttemptId
    item_id: ItemId
    origin: OriginKind
    state: AttemptState
    branch: str
    base_revision: str
    provenance: str
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
    blocker_artifact_ref_id: ArtifactRefId | None
    candidate_revision: str | None
    candidate_recorded_at: datetime | None
    accepted_scope_revision: int
    accepted_scope_digest: str
    subject_revision: int
    origin_created_at: datetime | None
    origin_updated_at: datetime | None
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPlanningImpact:
    impact_id: PlanningImpactId
    source_item_id: ItemId
    source_attempt_id: AttemptId | None
    source_scope_revision: int
    source_scope_digest: str
    primary_target_item_id: ItemId
    summary: str
    evidence: str
    recorded_project_revision: int
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPlanningObligation:
    impact_id: PlanningImpactId
    target_item_id: ItemId
    position: int
    observed_scope_revision: int
    observed_scope_digest: str
    state: PlanningObligationState
    disposition: PlanningDisposition | None
    evaluated_scope_revision: int | None
    evaluated_scope_digest: str | None
    resulting_scope_revision: int | None
    resulting_scope_digest: str | None
    primary_replacement_item_id: ItemId | None
    outcome_evidence: str | None
    reason: str | None
    resolved_project_revision: int | None
    recorded_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredPlanningReplacement:
    impact_id: PlanningImpactId
    target_item_id: ItemId
    replacement_item_id: ItemId
    position: int


@dataclass(frozen=True, slots=True)
class StoredProposal:
    proposal_id: ProposalId
    origin: OriginKind
    created_at: datetime
    recorded_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation: ProposalRelation
    relation_item_id: ItemId | None
    effect: str
    unlock: str
    urgency_evidence: str
    disposition: ProposalDisposition | None
    disposition_target_item_id: ItemId | None
    disposition_reason: str | None
    subject_revision: int
    origin_disposed_at: datetime | None
    disposition_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProposalEvidence:
    proposal_id: ProposalId
    position: int
    selector: str


@dataclass(frozen=True, slots=True)
class ProposalFreshness:
    proposal_id: ProposalId
    position: int
    assumption: str


@dataclass(frozen=True, slots=True)
class StoredResourceDefinition:
    resource_id: ResourceId
    origin: OriginKind
    kind: str
    description: str
    subject_revision: int
    origin_created_at: datetime | None
    origin_updated_at: datetime | None
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ItemResourceRequirement:
    item_id: ItemId
    resource_id: ResourceId
    position: int


@dataclass(frozen=True, slots=True)
class StoredResourceInstance:
    instance_id: ResourceInstanceId
    resource_id: ResourceId
    host_id: HostId
    discovery_kind: str
    discovery_fingerprint: str
    state: ResourceInstanceState
    subject_revision: int
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceInstanceLocator:
    instance_id: ResourceInstanceId
    host_id: HostId
    locator_schema: str
    locator: CanonicalJson
    observation_generation: int
    observation_digest: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredReservationCounter:
    instance_id: ResourceInstanceId
    generation_high_water: int


@dataclass(frozen=True, slots=True)
class StoredResourceReservation:
    reservation_id: ReservationId
    instance_id: ResourceInstanceId
    resource_id: ResourceId
    host_id: HostId
    acquisition_generation: int
    attempt_id: AttemptId
    item_id: ItemId
    state: ReservationState
    subject_revision: int
    created_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredCoordinationLease:
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: CoordinationLeaseState


@dataclass(frozen=True, slots=True)
class AttemptLeaseCounter:
    attempt_id: AttemptId
    generation_high_water: int


@dataclass(frozen=True, slots=True)
class AttemptLeaseGeneration:
    attempt_id: AttemptId
    generation: int
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId


@dataclass(frozen=True, slots=True)
class StoredAttemptLease:
    attempt_id: AttemptId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: AttemptLeaseState


@dataclass(frozen=True, slots=True)
class StoredResourceUseLease:
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
    acquired_at: datetime
    expires_at: datetime
    state: UseLeaseState


@dataclass(frozen=True, slots=True)
class ResourceMutationIntent:
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
    resolved_at: datetime | None
    result_observation_generation: int | None
    result_observation_digest: str | None
    evidence_schema: str | None
    evidence: CanonicalJson | None
    evidence_digest: str | None
    disposition_task_id: TaskId | None
    disposition_reason: str | None


@dataclass(frozen=True, slots=True)
class StoredFocus:
    item_id: ItemId | None
    attempt_id: AttemptId | None
    next_action: str
    subject_revision: int


@dataclass(frozen=True, slots=True)
class StoredTransitionReceipt:
    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: CanonicalJson
    outcome_schema: str
    outcome_payload: CanonicalJson
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class LifecycleRecords:
    project: ProjectRecord
    work_items: tuple[StoredWorkItem, ...] = ()
    scope_revisions: tuple[ItemScopeRevision, ...] = ()
    dependencies: tuple[ItemDependency, ...] = ()
    item_artifacts: tuple[ItemArtifactLink, ...] = ()
    attempts: tuple[StoredAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalRecords:
    proposals: tuple[StoredProposal, ...] = ()
    evidence: tuple[ProposalEvidence, ...] = ()
    freshness: tuple[ProposalFreshness, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningRecords:
    impacts: tuple[StoredPlanningImpact, ...] = ()
    obligations: tuple[StoredPlanningObligation, ...] = ()
    replacements: tuple[StoredPlanningReplacement, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactRecords:
    references: tuple[ArtifactReference, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorityRecords:
    coordination: StoredCoordinationLease | None = None
    attempt_counters: tuple[AttemptLeaseCounter, ...] = ()
    attempt_generations: tuple[AttemptLeaseGeneration, ...] = ()
    attempt_leases: tuple[StoredAttemptLease, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceRecords:
    definitions: tuple[StoredResourceDefinition, ...] = ()
    requirements: tuple[ItemResourceRequirement, ...] = ()
    instances: tuple[StoredResourceInstance, ...] = ()
    locators: tuple[ResourceInstanceLocator, ...] = ()
    reservation_counters: tuple[StoredReservationCounter, ...] = ()
    reservations: tuple[StoredResourceReservation, ...] = ()
    use_leases: tuple[StoredResourceUseLease, ...] = ()
    mutation_intents: tuple[ResourceMutationIntent, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoryRecords:
    receipts: tuple[StoredTransitionReceipt, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredWorkState:
    lifecycle: LifecycleRecords
    proposals: ProposalRecords
    planning: PlanningRecords
    artifacts: ArtifactRecords
    authority: AuthorityRecords
    resources: ResourceRecords
    history: HistoryRecords
    focus: StoredFocus
