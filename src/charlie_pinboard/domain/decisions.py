from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.domain import decision_models, work_models
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


def _action_capability(action: decision_models.Action) -> decision_models.ActionCapability:
    return decision_models.ActionCapability(
        action.subject,
        action.label,
        action.expected_revision,
        action.coordinator_generation,
        action.subject_revision,
        action.authorization,
        action.lease_id,
        action.command_authority,
    )


def command_action(command: decision_models.TransitionCommand) -> decision_models.Action:  # noqa: C901, PLR0912
    """Reconstruct the advertised action whose kind is fixed by the closed command variant."""

    match command:
        case decision_models.AcceptCheckpointCommand(capability=capability):
            kind = decision_models.ActionKind.ACCEPT_CHECKPOINT
        case decision_models.AcceptReviewAndContinueCommand(capability=capability):
            kind = decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
        case decision_models.AcceptProposalCommand(capability=capability):
            kind = decision_models.ActionKind.ACCEPT_PROPOSAL
        case decision_models.ActivateCommand(capability=capability):
            kind = decision_models.ActionKind.ACTIVATE
        case decision_models.BlockCommand(capability=capability):
            kind = decision_models.ActionKind.BLOCK
        case decision_models.BlockItemCommand(capability=capability):
            kind = decision_models.ActionKind.BLOCK_ITEM
        case decision_models.CloseCommand(capability=capability):
            kind = decision_models.ActionKind.CLOSE
        case decision_models.CompleteCommand(capability=capability):
            kind = decision_models.ActionKind.COMPLETE
        case decision_models.DeferCommand(capability=capability):
            kind = decision_models.ActionKind.DEFER
        case decision_models.MarkReadyCommand(capability=capability):
            kind = decision_models.ActionKind.MARK_READY
        case decision_models.MergeProposalCommand(capability=capability):
            kind = decision_models.ActionKind.MERGE_PROPOSAL
        case decision_models.PauseCommand(capability=capability):
            kind = decision_models.ActionKind.PAUSE
        case decision_models.RejectProposalCommand(capability=capability):
            kind = decision_models.ActionKind.REJECT_PROPOSAL
        case decision_models.ReopenCommand(capability=capability):
            kind = decision_models.ActionKind.REOPEN
        case decision_models.ResumeCommand(capability=capability):
            kind = decision_models.ActionKind.RESUME
        case decision_models.ReturnForCorrectionCommand(capability=capability):
            kind = decision_models.ActionKind.RETURN_FOR_CORRECTION
        case decision_models.ReturnProposalCommand(capability=capability):
            kind = decision_models.ActionKind.RETURN_PROPOSAL
        case decision_models.SubmitReviewCommand(capability=capability):
            kind = decision_models.ActionKind.SUBMIT_REVIEW
        case decision_models.TransferCoordinatorCommand(capability=capability):
            kind = decision_models.ActionKind.TRANSFER_COORDINATOR
        case _ as unreachable:
            assert_never(unreachable)
    return decision_models.Action(
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
    action: decision_models.Action,
    value: work_models.TransitionInput,
) -> DecisionResult[decision_models.TransitionCommand]:
    """Bind an external action discriminator and decoded payload into one closed command variant."""

    capability = _action_capability(action)
    match action.kind, value:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT, work_models.AcceptCheckpointInput():
            return decision_models.AcceptCheckpointCommand(capability, value)
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, work_models.AcceptReviewAndContinueInput():
            return decision_models.AcceptReviewAndContinueCommand(capability, value)
        case decision_models.ActionKind.ACTIVATE, work_models.ActivateInput():
            return decision_models.ActivateCommand(capability, value)
        case decision_models.ActionKind.PAUSE, work_models.ReasonInput():
            return decision_models.PauseCommand(capability, value)
        case decision_models.ActionKind.BLOCK, work_models.BlockInput():
            return decision_models.BlockCommand(capability, value)
        case decision_models.ActionKind.COMPLETE, work_models.EvidenceInput():
            return decision_models.CompleteCommand(capability, value)
        case decision_models.ActionKind.CLOSE, work_models.CloseInput():
            return decision_models.CloseCommand(capability, value)
        case decision_models.ActionKind.RESUME, work_models.ResumeInput():
            return decision_models.ResumeCommand(capability, value)
        case decision_models.ActionKind.SUBMIT_REVIEW, work_models.SubmitReviewInput():
            return decision_models.SubmitReviewCommand(capability, value)
        case decision_models.ActionKind.RETURN_FOR_CORRECTION, work_models.ReasonInput():
            return decision_models.ReturnForCorrectionCommand(capability, value)
        case decision_models.ActionKind.REOPEN, work_models.EvidenceInput():
            return decision_models.ReopenCommand(capability, value)
        case decision_models.ActionKind.MARK_READY, work_models.ReasonInput():
            return decision_models.MarkReadyCommand(capability, value)
        case decision_models.ActionKind.BLOCK_ITEM, work_models.BlockInput():
            return decision_models.BlockItemCommand(capability, value)
        case decision_models.ActionKind.DEFER, work_models.DeferInput():
            return decision_models.DeferCommand(capability, value)
        case decision_models.ActionKind.ACCEPT_PROPOSAL, work_models.AcceptProposalInput():
            return decision_models.AcceptProposalCommand(capability, value)
        case decision_models.ActionKind.MERGE_PROPOSAL, work_models.MergeProposalInput():
            return decision_models.MergeProposalCommand(capability, value)
        case decision_models.ActionKind.RETURN_PROPOSAL, work_models.ReasonInput():
            return decision_models.ReturnProposalCommand(capability, value)
        case decision_models.ActionKind.REJECT_PROPOSAL, work_models.ReasonInput():
            return decision_models.RejectProposalCommand(capability, value)
        case decision_models.ActionKind.TRANSFER_COORDINATOR, work_models.TransferCoordinatorInput():
            return decision_models.TransferCoordinatorCommand(capability, value)

    match action.kind:
        case (
            decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                f"Action '{action.kind.value}' is not a canonical transition.",
            )
        case (
            decision_models.ActionKind.ACCEPT_CHECKPOINT
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_PROPOSAL
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.COMPLETE
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
            | decision_models.ActionKind.SUBMIT_REVIEW
            | decision_models.ActionKind.TRANSFER_COORDINATOR
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
    actor: decision_models.ActorAuthority

    def make(
        self,
        kind: decision_models.ActionKind,
        subject: SubjectId,
        label: str,
        subject_revision: str | None = None,
        command_authority: work_models.CommandAttemptAuthority | None = None,
    ) -> decision_models.Action:
        return decision_models.Action(
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


def _authority(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority, attempt: AttemptId
) -> work_models.AttemptAuthority | None:
    if attempt not in actor.attempts:
        return None
    return snapshot.authority_for(attempt, actor.lease_id, actor.generation)


def _scope_stale(snapshot: LedgerSnapshot, item: work_models.WorkItem) -> bool:
    if item.attempt is None:
        return False
    attempt = snapshot.attempts_by_id().get(item.attempt)
    scope = next((value for value in snapshot.scopes if value.item == item.item), None)
    if attempt is None or scope is None or attempt.accepted_scope_revision is None:
        return False
    return (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (scope.revision, scope.digest)


def _worker_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> tuple[decision_models.Action, ...]:
    result: list[decision_models.Action] = []
    for attempt in factory.actor.attempts:
        item = snapshot.item_for_attempt(attempt)
        authority = _authority(snapshot, factory.actor, attempt)
        if item is None or authority is None or item.state != work_models.WorkState.ACTIVE:
            continue
        command_authority = next(
            (value for value in snapshot.command_attempt_authorities if value.attempt == attempt),
            None,
        )
        revision = snapshot.subject_revision(item.item)
        result.extend(
            (
                factory.make(
                    decision_models.ActionKind.CONTINUE,
                    attempt,
                    f"Continue {item.item}",
                    revision,
                    command_authority,
                ),
                factory.make(
                    decision_models.ActionKind.REPORT_BLOCKER,
                    attempt,
                    f"Prepare blocker report for {item.item}",
                    revision,
                    command_authority,
                ),
            )
        )
        if not _scope_stale(snapshot, item):
            result.append(
                factory.make(
                    decision_models.ActionKind.SUBMIT_REVIEW,
                    attempt,
                    f"Submit {item.item} for review",
                    revision,
                    command_authority,
                )
            )
    return tuple(result)


def _active_coordinator_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> list[decision_models.Action]:
    result: list[decision_models.Action] = []
    for item in snapshot.items:
        if item.state not in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == work_models.WorkState.ACTIVE:
            result.append(factory.make(decision_models.ActionKind.CONTINUE, item.attempt, f"Continue {item.item}"))
            if not _scope_stale(snapshot, item):
                result.append(
                    factory.make(
                        decision_models.ActionKind.DISPATCH, item.attempt, f"Prepare a worker launch for {item.item}"
                    )
                )
            result.extend(
                (
                    factory.make(decision_models.ActionKind.PAUSE, item.attempt, f"Pause and preserve {item.item}"),
                    factory.make(
                        decision_models.ActionKind.BLOCK, item.attempt, f"Block active attempt for {item.item}"
                    ),
                )
            )
        if not _scope_stale(snapshot, item):
            result.append(
                factory.make(decision_models.ActionKind.COMPLETE, item.attempt, f"Accept and complete {item.item}")
            )
        if (
            item.state == work_models.WorkState.REVIEW
            and factory.actor.authorization == decision_models.AuthorizationKind.COORDINATION
        ):
            attempt = snapshot.attempt(item.attempt)
            result.extend(
                (
                    factory.make(
                        decision_models.ActionKind.ACCEPT_CHECKPOINT,
                        item.attempt,
                        f"Accept a checkpoint for {item.item}",
                    ),
                    factory.make(
                        decision_models.ActionKind.RETURN_FOR_CORRECTION,
                        item.attempt,
                        f"Return {item.item} for correction",
                    ),
                )
            )
            if attempt is not None and attempt.state == work_models.AttemptState.REVIEW:
                result.append(
                    factory.make(
                        decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
                        item.attempt,
                        f"Accept the review and continue {item.item}",
                    )
                )
    return result


def _item_actions(
    snapshot: LedgerSnapshot, item: work_models.WorkItem, factory: ActionFactory
) -> list[decision_models.Action]:
    close = factory.make(decision_models.ActionKind.CLOSE, item.item, f"Record a terminal decision for {item.item}")
    if item.state == work_models.WorkState.INTAKE:
        return [
            factory.make(decision_models.ActionKind.MARK_READY, item.item, f"Mark {item.item} ready"),
            factory.make(decision_models.ActionKind.BLOCK_ITEM, item.item, f"Block unstarted work item {item.item}"),
            factory.make(decision_models.ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    if item.state == work_models.WorkState.READY:
        return [
            factory.make(decision_models.ActionKind.ACTIVATE, item.item, f"Activate {item.item}"),
            factory.make(decision_models.ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    dependencies_live = any(dependency in snapshot.items_by_id() for dependency in item.depends_on)
    if item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED} and not dependencies_live:
        result = [factory.make(decision_models.ActionKind.RESUME, item.item, f"Return {item.item} to ready")]
        if item.attempt is None:
            result.append(
                factory.make(decision_models.ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition")
            )
        return [*result, close]
    if item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}:
        return [close]
    if item.state == work_models.WorkState.DEFERRED:
        return [factory.make(decision_models.ActionKind.REOPEN, item.item, f"Reopen {item.item} for intake"), close]
    return []


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    revision = snapshot.revision if actor.revision_scoped else ""
    factory = ActionFactory(revision, actor)
    match actor.role:
        case decision_models.Role.OBSERVER:
            return (factory.make(decision_models.ActionKind.INSPECT, LedgerId("ledger"), "Inspect current work"),)
        case decision_models.Role.WORKER:
            result = _worker_actions(snapshot, factory)
            if not result:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "The supplied attempt lease is not current for an active item.",
                )
            return result
        case decision_models.Role.COORDINATOR:
            result = _active_coordinator_actions(snapshot, factory)
            for item in snapshot.items:
                result.extend(_item_actions(snapshot, item, factory))
            for proposal in snapshot.proposals:
                for kind, verb in (
                    (decision_models.ActionKind.ACCEPT_PROPOSAL, "Accept"),
                    (decision_models.ActionKind.MERGE_PROPOSAL, "Merge"),
                    (decision_models.ActionKind.RETURN_PROPOSAL, "Return"),
                    (decision_models.ActionKind.REJECT_PROPOSAL, "Reject"),
                ):
                    result.append(
                        factory.make(kind, proposal.proposal, f"{verb} proposal {proposal.proposal}", proposal.revision)
                    )
            if snapshot.can_transfer_coordinator:
                result.append(
                    factory.make(
                        decision_models.ActionKind.TRANSFER_COORDINATOR,
                        LedgerId("ledger"),
                        "Transfer coordinator ownership",
                    )
                )
            return tuple(result)
        case _ as unreachable:
            assert_never(unreachable)


def rediscover_action(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority, supplied: decision_models.Action
) -> DecisionResult[decision_models.Action]:
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
    if supplied.authorization == decision_models.AuthorizationKind.ATTEMPT:
        comparable = replace(supplied, expected_revision=current.expected_revision)
    if comparable != current:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{supplied.action_id}' no longer carries the exact current authority.",
        )
    return current


def _receipt(
    action: decision_models.Action,
    item: ItemId | None,
    outcome: str,
    evidence: str | None,
    now: datetime,
) -> decision_models.TransitionReceipt:
    return decision_models.TransitionReceipt(action.action_id, item, outcome, evidence, now)


def _result(
    action: decision_models.Action,
    now: datetime,
    change: decision_models.DecisionChange,
    *,
    item: ItemId | None = None,
    outcome: str | None = None,
    evidence: str | None = None,
) -> decision_models.Decision:
    return decision_models.Decision(
        action,
        change,
        _receipt(action, item, outcome or action.kind.value, evidence, now),
    )


def _activate(
    snapshot: LedgerSnapshot, command: decision_models.ActivateCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state != work_models.WorkState.READY:
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
        decision_models.ActivationChange(
            item.item,
            item.state,
            value.attempt,
            value.brief_artifact_ref_id,
            value.branch,
            value.base_revision,
            value.owner,
        ),
        item=item.item,
    )


def _block_dependencies(
    snapshot: LedgerSnapshot,
    item: work_models.WorkItem,
    value: work_models.BlockInput,
) -> DecisionResult[tuple[ItemId, ...]]:
    dependencies = tuple(dict.fromkeys((*item.depends_on, *value.depends_on)))
    if (
        len(value.depends_on) != len(set(value.depends_on))
        or item.item in dependencies
        or any(
            snapshot.item(dependency) is None and dependency not in snapshot.history_items
            for dependency in dependencies
        )
    ):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Blocker dependencies must be ordered unique existing identities other than their owner.",
        )
    return dependencies


def _pause_or_block(
    snapshot: LedgerSnapshot,
    command: decision_models.PauseCommand | decision_models.BlockCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.ACTIVE:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active.")
    match command:
        case decision_models.PauseCommand():
            change: decision_models.DecisionChange = decision_models.AttemptStateChange(
                item.item,
                item.state,
                work_models.WorkState.PAUSED,
                attempt_id,
                work_models.AttemptState.ACTIVE,
                work_models.AttemptState.PAUSED,
            )
        case decision_models.BlockCommand(value=value):
            dependencies = _block_dependencies(snapshot, item, value)
            if isinstance(dependencies, DecisionFailure):
                return dependencies
            change = decision_models.BlockAttemptChange(
                item.item,
                item.state,
                attempt_id,
                work_models.AttemptState.ACTIVE,
                dependencies,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return _result(
        action,
        now,
        change,
        item=item.item,
    )


def _fence_retained_attempt_authority(
    snapshot: LedgerSnapshot,
    attempt: AttemptId,
) -> decision_models.AttemptAuthorityChange | None:
    authority = next((value for value in snapshot.attempt_authorities if value.attempt == attempt), None)
    if authority is None:
        return None
    return decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )


def _complete(
    snapshot: LedgerSnapshot, command: decision_models.CompleteCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state not in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "The named attempt is not active or in review."
        )
    if _scope_stale(snapshot, item):
        return DecisionFailure(
            DecisionFailureCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope."
        )
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    before = (
        work_models.AttemptState.REVIEW
        if item.state == work_models.WorkState.REVIEW
        else work_models.AttemptState.ACTIVE
    )
    authority_change = _fence_retained_attempt_authority(snapshot, attempt_id)
    return _result(
        action,
        now,
        decision_models.CompletionChange(item.item, item.state, attempt_id, before, value.evidence, authority_change),
        item=item.item,
        evidence=value.evidence,
    )


def _close(
    snapshot: LedgerSnapshot, command: decision_models.CloseCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Active or review work requires the acceptance path."
        )
    if value.outcome == work_models.CloseOutcome.DROPPED and any(
        item.item in candidate.depends_on for candidate in snapshot.items
    ):
        return DecisionFailure(DecisionFailureCode.LIVE_DEPENDENTS, f"Item '{item.item}' still has live dependents.")
    if item.item in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    authority_change = None if item.attempt is None else _fence_retained_attempt_authority(snapshot, item.attempt)
    if item.attempt is None:
        change: decision_models.DecisionChange = decision_models.ItemClosureChange(
            item.item, item.state, value.outcome, value.reason
        )
    else:
        attempt = snapshot.attempts_by_id().get(item.attempt)
        if attempt is None:
            return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{item.attempt}' does not exist.")
        change = decision_models.AttemptClosureChange(
            item.item,
            item.state,
            value.outcome,
            value.reason,
            item.attempt,
            attempt.state,
            authority_change,
        )
    return _result(
        action,
        now,
        change,
        item=item.item,
        outcome=value.outcome.value,
        evidence=value.reason,
    )


def _resume(
    snapshot: LedgerSnapshot, command: decision_models.ResumeCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state not in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}:
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
    target = work_models.WorkState.ACTIVE if item.attempt is not None else work_models.WorkState.READY
    if item.attempt is not None:
        before = (
            work_models.AttemptState.PAUSED
            if item.state == work_models.WorkState.PAUSED
            else work_models.AttemptState.BLOCKED
        )
        change: decision_models.DecisionChange = decision_models.ResumeAttemptChange(
            item.item,
            item.state,
            item.attempt,
            before,
            value.brief_artifact_ref_id,
        )
    else:
        change = decision_models.ItemStateChange(item.item, item.state, target)
    return _result(
        action,
        now,
        change,
        item=item.item,
    )


def _submit_review(
    snapshot: LedgerSnapshot, command: decision_models.SubmitReviewCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.ACTIVE:
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
        decision_models.ReviewSubmissionChange(
            item.item,
            attempt_id,
            value.candidate,
            now,
        ),
        item=item.item,
    )


def _return_for_correction(
    snapshot: LedgerSnapshot,
    command: decision_models.ReturnForCorrectionCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
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
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _result(
        action,
        now,
        decision_models.ReviewReturnChange(
            item.item,
            attempt_id,
            authority_change,
        ),
        item=item.item,
        evidence=value.reason,
    )


def _accept_checkpoint(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptCheckpointCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
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
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _result(
        action,
        now,
        decision_models.CheckpointAcceptanceChange(
            item.item,
            value.checkpoint,
            attempt_id,
            value.candidate,
            authority_change,
        ),
        item=item.item,
        evidence=value.evidence,
    )


def _accept_review_and_continue(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptReviewAndContinueCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    attempt_id = AttemptId(action.subject)
    item = snapshot.item_for_attempt(attempt_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt_id}' does not exist.")
    if item.state != work_models.WorkState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an item in review can have its review accepted for continuation.",
        )
    attempt = snapshot.attempt(attempt_id)
    if attempt is None or attempt.state != work_models.AttemptState.REVIEW:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only an attempt in review can have its review accepted for continuation.",
        )
    if not value.evidence or attempt.protected_candidate_revision != value.candidate:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Review continuation requires nonempty evidence and the exact protected candidate.",
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Review continuation requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = decision_models.AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1),
    )
    return _result(
        action,
        now,
        decision_models.ReviewAcceptanceChange(item.item, attempt_id, value.candidate, authority_change),
        item=item.item,
        evidence=value.evidence,
    )


def _block_item(
    snapshot: LedgerSnapshot, command: decision_models.BlockItemCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if item.state != work_models.WorkState.INTAKE:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot perform '{action.kind.value}' now."
        )
    dependencies = _block_dependencies(snapshot, item, command.value)
    if isinstance(dependencies, DecisionFailure):
        return dependencies
    return _result(
        action,
        now,
        decision_models.BlockItemChange(item.item, item.state, dependencies),
        item=item.item,
    )


def _simple_item_transition(
    snapshot: LedgerSnapshot,
    command: decision_models.ReopenCommand | decision_models.MarkReadyCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    match command:
        case decision_models.ReopenCommand():
            expected = (work_models.WorkState.DEFERRED,)
            target = work_models.WorkState.INTAKE
        case decision_models.MarkReadyCommand():
            expected = (work_models.WorkState.INTAKE,)
            target = work_models.WorkState.READY
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
    return _result(action, now, decision_models.ItemStateChange(item.item, item.state, target), item=item.item)


def _defer(
    snapshot: LedgerSnapshot, command: decision_models.DeferCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    item_id = ItemId(action.subject)
    item = snapshot.item(item_id)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    if (
        item.state not in {work_models.WorkState.INTAKE, work_models.WorkState.READY, work_models.WorkState.BLOCKED}
        or item.attempt is not None
    ):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be deferred now.")
    return _result(
        action,
        now,
        decision_models.ItemStateChange(item.item, item.state, work_models.WorkState.DEFERRED),
        item=item.item,
    )


def _accept_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.AcceptProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    visible_item = snapshot.item(ItemId(proposal_id))
    if visible_item is None or visible_item.state != work_models.WorkState.INTAKE or visible_item.attempt is not None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only a current visible intake proposal can be accepted.",
        )
    if value.item != ItemId(proposal_id):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "A visible proposal must be accepted with its same work-item identity.",
        )
    current_item = snapshot.item(value.item)
    if current_item is None or current_item.state != work_models.WorkState.INTAKE or current_item.attempt is not None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only the proposal's current visible intake item can be accepted.",
        )
    dependencies = tuple(dict.fromkeys((*current_item.depends_on, *value.depends_on)))
    if (
        len(value.depends_on) != len(set(value.depends_on))
        or value.item in dependencies
        or any(
            snapshot.item(dependency) is None and dependency not in snapshot.history_items
            for dependency in dependencies
        )
    ):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Accepted proposal dependencies must be ordered unique existing identities other than their owner.",
        )
    accepted_item: decision_models.AcceptedProposalItem | None = None
    if (
        proposal.user_label is not None
        and proposal.trigger is not None
        and proposal.why_it_matters is not None
        and proposal.effect is not None
        and proposal.unlock is not None
        and proposal.urgency_evidence is not None
    ):
        scope = work_models.ItemScope(
            value.item,
            proposal.user_label,
            proposal.trigger,
            proposal.why_it_matters,
            proposal.effect,
            proposal.unlock,
            tuple(
                work_models.ScopeDependency(position, dependency) for position, dependency in enumerate(dependencies)
            ),
        )
        scope_digest = item_scope_digest(scope)
        if isinstance(scope_digest, DecisionFailure):
            return scope_digest
        accepted_item = decision_models.AcceptedProposalItem(
            value.item,
            work_models.WorkState(value.state.value),
            value.timing,
            value.next_action,
            dependencies,
            proposal.user_label,
            f"proposal:{proposal.proposal}",
            proposal.trigger,
            proposal.why_it_matters,
            proposal.effect,
            proposal.unlock,
            proposal.urgency_evidence,
            scope_digest,
        )
    if accepted_item is None:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Accepted proposal semantics are incomplete.",
        )
    return _result(
        action,
        now,
        decision_models.AcceptedProposalChange(proposal.proposal, now, accepted_item),
        item=value.item,
    )


def _merge_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.MergeProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    value = command.value
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    visible_item = snapshot.item(ItemId(proposal_id))
    if visible_item is None or visible_item.state != work_models.WorkState.INTAKE or visible_item.attempt is not None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only a current visible intake proposal can be merged.",
        )
    if snapshot.item(value.target) is None and value.target not in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{value.target}' does not exist.")
    return _result(
        action,
        now,
        decision_models.MergedProposalChange(proposal.proposal, value.target, now),
    )


def _dispose_proposal(
    snapshot: LedgerSnapshot,
    command: decision_models.ReturnProposalCommand | decision_models.RejectProposalCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    proposal_id = ProposalId(action.subject)
    proposal = snapshot.proposal(proposal_id)
    if proposal is None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal_id}' does not exist.")
    visible_item = snapshot.item(ItemId(proposal_id))
    if visible_item is None or visible_item.state != work_models.WorkState.INTAKE or visible_item.attempt is not None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Only a current visible intake proposal can be returned or rejected.",
        )
    match command:
        case decision_models.ReturnProposalCommand(value=value):
            disposition = decision_models.ReasonedProposalDispositionKind.RETURNED
        case decision_models.RejectProposalCommand(value=value):
            disposition = decision_models.ReasonedProposalDispositionKind.REJECTED
        case _ as unreachable:
            assert_never(unreachable)
    return _result(
        action,
        now,
        decision_models.ReasonedProposalDispositionChange(proposal.proposal, disposition, value.reason, now),
        evidence=value.reason,
    )


def _transfer(
    snapshot: LedgerSnapshot, command: decision_models.TransferCoordinatorCommand, now: datetime
) -> DecisionResult[decision_models.Decision]:
    action = command_action(command)
    if not snapshot.can_transfer_coordinator:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "This ledger does not use transferable coordinator ownership."
        )
    before = snapshot.coordination_lease
    if before is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The transferable coordination lease is unavailable.",
        )
    if before.state != work_models.CoordinationLeaseStatus.ACTIVE or before.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The coordination lease is not active.")
    value = command.value
    return _result(
        action,
        now,
        decision_models.CoordinatorTransferChange(
            decision_models.CoordinatorAuthorityChange(
                before,
                replace(before, task_id=value.task_id, host_id=value.host_id, generation=before.generation + 1),
            )
        ),
    )


def decide(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    command: decision_models.TransitionCommand,
    now: datetime,
) -> DecisionResult[decision_models.Decision]:
    match command:
        case decision_models.AcceptCheckpointCommand():
            return _accept_checkpoint(snapshot, command, now)
        case decision_models.AcceptReviewAndContinueCommand():
            return _accept_review_and_continue(snapshot, command, now)
        case decision_models.ActivateCommand():
            return _activate(snapshot, command, now)
        case decision_models.PauseCommand() | decision_models.BlockCommand():
            return _pause_or_block(snapshot, command, now)
        case decision_models.CompleteCommand():
            return _complete(snapshot, command, now)
        case decision_models.CloseCommand():
            return _close(snapshot, command, now)
        case decision_models.ResumeCommand():
            return _resume(snapshot, command, now)
        case decision_models.SubmitReviewCommand():
            return _submit_review(snapshot, command, now)
        case decision_models.ReturnForCorrectionCommand():
            return _return_for_correction(snapshot, command, now)
        case decision_models.BlockItemCommand():
            return _block_item(snapshot, command, now)
        case decision_models.ReopenCommand() | decision_models.MarkReadyCommand():
            return _simple_item_transition(snapshot, command, now)
        case decision_models.DeferCommand():
            return _defer(snapshot, command, now)
        case decision_models.AcceptProposalCommand():
            return _accept_proposal(snapshot, command, now)
        case decision_models.MergeProposalCommand():
            return _merge_proposal(snapshot, command, now)
        case decision_models.ReturnProposalCommand() | decision_models.RejectProposalCommand():
            return _dispose_proposal(snapshot, command, now)
        case decision_models.TransferCoordinatorCommand():
            return _transfer(snapshot, command, now)
        case _ as unreachable:
            assert_never(unreachable)
