from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from charlie_pinboard.domain import work_models
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    ItemId,
    LeaseId,
    LedgerId,
    ProposalId,
    SubjectId,
)


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


class ActionKind(Enum):
    value: str
    blocker_descriptor: BlockerActionDescriptor | None

    def __new__(
        cls,
        value: str,
        blocker_descriptor: BlockerActionDescriptor | None = None,
    ) -> ActionKind:
        member = object.__new__(cls)
        member._value_ = value
        member.blocker_descriptor = blocker_descriptor
        return member

    ACCEPT_CHECKPOINT = ("accept-checkpoint",)
    ACCEPT_REVIEW_AND_CONTINUE = ("accept-review-and-continue",)
    ACCEPT_PROPOSAL = ("accept-proposal",)
    ACTIVATE = ("activate",)
    BLOCK = (
        "block",
        BlockerActionDescriptor(
            ActionEffect.MUTATING,
            Role.COORDINATOR,
            ActionSubjectKind.ATTEMPT,
            ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
        ),
    )
    BLOCK_ITEM = (
        "block-item",
        BlockerActionDescriptor(
            ActionEffect.MUTATING,
            Role.COORDINATOR,
            ActionSubjectKind.ITEM,
            ActionLifecyclePrecondition.INTAKE_ITEM,
        ),
    )
    COMPLETE = ("complete",)
    CLOSE = ("close",)
    CONTINUE = ("continue",)
    DEFER = ("defer",)
    DISPATCH = ("dispatch",)
    INSPECT = ("inspect",)
    MARK_READY = ("mark-ready",)
    MERGE_PROPOSAL = ("merge-proposal",)
    PAUSE = ("pause",)
    REJECT_PROPOSAL = ("reject-proposal",)
    REOPEN = ("reopen",)
    REPORT_BLOCKER = (
        "report-blocker",
        BlockerActionDescriptor(
            ActionEffect.ADVISORY,
            Role.WORKER,
            ActionSubjectKind.ATTEMPT,
            ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
        ),
    )
    RESUME = ("resume",)
    RETURN_FOR_CORRECTION = ("return-for-correction",)
    RETURN_PROPOSAL = ("return-proposal",)
    SUBMIT_REVIEW = ("submit-review",)
    TRANSFER_COORDINATOR = ("transfer-coordinator",)


class ReasonedProposalDispositionKind(Enum):
    RETURNED = "returned"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ActionCapability[SubjectT: SubjectId]:
    subject: SubjectT
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: LeaseId | None = None
    command_authority: work_models.CommandAttemptAuthority | None = None


@dataclass(frozen=True, slots=True)
class AcceptCheckpointAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.ACCEPT_CHECKPOINT)


@dataclass(frozen=True, slots=True)
class AcceptReviewAndContinueAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.ACCEPT_REVIEW_AND_CONTINUE)


@dataclass(frozen=True, slots=True)
class AcceptProposalAction:
    capability: ActionCapability[ProposalId]
    kind: ActionKind = field(init=False, default=ActionKind.ACCEPT_PROPOSAL)


@dataclass(frozen=True, slots=True)
class ActivateAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.ACTIVATE)


@dataclass(frozen=True, slots=True)
class BlockAttemptAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.BLOCK)


@dataclass(frozen=True, slots=True)
class BlockItemAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.BLOCK_ITEM)


@dataclass(frozen=True, slots=True)
class CompleteAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.COMPLETE)


@dataclass(frozen=True, slots=True)
class CloseAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.CLOSE)


@dataclass(frozen=True, slots=True)
class ContinueAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.CONTINUE)


@dataclass(frozen=True, slots=True)
class DeferAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.DEFER)


@dataclass(frozen=True, slots=True)
class DispatchAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.DISPATCH)


@dataclass(frozen=True, slots=True)
class InspectAction:
    capability: ActionCapability[LedgerId]
    kind: ActionKind = field(init=False, default=ActionKind.INSPECT)


@dataclass(frozen=True, slots=True)
class MarkReadyAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.MARK_READY)


@dataclass(frozen=True, slots=True)
class MergeProposalAction:
    capability: ActionCapability[ProposalId]
    kind: ActionKind = field(init=False, default=ActionKind.MERGE_PROPOSAL)


@dataclass(frozen=True, slots=True)
class PauseAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.PAUSE)


@dataclass(frozen=True, slots=True)
class RejectProposalAction:
    capability: ActionCapability[ProposalId]
    kind: ActionKind = field(init=False, default=ActionKind.REJECT_PROPOSAL)


@dataclass(frozen=True, slots=True)
class ReopenAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.REOPEN)


@dataclass(frozen=True, slots=True)
class ReportBlockerAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.REPORT_BLOCKER)


@dataclass(frozen=True, slots=True)
class ResumeAction:
    capability: ActionCapability[ItemId]
    kind: ActionKind = field(init=False, default=ActionKind.RESUME)


@dataclass(frozen=True, slots=True)
class ReturnForCorrectionAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.RETURN_FOR_CORRECTION)


@dataclass(frozen=True, slots=True)
class ReturnProposalAction:
    capability: ActionCapability[ProposalId]
    kind: ActionKind = field(init=False, default=ActionKind.RETURN_PROPOSAL)


@dataclass(frozen=True, slots=True)
class SubmitReviewAction:
    capability: ActionCapability[AttemptId]
    kind: ActionKind = field(init=False, default=ActionKind.SUBMIT_REVIEW)


@dataclass(frozen=True, slots=True)
class TransferCoordinatorAction:
    capability: ActionCapability[LedgerId]
    kind: ActionKind = field(init=False, default=ActionKind.TRANSFER_COORDINATOR)


type AdvisoryAction = ContinueAction | DispatchAction | InspectAction | ReportBlockerAction
type TransitionAction = (
    AcceptCheckpointAction
    | AcceptReviewAndContinueAction
    | AcceptProposalAction
    | ActivateAction
    | BlockAttemptAction
    | BlockItemAction
    | CompleteAction
    | CloseAction
    | DeferAction
    | MarkReadyAction
    | MergeProposalAction
    | PauseAction
    | RejectProposalAction
    | ReopenAction
    | ResumeAction
    | ReturnForCorrectionAction
    | ReturnProposalAction
    | SubmitReviewAction
    | TransferCoordinatorAction
)
type Action = TransitionAction | AdvisoryAction


def action_id(action: Action) -> ActionId:
    return ActionId(f"{action.kind.value}:{action.capability.subject}")


@dataclass(frozen=True, slots=True)
class AcceptCheckpointCommand:
    action: AcceptCheckpointAction
    value: work_models.AcceptCheckpointInput


@dataclass(frozen=True, slots=True)
class AcceptReviewAndContinueCommand:
    action: AcceptReviewAndContinueAction
    value: work_models.AcceptReviewAndContinueInput


@dataclass(frozen=True, slots=True)
class ActivateCommand:
    action: ActivateAction
    value: work_models.ActivateInput


@dataclass(frozen=True, slots=True)
class PauseCommand:
    action: PauseAction
    value: work_models.ReasonInput


@dataclass(frozen=True, slots=True)
class BlockCommand:
    action: BlockAttemptAction
    value: work_models.BlockInput


@dataclass(frozen=True, slots=True)
class CompleteCommand:
    action: CompleteAction
    value: work_models.EvidenceInput


@dataclass(frozen=True, slots=True)
class CloseCommand:
    action: CloseAction
    value: work_models.CloseInput


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    action: ResumeAction
    value: work_models.ResumeInput


@dataclass(frozen=True, slots=True)
class SubmitReviewCommand:
    action: SubmitReviewAction
    value: work_models.SubmitReviewInput


@dataclass(frozen=True, slots=True)
class ReturnForCorrectionCommand:
    action: ReturnForCorrectionAction
    value: work_models.ReasonInput


@dataclass(frozen=True, slots=True)
class ReopenCommand:
    action: ReopenAction
    value: work_models.EvidenceInput


@dataclass(frozen=True, slots=True)
class MarkReadyCommand:
    action: MarkReadyAction
    value: work_models.ReasonInput


@dataclass(frozen=True, slots=True)
class BlockItemCommand:
    action: BlockItemAction
    value: work_models.BlockInput


@dataclass(frozen=True, slots=True)
class DeferCommand:
    action: DeferAction
    value: work_models.DeferInput


@dataclass(frozen=True, slots=True)
class AcceptProposalCommand:
    action: AcceptProposalAction
    value: work_models.AcceptProposalInput


@dataclass(frozen=True, slots=True)
class MergeProposalCommand:
    action: MergeProposalAction
    value: work_models.MergeProposalInput


@dataclass(frozen=True, slots=True)
class ReturnProposalCommand:
    action: ReturnProposalAction
    value: work_models.ReasonInput


@dataclass(frozen=True, slots=True)
class RejectProposalCommand:
    action: RejectProposalAction
    value: work_models.ReasonInput


@dataclass(frozen=True, slots=True)
class TransferCoordinatorCommand:
    action: TransferCoordinatorAction
    value: work_models.TransferCoordinatorInput


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
    before: work_models.WorkState
    after: work_models.WorkState


@dataclass(frozen=True, slots=True)
class ActivationChange:
    item: ItemId
    item_before: work_models.WorkState
    attempt: AttemptId
    brief_artifact_ref_id: ArtifactRefId
    branch: str
    base_revision: str
    owner: str


@dataclass(frozen=True, slots=True)
class AttemptStateChange:
    item: ItemId
    item_before: work_models.WorkState
    item_after: work_models.WorkState
    attempt: AttemptId
    attempt_before: work_models.AttemptState
    attempt_after: work_models.AttemptState


@dataclass(frozen=True, slots=True)
class BlockAttemptChange:
    item: ItemId
    item_before: work_models.WorkState
    attempt: AttemptId
    attempt_before: work_models.AttemptState
    dependencies_after: tuple[ItemId, ...]


@dataclass(frozen=True, slots=True)
class BlockItemChange:
    item: ItemId
    item_before: work_models.WorkState
    dependencies_after: tuple[ItemId, ...]


@dataclass(frozen=True, slots=True)
class ResumeAttemptChange:
    item: ItemId
    item_before: work_models.WorkState
    attempt: AttemptId
    attempt_before: work_models.AttemptState
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
    item_before: work_models.WorkState
    attempt: AttemptId
    attempt_before: work_models.AttemptState
    evidence: str
    authority_change: AttemptAuthorityChange | None


@dataclass(frozen=True, slots=True)
class ItemClosureChange:
    item: ItemId
    item_before: work_models.WorkState
    terminal_state: work_models.CloseOutcome
    evidence: str


@dataclass(frozen=True, slots=True)
class AttemptClosureChange:
    item: ItemId
    item_before: work_models.WorkState
    terminal_state: work_models.CloseOutcome
    evidence: str
    attempt: AttemptId
    attempt_before: work_models.AttemptState
    authority_change: AttemptAuthorityChange | None


@dataclass(frozen=True, slots=True)
class AcceptedProposalItem:
    item: ItemId
    state: work_models.WorkState
    timing: work_models.Timing | None
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
    before: work_models.AttemptAuthority
    after: work_models.AttemptAuthority


@dataclass(frozen=True, slots=True)
class CoordinatorAuthorityChange:
    before: work_models.CoordinationLeaseAuthority
    after: work_models.CoordinationLeaseAuthority


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
    action: TransitionAction
    change: DecisionChange
    receipt: TransitionReceipt
