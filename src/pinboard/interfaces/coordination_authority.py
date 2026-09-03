import sys
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.service import change_coordination_authority as apply_coordination_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CommandFailure, CommandResult

type CoordinationAuthorityCommand = (
    cli_commands.CoordinationAcquireCommand
    | cli_commands.CoordinationRenewCommand
    | cli_commands.CoordinationReleaseCommand
    | cli_commands.CoordinationRevokeCommand
    | cli_commands.CoordinationStatusCommand
)


def coordination_values(roots: cli_commands.ResolvedRoots) -> dict[str, str | int] | None:
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    value = state.authority.coordination
    if value is None:
        return None
    return {
        "task_id": str(value.task_id),
        "host_id": str(value.host_id),
        "lease_id": str(value.lease_id),
        "generation": value.generation,
        "acquired_at": value.acquired_at.isoformat(),
        "expires_at": value.expires_at.isoformat(),
        "status": value.state.value,
    }


def emit_coordination(roots: cli_commands.ResolvedRoots, *, json: bool) -> int:
    values = coordination_values(roots)
    if json:
        write_json({"lease": None} if values is None else values)
    elif values is None:
        print("OK COORDINATION_AVAILABLE")
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def retained_coordination(
    state: stored_state.StoredWorkState,
) -> CommandResult[stored_state.StoredCoordinationLease]:
    current = state.authority.coordination
    if current is None:
        return CommandFailure(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, "Coordination authority does not exist.")
    return current


def _supplied_coordination_authority(
    state: stored_state.StoredWorkState,
    current: stored_state.StoredCoordinationLease,
    lease_id: LeaseId,
    generation: int,
) -> work_models.CoordinationCommandAuthority:
    return work_models.CoordinationCommandAuthority(
        state.lifecycle.project.host_epoch,
        current.task_id,
        current.host_id,
        lease_id,
        generation,
        current.expires_at,
    )


def change_coordination_authority(
    roots: cli_commands.ResolvedRoots,
    command: CoordinationAuthorityCommand,
) -> CommandResult[int]:
    if isinstance(command, cli_commands.CoordinationStatusCommand):
        return emit_coordination(roots, json=command.json)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    state = store.snapshot()
    now = datetime.now(UTC)
    match command:
        case cli_commands.CoordinationAcquireCommand(task_id=task_id, host_id=host_id, ttl_seconds=ttl_seconds):
            authority_operation = authority_models.AcquireCoordinationAuthority(
                state.lifecycle.project.host_epoch,
                task_id,
                host_id,
                LeaseId(uuid4().hex),
                now,
                now + timedelta(seconds=ttl_seconds),
            )
        case cli_commands.CoordinationRenewCommand(lease_id=lease_id, generation=generation, ttl_seconds=ttl_seconds):
            current = retained_coordination(state)
            if isinstance(current, CommandFailure):
                return current
            authority_operation = authority_models.RenewCoordinationAuthority(
                _supplied_coordination_authority(state, current, lease_id, generation),
                now,
                now + timedelta(seconds=ttl_seconds),
            )
        case cli_commands.CoordinationReleaseCommand(lease_id=lease_id, generation=generation):
            current = retained_coordination(state)
            if isinstance(current, CommandFailure):
                return current
            authority_operation = authority_models.ReleaseCoordinationAuthority(
                _supplied_coordination_authority(state, current, lease_id, generation), now
            )
        case cli_commands.CoordinationRevokeCommand():
            current = retained_coordination(state)
            if isinstance(current, CommandFailure):
                return current
            authority_operation = authority_models.RevokeCoordinationAuthority(
                current.lease_id, current.generation, now
            )
        case _ as unreachable:
            assert_never(unreachable)
    result = apply_coordination_authority_change(store, authority_operation)
    if isinstance(result, DecisionFailure):
        return CommandFailure(result.code, result.message)
    view_result = work_views.refresh(
        roots,
        store,
        AffectedViews(queue=True, current_focus=True, history=True),
        datetime.now(UTC),
    )
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    return emit_coordination(roots, json=command.json)
