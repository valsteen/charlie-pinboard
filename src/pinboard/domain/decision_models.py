from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import assert_never

from pinboard.domain import work_models
from pinboard.domain.identifiers import (
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
    LEDGER = "ledger"
    PROPOSAL = "proposal"


class ActionLifecyclePrecondition(Enum):
    ACTIVE_ATTEMPT = "active-attempt"
    ACTIVE_ATTEMPT_CURRENT_SCOPE = "active-attempt-current-scope"
    ACTIVE_OR_REVIEW_ATTEMPT_CURRENT_SCOPE = "active-or-review-attempt-current-scope"
    ACTIVE_TRANSFERABLE_COORDINATION = "active-transferable-coordination"
    DEFERRED_ITEM = "deferred-item"
    INTAKE_ITEM = "intake-item"
    INTAKE_READY_OR_BLOCKED_UNSTARTED_ITEM = "intake-ready-or-blocked-unstarted-item"
    ITEM_OUTSIDE_ACTIVE_AND_REVIEW = "item-outside-active-and-review"
    PAUSED_OR_BLOCKED_ITEM_WITHOUT_LIVE_DEPENDENCIES = "paused-or-blocked-item-without-live-dependencies"
    READY_ITEM = "ready-item"
    REVIEW_ATTEMPT = "review-attempt"
    VALID_LEDGER = "valid-ledger"
    VISIBLE_INTAKE_PROPOSAL = "visible-intake-proposal"


@dataclass(frozen=True, slots=True)
class ActionSemantics:
    use_case: str
    effect: ActionEffect
    permitted_roles: tuple[Role, ...]
    subject_kind: ActionSubjectKind
    lifecycle_precondition: ActionLifecyclePrecondition
    practical_result: str


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


def action_semantics(kind: ActionKind) -> ActionSemantics:  # noqa: C901, PLR0912
    match kind:
        case ActionKind.ACCEPT_CHECKPOINT:
            return ActionSemantics(
                "Accept one independently reviewed checkpoint without completing its item.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.REVIEW_ATTEMPT,
                "Archive the checkpoint evidence, pause the retained attempt, and fence its worker authority.",
            )
        case ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return ActionSemantics(
                "Accept a reviewed candidate while continuing the same attempt.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.REVIEW_ATTEMPT,
                "Record the accepted review, return the attempt to active, and fence its prior worker authority.",
            )
        case ActionKind.ACCEPT_PROPOSAL:
            return ActionSemantics(
                "Admit a visible proposal as accepted work.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.PROPOSAL,
                ActionLifecyclePrecondition.VISIBLE_INTAKE_PROPOSAL,
                "Accept the proposal into its same-identity work item and dispose the proposal record.",
            )
        case ActionKind.ACTIVATE:
            return ActionSemantics(
                "Start one ready item from an accepted brief.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.READY_ITEM,
                "Create and activate the item's attempt.",
            )
        case ActionKind.REPORT_BLOCKER:
            return ActionSemantics(
                "Preserve blocker evidence for coordination.",
                ActionEffect.ADVISORY,
                (Role.WORKER,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
                "Prepare a blocker report without changing shared lifecycle state.",
            )
        case ActionKind.BLOCK:
            return ActionSemantics(
                "Stop an active attempt on named dependencies.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
                "Move the item and attempt to blocked and record their dependencies.",
            )
        case ActionKind.BLOCK_ITEM:
            return ActionSemantics(
                "Stop unstarted intake work on named dependencies.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.INTAKE_ITEM,
                "Move the item to blocked and record its dependencies without creating an attempt.",
            )
        case ActionKind.COMPLETE:
            return ActionSemantics(
                "Accept and finish an active or reviewed attempt.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_OR_REVIEW_ATTEMPT_CURRENT_SCOPE,
                "Record terminal completion and remove the item from live work.",
            )
        case ActionKind.CLOSE:
            return ActionSemantics(
                "Record a terminal decision for non-active work.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.ITEM_OUTSIDE_ACTIVE_AND_REVIEW,
                "Record the done or dropped outcome and remove the item from live work.",
            )
        case ActionKind.CONTINUE:
            return ActionSemantics(
                "Continue work already active in an accepted attempt.",
                ActionEffect.ADVISORY,
                (Role.COORDINATOR, Role.WORKER),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
                "Continue the attempt without changing shared lifecycle state.",
            )
        case ActionKind.DEFER:
            return ActionSemantics(
                "Set aside unstarted work with an explicit reopen condition.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.INTAKE_READY_OR_BLOCKED_UNSTARTED_ITEM,
                "Move the item to deferred and retain its reopen condition.",
            )
        case ActionKind.DISPATCH:
            return ActionSemantics(
                "Prepare or verify a worker launch for an active attempt.",
                ActionEffect.ADVISORY,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT_CURRENT_SCOPE,
                "Produce a canonical worker launch without changing lifecycle state.",
            )
        case ActionKind.INSPECT:
            return ActionSemantics(
                "Inspect current work without taking authority.",
                ActionEffect.ADVISORY,
                (Role.OBSERVER,),
                ActionSubjectKind.LEDGER,
                ActionLifecyclePrecondition.VALID_LEDGER,
                "Return current ledger facts without changing shared state.",
            )
        case ActionKind.MARK_READY:
            return ActionSemantics(
                "Admit an intake item to ready work.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.INTAKE_ITEM,
                "Move the item from intake to ready.",
            )
        case ActionKind.MERGE_PROPOSAL:
            return ActionSemantics(
                "Merge a visible proposal into an existing work identity.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.PROPOSAL,
                ActionLifecyclePrecondition.VISIBLE_INTAKE_PROPOSAL,
                "Dispose the proposal as merged and remove its duplicate visible item.",
            )
        case ActionKind.PAUSE:
            return ActionSemantics(
                "Preserve an active attempt without a named dependency condition.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT,
                "Move the item and attempt to paused for later resume.",
            )
        case ActionKind.REJECT_PROPOSAL:
            return ActionSemantics(
                "Reject a visible proposal.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.PROPOSAL,
                ActionLifecyclePrecondition.VISIBLE_INTAKE_PROPOSAL,
                "Dispose the proposal as rejected and remove its visible item.",
            )
        case ActionKind.REOPEN:
            return ActionSemantics(
                "Return deferred work for intake reconsideration.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.DEFERRED_ITEM,
                "Return deferred work to intake.",
            )
        case ActionKind.RESUME:
            return ActionSemantics(
                "Restore paused or blocked work after its dependencies are clear.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ITEM,
                ActionLifecyclePrecondition.PAUSED_OR_BLOCKED_ITEM_WITHOUT_LIVE_DEPENDENCIES,
                "Return paused or blocked work to active when an attempt exists, otherwise ready.",
            )
        case ActionKind.RETURN_FOR_CORRECTION:
            return ActionSemantics(
                "Return a reviewed attempt for correction.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.REVIEW_ATTEMPT,
                "Return the same attempt to active and fence its prior worker authority.",
            )
        case ActionKind.RETURN_PROPOSAL:
            return ActionSemantics(
                "Return a visible proposal for more evidence or clarification.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.PROPOSAL,
                ActionLifecyclePrecondition.VISIBLE_INTAKE_PROPOSAL,
                "Dispose the proposal as returned and remove its visible item.",
            )
        case ActionKind.SUBMIT_REVIEW:
            return ActionSemantics(
                "Submit an active attempt's exact candidate for review.",
                ActionEffect.MUTATING,
                (Role.WORKER,),
                ActionSubjectKind.ATTEMPT,
                ActionLifecyclePrecondition.ACTIVE_ATTEMPT_CURRENT_SCOPE,
                "Move the item and attempt to review and protect the candidate.",
            )
        case ActionKind.TRANSFER_COORDINATOR:
            return ActionSemantics(
                "Transfer graph-wide coordination ownership.",
                ActionEffect.MUTATING,
                (Role.COORDINATOR,),
                ActionSubjectKind.LEDGER,
                ActionLifecyclePrecondition.ACTIVE_TRANSFERABLE_COORDINATION,
                "Replace the coordination owner and advance its fencing generation.",
            )
        case _ as unreachable:
            assert_never(unreachable)


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
type LifecycleAction = (
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
)
type TransitionAction = LifecycleAction | TransferCoordinatorAction
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
    state: work_models.AcceptedProposalState
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
class ReturnedProposalChange:
    proposal: ProposalId
    reason: str
    disposed_at: datetime


@dataclass(frozen=True, slots=True)
class RejectedProposalChange:
    proposal: ProposalId
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
    | ReturnedProposalChange
    | RejectedProposalChange
    | CheckpointAcceptanceChange
    | CoordinatorTransferChange
)


@dataclass(frozen=True, slots=True)
class Decision:
    action: TransitionAction
    change: DecisionChange
    receipt: TransitionReceipt
