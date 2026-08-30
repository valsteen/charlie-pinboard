from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, assert_never

from pinboard.domain import work_models
from pinboard.domain.authority_models import AttemptLeaseStatus
from pinboard.domain.identifiers import (
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


def live_work_state(value: StoredWorkItemState) -> work_models.WorkState | None:
    match value:
        case (
            StoredWorkItemState.INTAKE
            | StoredWorkItemState.READY
            | StoredWorkItemState.ACTIVE
            | StoredWorkItemState.PAUSED
            | StoredWorkItemState.BLOCKED
            | StoredWorkItemState.DEFERRED
            | StoredWorkItemState.REVIEW
        ):
            return work_models.WorkState(value.value)
        case StoredWorkItemState.DONE | StoredWorkItemState.SUPERSEDED | StoredWorkItemState.DROPPED:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def stored_live_work_state(
    value: work_models.WorkState | work_models.AcceptedProposalState,
) -> StoredWorkItemState:
    match value:
        case (
            work_models.WorkState.INTAKE
            | work_models.WorkState.READY
            | work_models.WorkState.ACTIVE
            | work_models.WorkState.PAUSED
            | work_models.WorkState.BLOCKED
            | work_models.WorkState.DEFERRED
            | work_models.WorkState.REVIEW
            | work_models.AcceptedProposalState.INTAKE
            | work_models.AcceptedProposalState.READY
            | work_models.AcceptedProposalState.BLOCKED
            | work_models.AcceptedProposalState.DEFERRED
        ):
            return StoredWorkItemState(value.value)
        case _ as unreachable:
            assert_never(unreachable)


def stored_close_outcome(value: work_models.CloseOutcome) -> StoredWorkItemState:
    match value:
        case work_models.CloseOutcome.DONE | work_models.CloseOutcome.DROPPED:
            return StoredWorkItemState(value.value)
        case _ as unreachable:
            assert_never(unreachable)


class TransitionHistoryActionKind(Enum):
    ACCEPT_CHECKPOINT = "accept-checkpoint"
    ACCEPT_REVIEW_AND_CONTINUE = "accept-review-and-continue"
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
    REJECT_PROPOSAL = "reject-proposal"
    REOPEN = "reopen"
    REPORT_BLOCKER = "report-blocker"
    RESUME = "resume"
    RETURN_FOR_CORRECTION = "return-for-correction"
    RETURN_PROPOSAL = "return-proposal"
    REVISE_ITEM = "revise-item"
    SUBMIT_REVIEW = "submit-review"
    TRANSFER_COORDINATOR = "transfer-coordinator"


class TransitionHistoryAuthorizationKind(Enum):
    COORDINATOR = "coordinator"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    application: Literal["pinboard"]
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
    state: StoredWorkItemState
    timing: work_models.Timing | None
    source: str | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str | None
    subject_revision: int
    recorded_at: datetime
    updated_at: datetime
    queue_position: int | None


@dataclass(frozen=True, slots=True)
class ItemDefinitionRevision:
    item_id: ItemId
    revision: int
    digest: str
    definition: work_models.WorkItemDefinition
    reason: str
    source_task_id: TaskId
    before_digest: str | None
    after_digest: str
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
    role: work_models.ArtifactRole
    position: int


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    provenance: str
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
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
    relation: work_models.ProposalRelation
    effect: str
    unlock: str
    urgency_evidence: str
    disposition: work_models.ProposalDisposition | None
    subject_revision: int


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
    state: work_models.CoordinationLeaseStatus


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
    input_payload: work_models.CanonicalJson
    outcome_schema: str
    outcome_payload: work_models.CanonicalJson
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class LifecycleRecords:
    project: ProjectRecord
    work_items: tuple[StoredWorkItem, ...] = ()
    dependencies: tuple[ItemDependency, ...] = ()
    item_artifacts: tuple[ItemArtifactLink, ...] = ()
    attempts: tuple[StoredAttempt, ...] = ()
    definition_revisions: tuple[ItemDefinitionRevision, ...] = ()


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
