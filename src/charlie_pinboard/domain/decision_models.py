from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import assert_never

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
    AcceptReviewAndContinueInput,
    ActivateInput,
    AttemptAuthority,
    AttemptState,
    BlockInput,
    CloseInput,
    CloseOutcome,
    CommandAttemptAuthority,
    CoordinationLeaseAuthority,
    DeferInput,
    EvidenceInput,
    MergeProposalInput,
    ReasonInput,
    ResumeInput,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
    WorkState,
)


class ActionKind(Enum):
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


class ActionEffect(Enum):
    ADVISORY = "advisory"
    MUTATING = "mutating"


class ActionSubjectKind(Enum):
    ATTEMPT = "attempt"
    ITEM = "item"


class ActionLifecyclePrecondition(Enum):
    ACTIVE_ATTEMPT = "active-attempt"
    INTAKE_ITEM = "intake-item"


@dataclass(frozen=True, slots=True)
class BlockerActionDescriptor:
    effect: ActionEffect
    required_role: Role
    subject_kind: ActionSubjectKind
    lifecycle_precondition: ActionLifecyclePrecondition


def blocker_action_descriptor(kind: ActionKind) -> BlockerActionDescriptor | None:
    match kind:
        case ActionKind.REPORT_BLOCKER:
            return BlockerActionDescriptor(
                ActionEffect.ADVISORY,
                Role.WORKER,
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
            )
        case ActionKind.BLOCK:
            return BlockerActionDescriptor(
                ActionEffect.MUTATING,
                Role.COORDINATOR,
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
            )
        case ActionKind.BLOCK_ITEM:
            return BlockerActionDescriptor(
                ActionEffect.MUTATING,
                Role.COORDINATOR,
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.INTAKE_ITEM,
            )
        case (
            ActionKind.ACCEPT_CHECKPOINT
            | ActionKind.ACCEPT_PROPOSAL
            | ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | ActionKind.ACTIVATE
            | ActionKind.CLOSE
            | ActionKind.COMPLETE
            | ActionKind.CONTINUE
            | ActionKind.DEFER
            | ActionKind.DISPATCH
            | ActionKind.INSPECT
            | ActionKind.MARK_READY
            | ActionKind.MERGE_PROPOSAL
            | ActionKind.PAUSE
            | ActionKind.REJECT_PROPOSAL
            | ActionKind.REOPEN
            | ActionKind.RESUME
            | ActionKind.RETURN_FOR_CORRECTION
            | ActionKind.RETURN_PROPOSAL
            | ActionKind.SUBMIT_REVIEW
            | ActionKind.TRANSFER_COORDINATOR
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


class ReasonedProposalDispositionKind(Enum):
    RETURNED = "returned"
    REJECTED = "rejected"


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
class AcceptReviewAndContinueCommand:
    capability: ActionCapability
    value: AcceptReviewAndContinueInput


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
    | AcceptReviewAndContinueCommand
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
class ItemStateChange:
    item: ItemId
    before: WorkState
    after: WorkState


@dataclass(frozen=True, slots=True)
class ActivationChange:
    item: ItemId
    item_before: WorkState
    attempt: AttemptId
    brief_artifact_ref_id: ArtifactRefId
    branch: str
    base_revision: str
    owner: str


@dataclass(frozen=True, slots=True)
class AttemptStateChange:
    item: ItemId
    item_before: WorkState
    item_after: WorkState
    attempt: AttemptId
    attempt_before: AttemptState
    attempt_after: AttemptState


@dataclass(frozen=True, slots=True)
class BlockAttemptChange:
    item: ItemId
    item_before: WorkState
    attempt: AttemptId
    attempt_before: AttemptState
    dependencies_after: tuple[ItemId, ...]


@dataclass(frozen=True, slots=True)
class BlockItemChange:
    item: ItemId
    item_before: WorkState
    dependencies_after: tuple[ItemId, ...]


@dataclass(frozen=True, slots=True)
class ResumeAttemptChange:
    item: ItemId
    item_before: WorkState
    attempt: AttemptId
    attempt_before: AttemptState
    brief_artifact_ref_id: ArtifactRefId | None


@dataclass(frozen=True, slots=True)
class ReviewSubmissionChange:
    item: ItemId
    attempt: AttemptId
    protected_candidate_after: CandidateId
    candidate_observed_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewReturnChange:
    item: ItemId
    attempt: AttemptId
    authority_change: AttemptAuthorityChange


@dataclass(frozen=True, slots=True)
class ReviewAcceptanceChange:
    item: ItemId
    attempt: AttemptId
    candidate: CandidateId
    authority_change: AttemptAuthorityChange


@dataclass(frozen=True, slots=True)
class CompletionChange:
    item: ItemId
    item_before: WorkState
    attempt: AttemptId
    attempt_before: AttemptState
    evidence: str
    authority_change: AttemptAuthorityChange | None


@dataclass(frozen=True, slots=True)
class ItemClosureChange:
    item: ItemId
    item_before: WorkState
    terminal_state: CloseOutcome
    evidence: str


@dataclass(frozen=True, slots=True)
class AttemptClosureChange:
    item: ItemId
    item_before: WorkState
    terminal_state: CloseOutcome
    evidence: str
    attempt: AttemptId
    attempt_before: AttemptState
    authority_change: AttemptAuthorityChange | None


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
class AcceptedProposalChange:
    proposal: ProposalId
    disposed_at: datetime
    accepted_item: AcceptedProposalItem


@dataclass(frozen=True, slots=True)
class MergedProposalChange:
    proposal: ProposalId
    target_item: ItemId
    disposed_at: datetime


@dataclass(frozen=True, slots=True)
class ReasonedProposalDispositionChange:
    proposal: ProposalId
    disposition: ReasonedProposalDispositionKind
    reason: str
    disposed_at: datetime


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
    item: ItemId
    checkpoint: CheckpointId
    attempt: AttemptId
    candidate: CandidateId
    authority_change: AttemptAuthorityChange


@dataclass(frozen=True, slots=True)
class CoordinatorTransferChange:
    authority_change: CoordinatorAuthorityChange


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    action_id: ActionId
    item: ItemId | None
    outcome: str
    evidence: str | None
    decided_at: datetime


type DecisionChange = (
    ItemStateChange
    | ActivationChange
    | AttemptStateChange
    | BlockAttemptChange
    | BlockItemChange
    | ResumeAttemptChange
    | ReviewSubmissionChange
    | ReviewReturnChange
    | ReviewAcceptanceChange
    | CompletionChange
    | ItemClosureChange
    | AttemptClosureChange
    | AcceptedProposalChange
    | MergedProposalChange
    | ReasonedProposalDispositionChange
    | CheckpointAcceptanceChange
    | CoordinatorTransferChange
)


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    change: DecisionChange
    receipt: TransitionReceipt
