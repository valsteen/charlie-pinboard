import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import uuid4

from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.service import change_preparation_authority as apply_preparation_authority_change
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, LeaseId
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


def _retained(
    state: stored_state.StoredWorkState, item_id: ItemId, now: datetime
) -> CommandResult[tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration]]:
    retained = stored_state.retained_preparation(state, item_id)
    if retained is None:
        return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, f"Item '{item_id}' has no preparation claim.")
    lease, anchor = retained
    if anchor is None:
        return CommandFailure(CommandErrorCode.WORK_STATE_INVALID, "Preparation authority has no identity anchor.")
    if lease.state == authority_models.PreparationLeaseStatus.ACTIVE and lease.expires_at <= now:
        lease = replace(lease, state=authority_models.PreparationLeaseStatus.EXPIRED)
    return lease, anchor


def _emit(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    now: datetime,
    *,
    json: bool,
) -> CommandResult[int]:
    retained = _retained(state, item_id, now)
    if isinstance(retained, CommandFailure):
        return retained
    lease, anchor = retained
    values: dict[str, str | int] = {
        "item_id": str(item_id),
        "definition_revision": lease.definition_revision,
        "definition_digest": lease.definition_digest,
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


def preparation_status(
    roots: cli_commands.ResolvedRoots, command: cli_commands.PreparationStatusCommand
) -> CommandResult[int]:
    now = datetime.now(UTC)
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    return _emit(state, command.item_id, now, json=command.json)


def _coordination(
    state: stored_state.StoredWorkState,
    lease_id: LeaseId,
    generation: int,
) -> CommandResult[work_models.CoordinationCommandAuthority]:
    retained = state.authority.coordination
    if retained is None:
        return CommandFailure(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, "Coordination authority is absent.")
    return work_models.CoordinationCommandAuthority(
        state.lifecycle.project.host_epoch,
        retained.task_id,
        retained.host_id,
        lease_id,
        generation,
        retained.expires_at,
    )


def _current_token(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    now: datetime,
) -> CommandResult[work_models.PreparationCommandAuthority]:
    token = next(
        (
            value
            for value in project_decision_snapshot(state, now).command_preparation_authorities
            if value.item == item_id
        ),
        None,
    )
    if token is None:
        return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority is not active.")
    return replace(token, lease_id=lease_id, generation=generation)


def _operation(  # noqa: C901, PLR0912
    state: stored_state.StoredWorkState,
    command: (
        cli_commands.CoordinatorPreparationAcquireCommand
        | cli_commands.CoordinatedPreparationTransferCommand
        | cli_commands.PreparationRenewCommand
        | cli_commands.PreparationReleaseCommand
        | cli_commands.PreparationRevokeCommand
    ),
    now: datetime,
) -> CommandResult[authority_models.PreparationAuthorityOperation]:
    match command:
        case cli_commands.CoordinatorPreparationAcquireCommand():
            coordination = _coordination(state, command.coordination_lease_id, command.coordination_generation)
            if isinstance(coordination, CommandFailure):
                return coordination
            return authority_models.AcquireInitialPreparationAuthority(
                state.lifecycle.project.host_epoch,
                command.item_id,
                command.expected_project_revision,
                command.expected_item_subject_revision,
                command.expected_definition_revision,
                command.expected_definition_digest,
                coordination,
                command.task_id,
                command.host_id,
                LeaseId(uuid4().hex),
                now,
                now + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.CoordinatedPreparationTransferCommand():
            retained = _retained(state, command.item_id, now)
            if isinstance(retained, CommandFailure):
                return retained
            lease, anchor = retained
            if lease.state == authority_models.PreparationLeaseStatus.ACTIVE:
                return CommandFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority remains live.")
            coordination = _coordination(state, command.coordination_lease_id, command.coordination_generation)
            if isinstance(coordination, CommandFailure):
                return coordination
            return authority_models.TransferPreparationAuthority(
                authority_models.InactivePreparationAuthority(
                    state.lifecycle.project.host_epoch,
                    lease.item_id,
                    lease.definition_revision,
                    lease.definition_digest,
                    anchor.task_id,
                    anchor.host_id,
                    anchor.lease_id,
                    lease.generation,
                    lease.expires_at,
                    lease.state,
                ),
                coordination,
                command.task_id,
                command.host_id,
                LeaseId(uuid4().hex),
                now,
                now + timedelta(seconds=command.ttl_seconds),
            )
        case cli_commands.PreparationRenewCommand():
            current = _current_token(state, command.item_id, command.lease_id, command.generation, now)
            if isinstance(current, CommandFailure):
                return current
            return authority_models.RenewPreparationAuthority(
                current, now, now + timedelta(seconds=command.ttl_seconds)
            )
        case cli_commands.PreparationReleaseCommand():
            current = _current_token(state, command.item_id, command.lease_id, command.generation, now)
            if isinstance(current, CommandFailure):
                return current
            return authority_models.ReleasePreparationAuthority(current, now)
        case cli_commands.PreparationRevokeCommand():
            coordination = _coordination(state, command.coordination_lease_id, command.coordination_generation)
            if isinstance(coordination, CommandFailure):
                return coordination
            return authority_models.RevokePreparationAuthority(
                command.item_id,
                command.lease_id,
                command.generation,
                coordination,
                now,
            )
        case _ as unreachable:
            assert_never(unreachable)


def change_preparation_authority(
    roots: cli_commands.ResolvedRoots,
    command: (
        cli_commands.CoordinatorPreparationAcquireCommand
        | cli_commands.CoordinatedPreparationTransferCommand
        | cli_commands.PreparationRenewCommand
        | cli_commands.PreparationReleaseCommand
        | cli_commands.PreparationRevokeCommand
    ),
) -> CommandResult[int]:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    state = store.snapshot()
    operation_time = datetime.now(UTC)
    operation = _operation(state, command, operation_time)
    if isinstance(operation, CommandFailure):
        return operation
    result = apply_preparation_authority_change(store, operation)
    if isinstance(result, DecisionFailure):
        return CommandFailure(result.code, result.message)
    refresh_result = work_views.refresh(
        roots,
        store,
        AffectedViews(queue=True, items=(command.item_id,), current_focus=True, history=True),
        datetime.now(UTC),
    )
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    render_time = datetime.now(UTC)
    return _emit(store.snapshot(), command.item_id, render_time, json=command.json)
