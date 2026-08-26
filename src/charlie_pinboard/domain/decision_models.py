from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    ItemId,
    LeaseId,
    ProposalId,
    SubjectId,
)
from charlie_pinboard.domain.work_models import (
    AcceptCheckpointInput,
    AcceptProposalInput,
    ActivateInput,
    AttemptAuthority,
    AttemptState,
    BlockInput,
    CloseInput,
    CommandAttemptAuthority,
    CoordinationLeaseAuthority,
    DeferInput,
    EvidenceInput,
    MergeProposalInput,
    ProposalDispositionKind,
    ReasonInput,
    ResumeInput,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
    WorkState,
)


class ActionKind(Enum):
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
    REJECT_PROPOSAL = "reject-proposal"
    REOPEN = "reopen"
    REPORT_BLOCKER = "report-blocker"
    RESUME = "resume"
    RETURN_FOR_CORRECTION = "return-for-correction"
    RETURN_PROPOSAL = "return-proposal"
    SUBMIT_REVIEW = "submit-review"
    TRANSFER_COORDINATOR = "transfer-coordinator"

class AuthorizationKind(Enum):
    COORDINATOR = "coordinator"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    OBSERVER = "observer"

class Role(Enum):
    COORDINATOR = "coordinator"
    WORKER = "worker"
    OBSERVER = "observer"

@dataclass(frozen=True, slots=True)
class Action:
    action_id: ActionId
    kind: ActionKind
    subject: SubjectId
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: LeaseId | None = None
    command_authority: CommandAttemptAuthority | None = None

@dataclass(frozen=True, slots=True)
class ActionCapability:
    subject: SubjectId
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None
    authorization: AuthorizationKind
    lease_id: LeaseId | None
    command_authority: CommandAttemptAuthority | None

@dataclass(frozen=True, slots=True)
class AcceptCheckpointCommand:
    capability: ActionCapability
    value: AcceptCheckpointInput

@dataclass(frozen=True, slots=True)
class ActivateCommand:
    capability: ActionCapability
    value: ActivateInput

@dataclass(frozen=True, slots=True)
class PauseCommand:
    capability: ActionCapability
    value: ReasonInput

@dataclass(frozen=True, slots=True)
class BlockCommand:
    capability: ActionCapability
    value: BlockInput

@dataclass(frozen=True, slots=True)
class CompleteCommand:
    capability: ActionCapability
    value: EvidenceInput

@dataclass(frozen=True, slots=True)
class CloseCommand:
    capability: ActionCapability
    value: CloseInput

@dataclass(frozen=True, slots=True)
class ResumeCommand:
    capability: ActionCapability
    value: ResumeInput

@dataclass(frozen=True, slots=True)
class SubmitReviewCommand:
    capability: ActionCapability
    value: SubmitReviewInput

@dataclass(frozen=True, slots=True)
class ReturnForCorrectionCommand:
    capability: ActionCapability
    value: ReasonInput

@dataclass(frozen=True, slots=True)
class ReopenCommand:
    capability: ActionCapability
    value: EvidenceInput

@dataclass(frozen=True, slots=True)
class MarkReadyCommand:
    capability: ActionCapability
    value: ReasonInput

@dataclass(frozen=True, slots=True)
class BlockItemCommand:
    capability: ActionCapability
    value: BlockInput

@dataclass(frozen=True, slots=True)
class DeferCommand:
    capability: ActionCapability
    value: DeferInput

@dataclass(frozen=True, slots=True)
class AcceptProposalCommand:
    capability: ActionCapability
    value: AcceptProposalInput

@dataclass(frozen=True, slots=True)
class MergeProposalCommand:
    capability: ActionCapability
    value: MergeProposalInput

@dataclass(frozen=True, slots=True)
class ReturnProposalCommand:
    capability: ActionCapability
    value: ReasonInput

@dataclass(frozen=True, slots=True)
class RejectProposalCommand:
    capability: ActionCapability
    value: ReasonInput

@dataclass(frozen=True, slots=True)
class TransferCoordinatorCommand:
    capability: ActionCapability
    value: TransferCoordinatorInput


type TransitionCommand = (
    AcceptCheckpointCommand
    | ActivateCommand
    | PauseCommand
    | BlockCommand
    | CompleteCommand
    | CloseCommand
    | ResumeCommand
    | SubmitReviewCommand
    | ReturnForCorrectionCommand
    | ReopenCommand
    | MarkReadyCommand
    | BlockItemCommand
    | DeferCommand
    | AcceptProposalCommand
    | MergeProposalCommand
    | ReturnProposalCommand
    | RejectProposalCommand
    | TransferCoordinatorCommand
)


@dataclass(frozen=True, slots=True)
class ActorAuthority:
    role: Role
    authorization: AuthorizationKind
    generation: int
    lease_id: LeaseId | None = None
    attempts: tuple[AttemptId, ...] = ()
    revision_scoped: bool = True

@dataclass(frozen=True, slots=True)
class ItemChange:
    item: ItemId
    before: WorkState | None
    after: WorkState | None
    attempt: AttemptId | None = None
    outcome_evidence: str | None = None

@dataclass(frozen=True, slots=True)
class AttemptChange:
    attempt: AttemptId
    before: AttemptState | None
    after: AttemptState | None
    brief_artifact_ref_id: ArtifactRefId | None = None
    protected_candidate_before: CandidateId | None = None
    protected_candidate_after: CandidateId | None = None
    candidate_observed_at: datetime | None = None
    branch: str | None = None
    base_revision: str | None = None
    owner: str | None = None

@dataclass(frozen=True, slots=True)
class AcceptedProposalItem:
    item: ItemId
    state: WorkState
    timing: Timing | None
    next_action: str
    dependencies: tuple[ItemId, ...]
    user_label: str
    source: str
    trigger: str
    why_it_matters: str
    effect: str
    unlock: str
    notes: str
    scope_digest: str

@dataclass(frozen=True, slots=True)
class ProposalChange:
    proposal: ProposalId
    disposition: ProposalDispositionKind
    target_item: ItemId | None
    reason: str | None
    disposed_at: datetime
    accepted_item: AcceptedProposalItem | None = None

@dataclass(frozen=True, slots=True)
class AttemptAuthorityChange:
    before: AttemptAuthority
    after: AttemptAuthority

@dataclass(frozen=True, slots=True)
class CoordinatorAuthorityChange:
    before: CoordinationLeaseAuthority
    after: CoordinationLeaseAuthority

@dataclass(frozen=True, slots=True)
class CheckpointAcceptanceChange:
    checkpoint: CheckpointId
    attempt: AttemptId
    candidate: CandidateId
    evidence: str
    accepted_at: datetime

@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    action_id: ActionId
    item: ItemId | None
    outcome: str
    evidence: str | None
    decided_at: datetime

@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    item_change: ItemChange | None
    attempt_change: AttemptChange | None
    receipt: TransitionReceipt
    attempt_authority_change: AttemptAuthorityChange | None = None
    checkpoint_acceptance_change: CheckpointAcceptanceChange | None = None
    proposal_change: ProposalChange | None = None
    coordinator_authority_change: CoordinatorAuthorityChange | None = None
