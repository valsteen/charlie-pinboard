from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.domain.decision_models import (
    AcceptCheckpointCommand,
    AcceptedProposalItem,
    AcceptProposalCommand,
    Action,
    ActionCapability,
    ActionKind,
    ActivateCommand,
    ActorAuthority,
    AttemptAuthorityChange,
    AttemptChange,
    AuthorizationKind,
    BlockCommand,
    BlockItemCommand,
    CheckpointAcceptanceChange,
    CloseCommand,
    CompleteCommand,
    CoordinatorAuthorityChange,
    Decision,
    DeferCommand,
    ItemChange,
    MarkReadyCommand,
    MergeProposalCommand,
    PauseCommand,
    ProposalChange,
    RejectProposalCommand,
    ReopenCommand,
    ResumeCommand,
    ReturnForCorrectionCommand,
    ReturnProposalCommand,
    Role,
    SubmitReviewCommand,
    TransferCoordinatorCommand,
    TransitionCommand,
    TransitionReceipt,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    ItemId,
    LedgerId,
    ProposalId,
    SubjectId,
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.work_models import (
    AcceptCheckpointInput,
    AcceptProposalInput,
    ActivateInput,
    AttemptAuthority,
    AttemptState,
    BlockInput,
    CloseInput,
    CloseOutcome,
    CommandAttemptAuthority,
    CoordinationLeaseStatus,
    DeferInput,
    EvidenceInput,
    ItemScope,
    MergeProposalInput,
    ProposalDispositionKind,
    ReasonInput,
    ResumeInput,
    ScopeDependency,
    SubmitReviewInput,
    TransferCoordinatorInput,
    TransitionInput,
    WorkItem,
    WorkState,
)


def _action_capability(action: Action) -> ActionCapability:
    return ActionCapability(
        action.subject,
        action.label,
        action.expected_revision,
        action.coordinator_generation,
        action.subject_revision,
        action.authorization,
        action.lease_id,
        action.command_authority,
    )


def command_action(command: TransitionCommand) -> Action:  # noqa: C901, PLR0912
    """Reconstruct the advertised action whose kind is fixed by the closed command variant."""

    match command:
        case AcceptCheckpointCommand(capability=capability):
            kind = ActionKind.ACCEPT_CHECKPOINT
        case AcceptProposalCommand(capability=capability):
            kind = ActionKind.ACCEPT_PROPOSAL
        case ActivateCommand(capability=capability):
            kind = ActionKind.ACTIVATE
        case BlockCommand(capability=capability):
            kind = ActionKind.BLOCK
        case BlockItemCommand(capability=capability):
            kind = ActionKind.BLOCK_ITEM
        case CloseCommand(capability=capability):
            kind = ActionKind.CLOSE
        case CompleteCommand(capability=capability):
            kind = ActionKind.COMPLETE
        case DeferCommand(capability=capability):
            kind = ActionKind.DEFER
        case MarkReadyCommand(capability=capability):
            kind = ActionKind.MARK_READY
        case MergeProposalCommand(capability=capability):
            kind = ActionKind.MERGE_PROPOSAL
        case PauseCommand(capability=capability):
            kind = ActionKind.PAUSE
        case RejectProposalCommand(capability=capability):
            kind = ActionKind.REJECT_PROPOSAL
        case ReopenCommand(capability=capability):
            kind = ActionKind.REOPEN
        case ResumeCommand(capability=capability):
            kind = ActionKind.RESUME
        case ReturnForCorrectionCommand(capability=capability):
            kind = ActionKind.RETURN_FOR_CORRECTION
        case ReturnProposalCommand(capability=capability):
            kind = ActionKind.RETURN_PROPOSAL
        case SubmitReviewCommand(capability=capability):
            kind = ActionKind.SUBMIT_REVIEW
        case TransferCoordinatorCommand(capability=capability):
            kind = ActionKind.TRANSFER_COORDINATOR
        case _ as unreachable:
            assert_never(unreachable)
    return Action(
        ActionId(f"{kind.value}:{capability.subject}"),
        kind,
        capability.subject,
        capability.label,
        capability.expected_revision,
        capability.coordinator_generation,
        capability.subject_revision,
        capability.authorization,
        capability.lease_id,
        capability.command_authority,
    )


def bind_transition(  # noqa: C901, PLR0912
    action: Action,
    value: TransitionInput,
) -> DecisionResult[TransitionCommand]:
    """Bind an external action discriminator and decoded payload into one closed command variant."""

    capability = _action_capability(action)
    match action.kind, value:
        case ActionKind.ACCEPT_CHECKPOINT, AcceptCheckpointInput():
            return AcceptCheckpointCommand(capability, value)
        case ActionKind.ACTIVATE, ActivateInput():
            return ActivateCommand(capability, value)
        case ActionKind.PAUSE, ReasonInput():
            return PauseCommand(capability, value)
        case ActionKind.BLOCK, BlockInput():
            return BlockCommand(capability, value)
        case ActionKind.COMPLETE, EvidenceInput():
            return CompleteCommand(capability, value)
        case ActionKind.CLOSE, CloseInput():
            return CloseCommand(capability, value)
        case ActionKind.RESUME, ResumeInput():
            return ResumeCommand(capability, value)
        case ActionKind.SUBMIT_REVIEW, SubmitReviewInput():
            return SubmitReviewCommand(capability, value)
        case ActionKind.RETURN_FOR_CORRECTION, ReasonInput():
            return ReturnForCorrectionCommand(capability, value)
        case ActionKind.REOPEN, EvidenceInput():
            return ReopenCommand(capability, value)
        case ActionKind.MARK_READY, ReasonInput():
            return MarkReadyCommand(capability, value)
        case ActionKind.BLOCK_ITEM, BlockInput():
            return BlockItemCommand(capability, value)
        case ActionKind.DEFER, DeferInput():
            return DeferCommand(capability, value)
        case ActionKind.ACCEPT_PROPOSAL, AcceptProposalInput():
            return AcceptProposalCommand(capability, value)
        case ActionKind.MERGE_PROPOSAL, MergeProposalInput():
            return MergeProposalCommand(capability, value)
        case ActionKind.RETURN_PROPOSAL, ReasonInput():
            return ReturnProposalCommand(capability, value)
        case ActionKind.REJECT_PROPOSAL, ReasonInput():
            return RejectProposalCommand(capability, value)
        case ActionKind.TRANSFER_COORDINATOR, TransferCoordinatorInput():
            return TransferCoordinatorCommand(capability, value)

    match action.kind:
        case ActionKind.CONTINUE | ActionKind.DISPATCH | ActionKind.INSPECT | ActionKind.REPORT_BLOCKER:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                f"Action '{action.kind.value}' is not a canonical transition.",
            )
        case (
            ActionKind.ACCEPT_CHECKPOINT
            | ActionKind.ACCEPT_PROPOSAL
            | ActionKind.ACTIVATE
            | ActionKind.BLOCK
            | ActionKind.BLOCK_ITEM
            | ActionKind.COMPLETE
            | ActionKind.CLOSE
            | ActionKind.DEFER
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
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                f"Input for '{action.kind.value}' does not match its canonical command variant.",
            )
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class ActionFactory:
    revision: str
    actor: ActorAuthority

    def make(
        self,
        kind: ActionKind,
        subject: SubjectId,
        label: str,
        subject_revision: str | None = None,
        command_authority: CommandAttemptAuthority | None = None,
    ) -> Action:
        return Action(
            action_id=ActionId(f"{kind.value}:{subject}"),
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=self.revision,
            coordinator_generation=self.actor.generation,
            subject_revision=subject_revision,
            authorization=self.actor.authorization,
            lease_id=self.actor.lease_id,
            command_authority=command_authority,
        )


def _authority(snapshot: LedgerSnapshot, actor: ActorAuthority, attempt: AttemptId) -> AttemptAuthority | None:
    if attempt not in actor.attempts:
        return None
    return snapshot.authority_for(attempt, actor.lease_id, actor.generation)


def _scope_stale(snapshot: LedgerSnapshot, item: WorkItem) -> bool:
    if item.attempt is None:
        return False
    attempt = snapshot.attempts_by_id().get(item.attempt)
    scope = next((value for value in snapshot.scopes if value.item == item.item), None)
    if attempt is None or scope is None or attempt.accepted_scope_revision is None:
        return False
    return (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (scope.revision, scope.digest)


def _worker_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> tuple[Action, ...]:
    result: list[Action] = []
    for attempt in factory.actor.attempts:
        item = snapshot.item_for_attempt(attempt)
        authority = _authority(snapshot, factory.actor, attempt)
        if item is None or authority is None or item.state != WorkState.ACTIVE:
            continue
        command_authority = next(
            (value for value in snapshot.command_attempt_authorities if value.attempt == attempt),
            None,
        )
        revision = snapshot.subject_revision(item.item)
        result.extend(
            (
                factory.make(
                    ActionKind.CONTINUE,
                    attempt,
                    f"Continue {item.item}",
                    revision,
                    command_authority,
                ),
                factory.make(
                    ActionKind.REPORT_BLOCKER,
                    attempt,
                    f"Report a blocker for {item.item}",
                    revision,
                    command_authority,
                ),
            )
        )
        if not _scope_stale(snapshot, item):
            result.append(
                factory.make(
                    ActionKind.SUBMIT_REVIEW,
                    attempt,
                    f"Submit {item.item} for review",
                    revision,
                    command_authority,
                )
            )
    return tuple(result)


def _active_coordinator_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    for item in snapshot.items:
        if item.state not in {WorkState.ACTIVE, WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == WorkState.ACTIVE:
            result.append(factory.make(ActionKind.CONTINUE, item.attempt, f"Continue {item.item}"))
            if not _scope_stale(snapshot, item):
                result.append(
                    factory.make(ActionKind.DISPATCH, item.attempt, f"Prepare a worker launch for {item.item}")
                )
            result.extend(
                (
                    factory.make(ActionKind.PAUSE, item.attempt, f"Pause and preserve {item.item}"),
                    factory.make(ActionKind.BLOCK, item.attempt, f"Block {item.item} on a named condition"),
                )
            )
        if not _scope_stale(snapshot, item):
            result.append(factory.make(ActionKind.COMPLETE, item.attempt, f"Accept and complete {item.item}"))
        if item.state == WorkState.REVIEW and factory.actor.authorization == AuthorizationKind.COORDINATION:
            result.extend(
                (
                    factory.make(
                        ActionKind.ACCEPT_CHECKPOINT,
                        item.attempt,
                        f"Accept a checkpoint for {item.item}",
                    ),
                    factory.make(
                        ActionKind.RETURN_FOR_CORRECTION,
                        item.attempt,
                        f"Return {item.item} for correction",
                    ),
                )
            )
    return result


def _item_actions(snapshot: LedgerSnapshot, item: WorkItem, factory: ActionFactory) -> list[Action]:
    close = factory.make(ActionKind.CLOSE, item.item, f"Record a terminal decision for {item.item}")
    if item.state == WorkState.INTAKE:
        return [
            factory.make(ActionKind.MARK_READY, item.item, f"Mark {item.item} ready"),
            factory.make(ActionKind.BLOCK_ITEM, item.item, f"Block {item.item} on a named condition"),
            factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    if item.state == WorkState.READY:
        return [
            factory.make(ActionKind.ACTIVATE, item.item, f"Activate {item.item}"),
            factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    dependencies_live = any(dependency in snapshot.items_by_id() for dependency in item.depends_on)
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not dependencies_live:
        result = [factory.make(ActionKind.RESUME, item.item, f"Return {item.item} to ready")]
        if item.attempt is None:
            result.append(factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"))
        return [*result, close]
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED}:
        return [close]
    if item.state == WorkState.DEFERRED:
        return [factory.make(ActionKind.REOPEN, item.item, f"Reopen {item.item} for intake"), close]
    return []


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> DecisionResult[tuple[Action, ...]]:
    revision = snapshot.revision if actor.revision_scoped else ""
    factory = ActionFactory(revision, actor)
    match actor.role:
        case Role.OBSERVER:
            return (factory.make(ActionKind.INSPECT, LedgerId("ledger"), "Inspect current work"),)
        case Role.WORKER:
            result = _worker_actions(snapshot, factory)
            if not result:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "The supplied attempt lease is not current for an active item.",
                )
            return result
        case Role.COORDINATOR:
            result = _active_coordinator_actions(snapshot, factory)
            for item in snapshot.items:
                result.extend(_item_actions(snapshot, item, factory))
            for proposal in snapshot.proposals:
                for kind, verb in (
                    (ActionKind.ACCEPT_PROPOSAL, "Accept"),
                    (ActionKind.MERGE_PROPOSAL, "Merge"),
                    (ActionKind.RETURN_PROPOSAL, "Return"),
                    (ActionKind.REJECT_PROPOSAL, "Reject"),
                ):
                    result.append(
                        factory.make(kind, proposal.proposal, f"{verb} proposal {proposal.proposal}", proposal.revision)
                    )
            if snapshot.can_transfer_coordinator:
                result.append(
                    factory.make(ActionKind.TRANSFER_COORDINATOR, LedgerId("ledger"), "Transfer coordinator ownership")
                )
            return tuple(result)
        case _ as unreachable:
            assert_never(unreachable)


def rediscover_action(snapshot: LedgerSnapshot, actor: ActorAuthority, supplied: Action) -> DecisionResult[Action]:
    """Reselect one action and compare its complete subject-scoped mutation authority."""

    available = available_actions(snapshot, actor)
    if isinstance(available, DecisionFailure):
        return available
    current = next(
        (candidate for candidate in available if candidate.action_id == supplied.action_id),
        None,
    )
    if current is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Action '{supplied.action_id}' is no longer legal."
        )
    comparable = supplied
    if supplied.authorization == AuthorizationKind.ATTEMPT:
        comparable = replace(supplied, expected_revision=current.expected_revision)
    if comparable != current:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{supplied.action_id}' no longer carries the exact current authority.",
        )
    return current


def _receipt(
    action: Action,
    item: ItemId | None,
    outcome: str,
    evidence: str | None,
    now: datetime,
) -> TransitionReceipt:
    return TransitionReceipt(action.action_id, item, outcome, evidence, now)


def _result(
    action: Action,
    now: datetime,
    *,
    item: ItemId | None = None,
    item_change: ItemChange | None = None,
    attempt_change: AttemptChange | None = None,
    attempt_authority_change: AttemptAuthorityChange | None = None,
    checkpoint_acceptance_change: CheckpointAcceptanceChange | None = None,
    proposal_change: ProposalChange | None = None,
    coordinator_authority_change: CoordinatorAuthorityChange | None = None,
    outcome: str | None = None,
    evidence: str | None = None,
) -> Decision:
    return Decision(
        action,
        item_change,
        attempt_change,
        _receipt(action, item, outcome or action.kind.value, evidence, now),
        attempt_authority_change,
        checkpoint_acceptance_change,
        proposal_change,
        coordinator_authority_change,
    )


def _activate(snapshot: LedgerSnapshot, command: ActivateCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state != WorkState.READY:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not ready for activation."
        )
    artifact = next(
        (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == value.brief_artifact_ref_id),
        None,
    )
    if artifact is None or artifact.kind != "brief":
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Activation requires one existing brief artifact reference.",
        )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.ACTIVE, value.attempt),
        attempt_change=AttemptChange(
            value.attempt,
            None,
            AttemptState.ACTIVE,
            brief_artifact_ref_id=value.brief_artifact_ref_id,
            branch=value.branch,
            base_revision=value.base_revision,
            owner=value.owner,
        ),
    )


def _pause_or_block(
    snapshot: LedgerSnapshot,
    command: PauseCommand | BlockCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    match command:
        case PauseCommand():
            target = WorkState.PAUSED
        case BlockCommand():
            target = WorkState.BLOCKED
        case _ as unreachable:
            assert_never(unreachable)
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != WorkState.ACTIVE:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active.")
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=AttemptChange(attempt_id, AttemptState.ACTIVE, AttemptState(target.value)),
    )


def _fence_retained_attempt_authority(
    snapshot: LedgerSnapshot,
    attempt: AttemptId,
) -> AttemptAuthorityChange | None:
    authority = next((value for value in snapshot.attempt_authorities if value.attempt == attempt), None)
    if authority is None:
        return None
    return AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )


def _complete(snapshot: LedgerSnapshot, command: CompleteCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state not in {WorkState.ACTIVE, WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active or in review."
        )
    if _scope_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope."
        )
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    before = AttemptState.REVIEW if item.state == WorkState.REVIEW else AttemptState.ACTIVE
    authority_change = _fence_retained_attempt_authority(snapshot, attempt_id)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.evidence),
        attempt_change=AttemptChange(attempt_id, before, AttemptState.DONE),
        attempt_authority_change=authority_change,
        evidence=value.evidence,
    )


def _close(snapshot: LedgerSnapshot, command: CloseCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state in {WorkState.ACTIVE, WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Active or review work requires the acceptance path."
        )
    if value.outcome == CloseOutcome.DROPPED and any(item.item in candidate.depends_on for candidate in snapshot.items):
        return DecisionFailure(DecisionFailureCode.LIVE_DEPENDENTS, f"Item '{item.item}' still has live dependents.")
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    attempt_change = None
    if item.attempt is not None:
        attempt = snapshot.attempts_by_id().get(item.attempt)
        attempt_change = AttemptChange(item.attempt, None if attempt is None else attempt.state, AttemptState.DONE)
    authority_change = None if item.attempt is None else _fence_retained_attempt_authority(snapshot, item.attempt)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.reason),
        attempt_change=attempt_change,
        attempt_authority_change=authority_change,
        outcome=value.outcome.value,
        evidence=value.reason,
    )


def _resume(snapshot: LedgerSnapshot, command: ResumeCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in {WorkState.PAUSED, WorkState.BLOCKED}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not paused or blocked."
        )
    if any(dependency in snapshot.items_by_id() for dependency in item.depends_on):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED, f"Item '{item.item}' still has a live dependency."
        )
    if value.brief_artifact_ref_id is not None:
        if item.attempt is None:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Resuming with a revised brief requires an existing attempt.",
            )
        artifact = next(
            (candidate for candidate in snapshot.artifacts if candidate.artifact_ref_id == value.brief_artifact_ref_id),
            None,
        )
        if artifact is None or artifact.kind != "brief":
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Resuming with a revised brief requires one existing brief artifact reference.",
            )
    target = WorkState.ACTIVE if item.attempt is not None else WorkState.READY
    attempt_change = None
    if item.attempt is not None:
        before = AttemptState.PAUSED if item.state == WorkState.PAUSED else AttemptState.BLOCKED
        attempt_change = AttemptChange(
            item.attempt,
            before,
            AttemptState.ACTIVE,
            brief_artifact_ref_id=value.brief_artifact_ref_id,
        )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=attempt_change,
    )


def _submit_review(snapshot: LedgerSnapshot, command: SubmitReviewCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != WorkState.ACTIVE:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Only an active attempt can be submitted for review."
        )
    if _scope_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope."
        )
    attempt = snapshot.attempt(attempt_id)
    if attempt is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.REVIEW, item.attempt),
        attempt_change=AttemptChange(
            attempt_id,
            AttemptState.ACTIVE,
            AttemptState.REVIEW,
            protected_candidate_before=attempt.protected_candidate_revision,
            protected_candidate_after=value.candidate,
            candidate_observed_at=now,
        ),
    )


def _return_for_correction(
    snapshot: LedgerSnapshot,
    command: ReturnForCorrectionCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Only an attempt in review can be returned for correction."
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Returning a review requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, WorkState.REVIEW, WorkState.ACTIVE, item.attempt),
        attempt_change=AttemptChange(
            attempt_id,
            AttemptState.REVIEW,
            AttemptState.ACTIVE,
            protected_candidate_before=snapshot.attempts_by_id()[attempt_id].protected_candidate_revision,
        ),
        attempt_authority_change=authority_change,
        evidence=value.reason,
    )


def _accept_checkpoint(
    snapshot: LedgerSnapshot,
    command: AcceptCheckpointCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an attempt in review can have a checkpoint accepted.",
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Checkpoint acceptance requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, WorkState.REVIEW, WorkState.PAUSED, item.attempt),
        attempt_change=AttemptChange(attempt_id, AttemptState.REVIEW, AttemptState.PAUSED),
        attempt_authority_change=authority_change,
        evidence=value.evidence,
        checkpoint_acceptance_change=CheckpointAcceptanceChange(
            value.checkpoint,
            attempt_id,
            value.candidate,
            value.evidence,
            now,
        ),
    )


def _simple_item_transition(
    snapshot: LedgerSnapshot,
    command: ReopenCommand | MarkReadyCommand | BlockItemCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    match command:
        case ReopenCommand():
            expected = (WorkState.DEFERRED,)
            target = WorkState.INTAKE
        case MarkReadyCommand():
            expected = (WorkState.INTAKE,)
            target = WorkState.READY
        case BlockItemCommand():
            expected = (WorkState.INTAKE, WorkState.READY)
            target = WorkState.BLOCKED
        case _ as unreachable:
            assert_never(unreachable)
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in expected:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot perform '{action.kind.value}' now."
        )
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, target))


def _defer(snapshot: LedgerSnapshot, command: DeferCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED} or item.attempt is not None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be deferred now.")
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, WorkState.DEFERRED))


def _accept_proposal(
    snapshot: LedgerSnapshot,
    command: AcceptProposalCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    if value.item in snapshot.items_by_id() or value.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.ITEM_ALREADY_EXISTS, f"Item '{value.item}' already exists.")
    if (
        len(value.depends_on) != len(set(value.depends_on))
        or value.item in value.depends_on
        or any(
            snapshot.item(dependency) is None and dependency not in snapshot.history_items
            for dependency in value.depends_on
        )
    ):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Accepted proposal dependencies must be ordered unique existing identities other than their owner.",
        )
    change = ItemChange(value.item, None, WorkState(value.state.value))
    accepted_item: AcceptedProposalItem | None = None
    if (
        proposal.user_label is not None
        and proposal.trigger is not None
        and proposal.why_it_matters is not None
        and proposal.effect is not None
        and proposal.unlock is not None
        and proposal.urgency_evidence is not None
    ):
        scope = ItemScope(
            value.item,
            proposal.user_label,
            proposal.trigger,
            proposal.why_it_matters,
            proposal.effect,
            proposal.unlock,
            tuple(ScopeDependency(position, dependency) for position, dependency in enumerate(value.depends_on)),
        )
        scope_digest = item_scope_digest(scope)
        if isinstance(scope_digest, DecisionFailure):
            return scope_digest
        accepted_item = AcceptedProposalItem(
            value.item,
            WorkState(value.state.value),
            value.timing,
            value.next_action,
            value.depends_on,
            proposal.user_label,
            f"proposal:{proposal.proposal}",
            proposal.trigger,
            proposal.why_it_matters,
            proposal.effect,
            proposal.unlock,
            proposal.urgency_evidence,
            scope_digest,
        )
    return _result(
        action,
        now,
        item=value.item,
        item_change=change,
        proposal_change=ProposalChange(
            proposal.proposal,
            ProposalDispositionKind.ACCEPTED,
            value.item,
            None,
            now,
            accepted_item,
        ),
    )


def _merge_proposal(
    snapshot: LedgerSnapshot,
    command: MergeProposalCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    value = command.value
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    if snapshot.item(value.target) is None and value.target not in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{value.target}' does not exist.")
    return _result(
        action,
        now,
        proposal_change=ProposalChange(
            proposal.proposal,
            ProposalDispositionKind.MERGED,
            value.target,
            None,
            now,
        ),
    )


def _dispose_proposal(
    snapshot: LedgerSnapshot,
    command: ReturnProposalCommand | RejectProposalCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    action = command_action(command)
    match command:
        case ReturnProposalCommand(value=value):
            disposition = ProposalDispositionKind.RETURNED
        case RejectProposalCommand(value=value):
            disposition = ProposalDispositionKind.REJECTED
        case _ as unreachable:
            assert_never(unreachable)
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    return _result(
        action,
        now,
        evidence=value.reason,
        proposal_change=ProposalChange(proposal.proposal, disposition, None, value.reason, now),
    )


def _transfer(snapshot: LedgerSnapshot, command: TransferCoordinatorCommand, now: datetime) -> DecisionResult[Decision]:
    action = command_action(command)
    if not snapshot.can_transfer_coordinator:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "This ledger does not use transferable coordinator ownership."
        )
    before = snapshot.coordination_lease
    if before is None:
        return _result(action, now)
    if before.state != CoordinationLeaseStatus.ACTIVE or before.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The coordination lease is not active.")
    value = command.value
    return _result(
        action,
        now,
        coordinator_authority_change=CoordinatorAuthorityChange(
            before,
            replace(before, task_id=value.task_id, host_id=value.host_id, generation=before.generation + 1),
        ),
    )


def decide(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    command: TransitionCommand,
    now: datetime,
) -> DecisionResult[Decision]:
    match command:
        case AcceptCheckpointCommand():
            return _accept_checkpoint(snapshot, command, now)
        case ActivateCommand():
            return _activate(snapshot, command, now)
        case PauseCommand() | BlockCommand():
            return _pause_or_block(snapshot, command, now)
        case CompleteCommand():
            return _complete(snapshot, command, now)
        case CloseCommand():
            return _close(snapshot, command, now)
        case ResumeCommand():
            return _resume(snapshot, command, now)
        case SubmitReviewCommand():
            return _submit_review(snapshot, command, now)
        case ReturnForCorrectionCommand():
            return _return_for_correction(snapshot, command, now)
        case ReopenCommand() | MarkReadyCommand() | BlockItemCommand():
            return _simple_item_transition(snapshot, command, now)
        case DeferCommand():
            return _defer(snapshot, command, now)
        case AcceptProposalCommand():
            return _accept_proposal(snapshot, command, now)
        case MergeProposalCommand():
            return _merge_proposal(snapshot, command, now)
        case ReturnProposalCommand() | RejectProposalCommand():
            return _dispose_proposal(snapshot, command, now)
        case TransferCoordinatorCommand():
            return _transfer(snapshot, command, now)
        case _ as unreachable:
            assert_never(unreachable)
