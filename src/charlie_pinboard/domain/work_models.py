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
    ProposalId,
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


class ProposalDispositionKind(Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    RETURNED = "returned"
    REJECTED = "rejected"


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
class ItemScope:
    item_id: ItemId
    user_label: str
    trigger: str | None
    why_it_matters: str | None
    effect: str | None
    unlock: str | None
    dependencies: tuple[ScopeDependency, ...] = ()
    artifacts: tuple[ScopeArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeAnchor:
    item: ItemId
    revision: int
    digest: str
    scope: ItemScope


class CoordinationLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"


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


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    attempt: AttemptId
    item: ItemId
    lease_id: LeaseId | None
    generation: int


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



