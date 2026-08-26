from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from charlie_pinboard.domain.authority_decisions import AttemptLeaseStatus
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    CanonicalJson,
    CoordinationLeaseStatus,
    ProposalDispositionKind,
    ProposalRelationKind,
    Timing,
)


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
    MARK_READY = "mark-ready"
    MERGE_PROPOSAL = "merge-proposal"
    PAUSE = "pause"
    PORTABLE_COPY = "portable-copy"
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
    PORTABLE_COPY = "portable-copy"


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
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredProposal:
    proposal_id: ProposalId
    created_at: datetime
    recorded_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation: ProposalRelationKind
    relation_item_id: ItemId | None
    effect: str
    unlock: str
    urgency_evidence: str
    disposition: ProposalDispositionKind | None
    disposition_target_item_id: ItemId | None
    disposition_reason: str | None
    subject_revision: int
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
class StoredCoordinationLease:
    lease_id: LeaseId
    task_id: TaskId
    host_id: HostId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: CoordinationLeaseStatus


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
    state: AttemptLeaseStatus


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
class AuthorityRecords:
    coordination: StoredCoordinationLease | None = None
    attempt_counters: tuple[AttemptLeaseCounter, ...] = ()
    attempt_generations: tuple[AttemptLeaseGeneration, ...] = ()
    attempt_leases: tuple[StoredAttemptLease, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredWorkState:
    lifecycle: LifecycleRecords
    proposals: ProposalRecords
    artifact_references: tuple[ArtifactReference, ...]
    authority: AuthorityRecords
    transition_receipts: tuple[StoredTransitionReceipt, ...]
    focus: StoredFocus
