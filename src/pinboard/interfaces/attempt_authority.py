import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot, project_inactive_attempt_authority
from pinboard.application.service import change_attempt_authority as apply_attempt_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


def _emit_attempt_authority(
    state: stored_state.StoredWorkState, attempt_id: AttemptId, *, json: bool
) -> CommandResult[int]:
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    if lease is None:
        return CommandFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' has no retained authority."
        )
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        return CommandFailure(CommandErrorCode.WORK_STATE_INVALID, "Attempt authority has no exact identity anchor.")
    values: dict[str, str | int] = {
        "attempt_id": str(attempt_id),
        "task_id": str(anchor.task_id),
        "host_id": str(anchor.host_id),
        "lease_id": str(anchor.lease_id),
        "generation": lease.generation,
        "acquired_at": lease.acquired_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "status": lease.state.value,
    }
    if json:
        write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def attempt_status(roots: cli_commands.ResolvedRoots, command: cli_commands.AttemptStatusCommand) -> CommandResult[int]:
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    return _emit_attempt_authority(state, command.attempt_id, json=command.json)


def _current_attempt(
    state: stored_state.StoredWorkState, attempt_id: AttemptId
) -> CommandResult[stored_state.StoredAttempt]:
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if attempt is None:
        return CommandFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' is not current.")
    return attempt


def _attempt_acquire_operation(
    state: stored_state.StoredWorkState,
    attempt: stored_state.StoredAttempt,
    command: cli_commands.AttemptAcquireCommand | cli_commands.CoordinatedAttemptAcquireCommand,
    now: datetime,
) -> CommandResult[authority_models.AttemptAuthorityOperation]:
    attempt_id = command.attempt_id
    retained_record = next(
        (value for value in state.authority.attempt_leases if value.attempt_id == attempt_id),
        None,
    )
    lease_id = LeaseId(uuid4().hex)
    if retained_record is None:
        return authority_models.AcquireInitialAttemptAuthority(
            state.lifecycle.project.host_epoch,
            attempt_id,
            attempt.item_id,
            command.task_id,
            command.host_id,
            lease_id,
            now,
            now + timedelta(seconds=command.ttl_seconds),
        )
    inactive = project_inactive_attempt_authority(state, attempt_id, now)
    if isinstance(inactive, DecisionFailure):
        return CommandFailure(inactive.code, inactive.message)
    coordination = state.authority.coordination
    if coordination is None:
        return CommandFailure(
            DecisionFailureCode.COORDINATION_LEASE_REQUIRED, "Attempt reacquisition requires coordination."
        )
    if isinstance(command, cli_commands.AttemptAcquireCommand):
        return CommandFailure(
            DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
            "Attempt reacquisition requires the exact coordination lease and generation.",
        )
    return authority_models.TransferAttemptAuthority(
        inactive,
        work_models.CoordinationCommandAuthority(
            state.lifecycle.project.host_epoch,
            coordination.task_id,
            coordination.host_id,
            command.coordination_lease_id,
            command.coordination_generation,
            coordination.expires_at,
        ),
        command.task_id,
        command.host_id,
        lease_id,
        now,
        now + timedelta(seconds=command.ttl_seconds),
    )


def _attempt_renew_operation(
    state: stored_state.StoredWorkState,
    command: cli_commands.AttemptRenewCommand,
    now: datetime,
) -> CommandResult[authority_models.RenewAttemptAuthority]:
    retained = next(
        (
            value
            for value in project_decision_snapshot(state, now).command_attempt_authorities
            if value.attempt == command.attempt_id
        ),
        None,
    )
    if retained is None:
        return CommandFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.")
    return authority_models.RenewAttemptAuthority(
        replace(retained, lease_id=command.lease_id, generation=command.generation),
        now,
        now + timedelta(seconds=command.ttl_seconds),
    )


def _attempt_release_operation(
    state: stored_state.StoredWorkState,
    command: cli_commands.AttemptReleaseCommand,
    now: datetime,
) -> CommandResult[authority_models.ReleaseAttemptAuthority]:
    retained = next(
        (
            value
            for value in project_decision_snapshot(state, now).command_attempt_authorities
            if value.attempt == command.attempt_id
        ),
        None,
    )
    if retained is None:
        return CommandFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.")
    return authority_models.ReleaseAttemptAuthority(
        replace(retained, lease_id=command.lease_id, generation=command.generation),
        now,
    )


def _attempt_revoke_operation(
    state: stored_state.StoredWorkState,
    command: cli_commands.AttemptRevokeCommand,
    now: datetime,
) -> CommandResult[authority_models.RevokeAttemptAuthority]:
    coordination = state.authority.coordination
    if coordination is None:
        return CommandFailure(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, "Coordination authority is absent.")
    return authority_models.RevokeAttemptAuthority(
        command.attempt_id,
        command.lease_id,
        command.generation,
        work_models.CoordinationCommandAuthority(
            state.lifecycle.project.host_epoch,
            coordination.task_id,
            coordination.host_id,
            command.coordination_lease_id,
            command.coordination_generation,
            coordination.expires_at,
        ),
        now,
    )


def change_attempt_authority(
    roots: cli_commands.ResolvedRoots,
    command: (
        cli_commands.AttemptAcquireCommand
        | cli_commands.CoordinatedAttemptAcquireCommand
        | cli_commands.AttemptRenewCommand
        | cli_commands.AttemptReleaseCommand
        | cli_commands.AttemptRevokeCommand
    ),
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    state = store.snapshot()
    attempt = _current_attempt(state, command.attempt_id)
    if isinstance(attempt, CommandFailure):
        return attempt
    now = datetime.now(UTC)
    match command:
        case cli_commands.AttemptAcquireCommand() | cli_commands.CoordinatedAttemptAcquireCommand():
            authority_operation = _attempt_acquire_operation(state, attempt, command, now)
        case cli_commands.AttemptRenewCommand():
            authority_operation = _attempt_renew_operation(state, command, now)
        case cli_commands.AttemptReleaseCommand():
            authority_operation = _attempt_release_operation(state, command, now)
        case cli_commands.AttemptRevokeCommand():
            authority_operation = _attempt_revoke_operation(state, command, now)
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(authority_operation, CommandFailure):
        return authority_operation
    result = apply_attempt_authority_change(store, authority_operation)
    if isinstance(result, DecisionFailure):
        return CommandFailure(result.code, result.message)
    refresh_result = work_views.refresh(
        roots,
        store,
        AffectedViews(queue=True, current_focus=True, history=True),
        datetime.now(UTC),
    )
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    return _emit_attempt_authority(store.snapshot(), command.attempt_id, json=command.json)
