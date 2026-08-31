import sys
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
from pinboard.application.service import change_coordination_authority, execute, execute_checkpoint_acceptance
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_models import AcquireCoordinationAuthority, ReleaseCoordinationAuthority
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ActionId, HostId, LeaseId, TaskId
from pinboard.interfaces import action_selection, cli_commands, coordination_authority, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import (
    CommandFailure,
    CommandResult,
    TransitionInputFailure,
)
from pinboard.interfaces.transition_input import parse_item_revision_input, parse_transition_command
from pinboard.interfaces.transition_models import CloseView, CoordinatedTransitionView, ItemRevisionView
from pinboard.interfaces.work_briefs import read_transition_work_brief_identity


def close(roots: cli_commands.ResolvedRoots, command: cli_commands.CloseCommand) -> CommandResult[int]:
    payload = msgspec.json.encode({"outcome": command.outcome.value, "reason": command.reason}, order="sorted")
    if isinstance(
        revision := execute_borrowed_coordination(
            roots,
            command.task_id,
            command.host_id,
            command.ttl_seconds,
            ActionId(f"close:{command.item_id}"),
            payload,
        ),
        CommandFailure,
    ):
        return revision
    value = CloseView(command.item_id, command.outcome.value, command.reason, revision)
    if command.json:
        write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0


def revise_item(roots: cli_commands.ResolvedRoots, command: cli_commands.ItemReviseCommand) -> CommandResult[int]:
    try:
        payload = command.file.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read item revision: {error}")
    if isinstance(parsed := parse_item_revision_input(payload), TransitionInputFailure):
        return CommandFailure(parsed.code, parsed.message)
    digest = work_item_definition_digest(parsed.definition)
    if not isinstance(digest, str):
        return CommandFailure(digest.code, digest.message)
    if isinstance(
        project_revision := execute_borrowed_coordination(
            roots,
            command.task_id,
            command.host_id,
            command.ttl_seconds,
            ActionId(f"revise-item:{parsed.item_id}"),
            payload,
        ),
        CommandFailure,
    ):
        return project_revision
    value = ItemRevisionView(str(parsed.item_id), parsed.expected_revision + 1, digest, project_revision)
    if command.json:
        write_json(value)
    else:
        print(
            f"OK ITEM_REVISED item={value.item_id} definition_revision={value.definition_revision} "
            f"definition_digest={value.definition_digest} project_revision={value.project_revision}"
        )
    return 0


def transition(roots: cli_commands.ResolvedRoots, cli_command: cli_commands.TransitionCommand) -> CommandResult[int]:
    if isinstance(action := action_selection.action_from_command(cli_command), CommandFailure):
        return action
    try:
        payload = cli_command.payload.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}")
    role = (
        decision_models.Role.WORKER
        if action.capability.authorization == decision_models.AuthorizationKind.ATTEMPT
        else decision_models.Role.PREPARER
        if action.capability.authorization == decision_models.AuthorizationKind.PREPARATION
        else decision_models.Role.COORDINATOR
    )
    if isinstance(action := action_selection.reselect_action(roots, action, role), CommandFailure):
        return action
    if isinstance(command := parse_transition_command(action, payload), TransitionInputFailure):
        return CommandFailure(command.code, command.message)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    if isinstance(transition_brief_identity := read_brief_identity(store, command, artifacts), CommandFailure):
        return transition_brief_identity
    match command:
        case decision_models.AcceptCheckpointCommand():
            if isinstance(checkpoint_artifacts := publish_checkpoint_artifacts(roots, command, artifacts), CommandFailure):
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
    state = store.snapshot()
    affected_attempt = next(
        (attempt.attempt_id for attempt in state.lifecycle.attempts if attempt.attempt_id == action.capability.subject),
        None,
    )
    affected = AffectedViews(
        queue=True,
        current_focus=True,
        history=True,
        items=(result.item,) if result.item is not None else (),
        attempts=(affected_attempt,) if affected_attempt is not None else (),
    )
    view_result = work_views.refresh(roots, store, affected, datetime.now(UTC))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    revision = str(state.lifecycle.project.revision)
    print(f"OK TRANSITION_APPLIED {decision_models.action_id(action)} revision={revision}")
    return 0


def read_brief_identity(
    store: SQLiteWorkStore,
    command: decision_models.TransitionCommand,
    artifacts: ArtifactRepository,
) -> CommandResult[WorkBriefIdentity | None]:
    if isinstance(identity := read_transition_work_brief_identity(store.snapshot(), command, artifacts), DecisionFailure):
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


def coordinated_transition(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.CoordinationApplyCommand,
) -> CommandResult[int]:
    try:
        payload = command.payload.read_bytes()
    except OSError as error:
        return CommandFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}")
    if isinstance(
        transition_revision := execute_borrowed_coordination(
            roots,
            command.task_id,
            command.host_id,
            command.ttl_seconds,
            command.action_id,
            payload,
        ),
        CommandFailure,
    ):
        return transition_revision
    value = CoordinatedTransitionView(command.action_id, transition_revision)
    if command.json:
        write_json(value)
    else:
        print(f"OK COORDINATED_TRANSITION action={value.action_id} revision={value.revision}")
    return 0


def execute_borrowed_coordination(
    roots: cli_commands.ResolvedRoots,
    task_id: TaskId,
    host_id: HostId,
    ttl_seconds: int,
    selected_action_id: ActionId,
    payload: bytes,
) -> CommandResult[str]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    state = store.snapshot()
    now = datetime.now(UTC)
    acquire = AcquireCoordinationAuthority(
        state.lifecycle.project.host_epoch,
        task_id,
        host_id,
        LeaseId(uuid4().hex),
        now,
        now + timedelta(seconds=ttl_seconds),
    )
    if isinstance(acquired := change_coordination_authority(store, acquire), DecisionFailure):
        return CommandFailure(acquired.code, acquired.message)
    retained = state.authority.coordination
    borrowed = work_models.CoordinationCommandAuthority(
        state.lifecycle.project.host_epoch,
        task_id,
        host_id,
        acquire.lease_id,
        1 if retained is None else retained.generation + 1,
        acquire.expires_at,
    )
    try:
        transition_result = apply_borrowed_transition(roots, store, artifacts, selected_action_id, payload)
    except Exception as transition_error:
        try:
            released = change_coordination_authority(
                store,
                ReleaseCoordinationAuthority(borrowed, datetime.now(UTC)),
            )
        except Exception as cleanup_error:  # noqa: BLE001 - cleanup must preserve any primary infrastructure failure
            transition_error.add_note(
                f"Borrowed coordination cleanup raised {type(cleanup_error).__name__}: {cleanup_error}"
            )
            raise transition_error from None
        if isinstance(released, DecisionFailure):
            transition_error.add_note(
                f"Borrowed coordination cleanup failed with {released.code.value}: {released.message}"
            )
        raise
    try:
        released = change_coordination_authority(
            store,
            ReleaseCoordinationAuthority(borrowed, datetime.now(UTC)),
        )
    except Exception as cleanup_error:
        if isinstance(transition_result, CommandFailure):
            cleanup_error.add_note(
                f"Original transition rejection {transition_result.code.value}: {transition_result.message}"
            )
        else:
            cleanup_error.add_note(f"Transition committed at revision {transition_result} before cleanup failed.")
        raise
    if isinstance(released, DecisionFailure):
        if isinstance(transition_result, CommandFailure):
            return CommandFailure(
                released.code,
                "Borrowed coordination release failed after transition rejection "
                f"{transition_result.code.value}: {transition_result.message}: {released.message}",
            )
        return CommandFailure(
            released.code,
            f"Borrowed coordination release failed after transition revision {transition_result}: {released.message}",
        )
    if isinstance(transition_result, CommandFailure):
        return transition_result
    view_result = work_views.rebuild(roots, store, datetime.now(UTC))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    return transition_result


def apply_borrowed_transition(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    artifacts: ArtifactRepository,
    selected_action_id: ActionId,
    payload: bytes,
) -> CommandResult[str]:
    if isinstance(coordination := coordination_authority.retained_coordination(store.snapshot()), CommandFailure):
        return coordination
    if isinstance(
        available := discover_actions(
            store,
            decision_models.Role.COORDINATOR,
            lease_id=coordination.lease_id,
            generation=coordination.generation,
            now=datetime.now(UTC),
        ),
        DecisionFailure,
    ):
        return CommandFailure(available.code, available.message)
    action = next(
        (candidate for candidate in available if decision_models.action_id(candidate) == selected_action_id),
        None,
    )
    if action is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{selected_action_id}' is not currently legal.",
        )
    if isinstance(action, decision_models.TransferCoordinatorAction):
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Borrowed coordination cannot transfer retained authority.",
        )
    if isinstance(command := parse_transition_command(action, payload), TransitionInputFailure):
        return CommandFailure(command.code, command.message)
    if isinstance(transition_brief_identity := read_brief_identity(store, command, artifacts), CommandFailure):
        return transition_brief_identity
    match command:
        case decision_models.AcceptCheckpointCommand():
            if isinstance(checkpoint_artifacts := publish_checkpoint_artifacts(roots, command, artifacts), CommandFailure):
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
    return str(store.snapshot().lifecycle.project.revision)
