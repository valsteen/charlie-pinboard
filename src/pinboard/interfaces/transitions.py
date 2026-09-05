import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.actions import discover_actions
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    EvidenceArtifactRef,
    NewArtifact,
    ResultArtifactRef,
    WorkBriefIdentity,
)
from pinboard.application.service import (
    decide_and_commit_coordination_authority_change,
    execute,
    execute_checkpoint_acceptance,
)
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ActionId, HostId, ItemId, LeaseId, TaskId
from pinboard.interfaces import action_selection, cli_commands, coordination_authority, transition_models, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import (
    CommandFailure,
    CommandResult,
    TransitionInputFailure,
)
from pinboard.interfaces.transition_input import parse_item_revision_input, parse_transition_command
from pinboard.interfaces.work_briefs import read_transition_work_brief_identity


@dataclass(frozen=True, slots=True)
class _EncodedBorrowedTransitionRequest:
    action_id: ActionId
    encoded_payload: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedItemRevisionRequest:
    validated_revision: work_models.ReviseItemDefinitionInput


type _BorrowedTransitionRequest = _EncodedBorrowedTransitionRequest | _ValidatedItemRevisionRequest


def _item_changed_by_transition(
    action: decision_models.Action,
    receipt: decision_models.TransitionReceipt,
) -> ItemId | None:
    subject_kind = decision_models.action_semantics(action.kind).subject_kind
    match subject_kind:
        case decision_models.ActionSubjectKind.PROPOSAL:
            return ItemId(action.capability.subject)
        case (
            decision_models.ActionSubjectKind.ATTEMPT
            | decision_models.ActionSubjectKind.ITEM
            | decision_models.ActionSubjectKind.LEDGER
        ):
            return receipt.item
        case _ as unreachable:
            assert_never(unreachable)


def close(roots: cli_commands.ResolvedRoots, command: cli_commands.CloseCommand) -> CommandResult[int]:
    encoded_transition = msgspec.json.encode(
        {"outcome": command.outcome.value, "reason": command.reason}, order="sorted"
    )
    transition_revision = execute_with_borrowed_coordination(
        roots,
        command.task_id,
        command.host_id,
        command.ttl_seconds,
        _EncodedBorrowedTransitionRequest(ActionId(f"close:{command.item_id}"), encoded_transition),
    )
    if isinstance(transition_revision, CommandFailure):
        return transition_revision
    value = transition_models.CloseView(command.item_id, command.outcome.value, command.reason, transition_revision)
    if command.json:
        write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0


def revise_item(roots: cli_commands.ResolvedRoots, command: cli_commands.ItemReviseCommand) -> CommandResult[int]:
    try:
        revision_bytes = command.file.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read item revision: {error}")
    validated_revision = parse_item_revision_input(revision_bytes)
    if isinstance(validated_revision, TransitionInputFailure):
        return CommandFailure(validated_revision.code, validated_revision.message)
    definition_digest = work_item_definition_digest(validated_revision.definition)
    if not isinstance(definition_digest, str):
        return CommandFailure(definition_digest.code, definition_digest.message)
    transition_revision = execute_with_borrowed_coordination(
        roots,
        command.task_id,
        command.host_id,
        command.ttl_seconds,
        _ValidatedItemRevisionRequest(validated_revision),
    )
    if isinstance(transition_revision, CommandFailure):
        return transition_revision
    value = transition_models.ItemRevisionView(
        str(validated_revision.item_id),
        validated_revision.expected_revision + 1,
        definition_digest,
        transition_revision,
    )
    if command.json:
        write_json(value)
    else:
        print(
            f"OK ITEM_REVISED item={value.item_id} definition_revision={value.definition_revision} "
            f"definition_digest={value.definition_digest} project_revision={value.project_revision}"
        )
    return 0


def transition(roots: cli_commands.ResolvedRoots, cli_command: cli_commands.TransitionCommand) -> CommandResult[int]:
    supplied_action_receipt = action_selection.parse_action_receipt(cli_command)
    if isinstance(supplied_action_receipt, CommandFailure):
        return supplied_action_receipt
    try:
        encoded_payload = cli_command.payload.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}")
    selected_action = action_selection.select_current_action(roots, supplied_action_receipt)
    if isinstance(selected_action, CommandFailure):
        return selected_action
    decoded_command = parse_transition_command(selected_action, encoded_payload)
    if isinstance(decoded_command, TransitionInputFailure):
        return CommandFailure(decoded_command.code, decoded_command.message)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    commit_result = _execute_transition_command(roots, store, artifacts, decoded_command)
    if isinstance(commit_result, CommandFailure):
        return commit_result
    committed_receipt = commit_result
    committed_state = store.snapshot()
    affected_attempt = next(
        (
            attempt.attempt_id
            for attempt in committed_state.lifecycle.attempts
            if attempt.attempt_id == selected_action.capability.subject
        ),
        None,
    )
    changed_item = _item_changed_by_transition(selected_action, committed_receipt)
    affected = AffectedViews(
        queue=True,
        current_focus=True,
        history=True,
        items=(changed_item,) if changed_item is not None else (),
        attempts=(affected_attempt,) if affected_attempt is not None else (),
    )
    view_result = work_views.refresh(roots, store, affected, datetime.now(UTC))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    committed_revision = str(committed_state.lifecycle.project.revision)
    print(f"OK TRANSITION_APPLIED {decision_models.action_id(selected_action)} revision={committed_revision}")
    return 0


def read_brief_identity(
    store: SQLiteWorkStore,
    command: decision_models.TransitionCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[WorkBriefIdentity | None]:
    identity = read_transition_work_brief_identity(store.snapshot(), command, artifacts)
    if isinstance(identity, DecisionFailure):
        return CommandFailure(identity.code, identity.message)
    return identity


def publish_checkpoint_artifacts(
    roots: cli_commands.ResolvedRoots,
    command: decision_models.AcceptCheckpointCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[CheckpointArtifacts]:
    action = command.action
    value = command.value
    attempt_id = str(action.capability.subject)
    checkpoint_id = str(value.checkpoint)
    attempt_root = roots.work / "attempts" / attempt_id
    try:
        result_bytes = (attempt_root / "result.md").read_bytes()
        review_bytes = (attempt_root / "review.md").read_bytes()
    except OSError as error:
        return CommandFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot read current checkpoint result.md and review.md: {error}",
        )
    result = artifacts.publish(
        NewArtifact(stored_state.ArtifactKind.RESULT, f"{attempt_id}-{checkpoint_id}-result", 1, ".md", result_bytes)
    )
    review = artifacts.publish(
        NewArtifact(
            stored_state.ArtifactKind.EVIDENCE,
            f"{attempt_id}-{checkpoint_id}-review",
            1,
            ".md",
            review_bytes,
        )
    )
    return CheckpointArtifacts(
        ResultArtifactRef(result.key, result.revision, result.selector, result.content_sha256, result.size_bytes),
        EvidenceArtifactRef(review.key, review.revision, review.selector, review.content_sha256, review.size_bytes),
    )


def _execute_transition_command(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    artifacts: ArtifactRepository,
    command: decision_models.TransitionCommand,
) -> CommandResult[decision_models.TransitionReceipt]:
    transition_brief_identity = read_brief_identity(store, command, artifacts)
    if isinstance(transition_brief_identity, CommandFailure):
        return transition_brief_identity
    match command:
        case decision_models.AcceptCheckpointCommand():
            checkpoint_artifacts = publish_checkpoint_artifacts(roots, command, artifacts)
            if isinstance(checkpoint_artifacts, CommandFailure):
                return checkpoint_artifacts
            result = execute_checkpoint_acceptance(
                store,
                command,
                datetime.now(UTC),
                checkpoint_artifacts,
                transition_brief_identity=transition_brief_identity,
            )
        case (
            decision_models.AcceptReviewAndContinueCommand()
            | decision_models.ActivateCommand()
            | decision_models.PauseCommand()
            | decision_models.BlockCommand()
            | decision_models.CompleteCommand()
            | decision_models.CloseCommand()
            | decision_models.ResumeCommand()
            | decision_models.SubmitReviewCommand()
            | decision_models.ReturnForCorrectionCommand()
            | decision_models.ReopenCommand()
            | decision_models.MarkReadyCommand()
            | decision_models.BlockItemCommand()
            | decision_models.DeferCommand()
            | decision_models.AcceptProposalCommand()
            | decision_models.MergeProposalCommand()
            | decision_models.ReturnProposalCommand()
            | decision_models.RejectProposalCommand()
            | decision_models.ReviseItemCommand()
            | decision_models.TransferCoordinatorCommand()
        ):
            result = execute(
                store,
                command,
                datetime.now(UTC),
                transition_brief_identity=transition_brief_identity,
            )
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(result, DecisionFailure):
        return CommandFailure(result.code, result.message)
    return result


def coordinated_transition(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.CoordinationApplyCommand,
) -> CommandResult[int]:
    try:
        transition_bytes = command.payload.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}")
    transition_revision = execute_with_borrowed_coordination(
        roots,
        command.task_id,
        command.host_id,
        command.ttl_seconds,
        _EncodedBorrowedTransitionRequest(command.action_id, transition_bytes),
    )
    if isinstance(transition_revision, CommandFailure):
        return transition_revision
    value = transition_models.CoordinatedTransitionView(command.action_id, transition_revision)
    if command.json:
        write_json(value)
    else:
        print(f"OK COORDINATED_TRANSITION action={value.action_id} revision={value.revision}")
    return 0


def execute_with_borrowed_coordination(
    roots: cli_commands.ResolvedRoots,
    task_id: TaskId,
    host_id: HostId,
    ttl_seconds: int,
    request: _BorrowedTransitionRequest,
) -> CommandResult[str]:
    """Acquire, commit one current transition, release, then rebuild replaceable views."""

    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    state_observed_before_acquisition = store.snapshot()
    acquisition_requested_at = datetime.now(UTC)
    requested_acquisition = authority_models.AcquireCoordinationAuthority(
        state_observed_before_acquisition.lifecycle.project.host_epoch,
        task_id,
        host_id,
        LeaseId(uuid4().hex),
        acquisition_requested_at,
        acquisition_requested_at + timedelta(seconds=ttl_seconds),
    )
    acquisition_result = decide_and_commit_coordination_authority_change(store, requested_acquisition)
    if isinstance(acquisition_result, DecisionFailure):
        return CommandFailure(acquisition_result.code, acquisition_result.message)
    coordination_record_observed_before_acquisition = state_observed_before_acquisition.authority.coordination
    borrowed_authority = work_models.CoordinationCommandAuthority(
        state_observed_before_acquisition.lifecycle.project.host_epoch,
        task_id,
        host_id,
        requested_acquisition.lease_id,
        1
        if coordination_record_observed_before_acquisition is None
        else coordination_record_observed_before_acquisition.generation + 1,
        requested_acquisition.expires_at,
    )
    try:
        transition_result = _select_decode_and_commit_borrowed_transition(roots, store, artifacts, request)
    except Exception as transition_error:
        try:
            release_result = decide_and_commit_coordination_authority_change(
                store,
                authority_models.ReleaseCoordinationAuthority(borrowed_authority, datetime.now(UTC)),
            )
        except Exception as cleanup_error:
            transition_error.add_note(
                f"Borrowed coordination cleanup raised {type(cleanup_error).__name__}: {cleanup_error}"
            )
            raise transition_error from None
        if isinstance(release_result, DecisionFailure):
            transition_error.add_note(
                f"Borrowed coordination cleanup failed with {release_result.code.value}: {release_result.message}"
            )
        raise
    try:
        release_result = decide_and_commit_coordination_authority_change(
            store,
            authority_models.ReleaseCoordinationAuthority(borrowed_authority, datetime.now(UTC)),
        )
    except Exception as cleanup_error:
        if isinstance(transition_result, CommandFailure):
            cleanup_error.add_note(
                f"Original transition rejection {transition_result.code.value}: {transition_result.message}"
            )
        else:
            cleanup_error.add_note(f"Transition committed at revision {transition_result} before cleanup failed.")
        raise
    if isinstance(release_result, DecisionFailure):
        if isinstance(transition_result, CommandFailure):
            return CommandFailure(
                release_result.code,
                "Borrowed coordination release failed after transition rejection "
                f"{transition_result.code.value}: {transition_result.message}: {release_result.message}",
            )
        return CommandFailure(
            release_result.code,
            f"Borrowed coordination release failed after transition revision {transition_result}: {release_result.message}",
        )
    if isinstance(transition_result, CommandFailure):
        return transition_result
    committed_transition_revision = transition_result
    rebuild_result = work_views.rebuild(roots, store, datetime.now(UTC))
    if rebuild_result.warning is not None:
        print(rebuild_result.warning.message, file=sys.stderr)
    return committed_transition_revision


def _requested_borrowed_action_id(request: _BorrowedTransitionRequest) -> ActionId:
    match request:
        case _EncodedBorrowedTransitionRequest(action_id=action_id):
            return action_id
        case _ValidatedItemRevisionRequest(validated_revision=validated_revision):
            return ActionId(f"revise-item:{validated_revision.item_id}")
        case _ as unreachable:
            assert_never(unreachable)


def _decode_selected_borrowed_transition(
    action: decision_models.Action,
    request: _BorrowedTransitionRequest,
) -> CommandResult[decision_models.TransitionCommand]:
    match request:
        case _EncodedBorrowedTransitionRequest(encoded_payload=encoded_payload):
            command = parse_transition_command(action, encoded_payload)
            if isinstance(command, TransitionInputFailure):
                return CommandFailure(command.code, command.message)
            return command
        case _ValidatedItemRevisionRequest(validated_revision=validated_revision):
            if not isinstance(action, decision_models.ReviseItemAction):
                return CommandFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    f"Action '{_requested_borrowed_action_id(request)}' is not an item-revision action.",
                )
            return action.command(validated_revision)
        case _ as unreachable:
            assert_never(unreachable)


def _select_decode_and_commit_borrowed_transition(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    artifacts: ArtifactRepository,
    request: _BorrowedTransitionRequest,
) -> CommandResult[str]:
    state_observed_after_acquisition = store.snapshot()
    retained_authority_after_acquisition = coordination_authority.find_retained_coordination_authority(
        state_observed_after_acquisition
    )
    if isinstance(retained_authority_after_acquisition, CommandFailure):
        return retained_authority_after_acquisition
    current_actions = discover_actions(
        state_observed_after_acquisition,
        decision_models.Role.COORDINATOR,
        lease_id=retained_authority_after_acquisition.lease_id,
        generation=retained_authority_after_acquisition.generation,
        now=datetime.now(UTC),
    )
    if isinstance(current_actions, DecisionFailure):
        return CommandFailure(current_actions.code, current_actions.message)
    requested_action_id = _requested_borrowed_action_id(request)
    selected_action = next(
        (candidate for candidate in current_actions if decision_models.action_id(candidate) == requested_action_id),
        None,
    )
    if selected_action is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{requested_action_id}' is not currently legal.",
        )
    if isinstance(selected_action, decision_models.TransferCoordinatorAction):
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Borrowed coordination cannot transfer retained authority.",
        )
    decoded_transition = _decode_selected_borrowed_transition(selected_action, request)
    if isinstance(decoded_transition, CommandFailure):
        return decoded_transition
    transition_commit_result = _execute_transition_command(roots, store, artifacts, decoded_transition)
    if isinstance(transition_commit_result, CommandFailure):
        return transition_commit_result
    return str(int(selected_action.capability.expected_revision) + 1)
