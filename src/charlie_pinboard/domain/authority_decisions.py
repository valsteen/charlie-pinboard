from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import assert_never

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId
from charlie_pinboard.domain.model import (
    AttemptTaskUse,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    CoordinationLeaseAuthority,
    CoordinationLeaseStatus,
    UseLeaseGenerationKind,
    UseLeaseState,
)


@dataclass(frozen=True, slots=True)
class AcquireCoordinationAuthority:
    host_epoch: int
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewCoordinationAuthority:
    authority: CoordinationCommandAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseCoordinationAuthority:
    authority: CoordinationCommandAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeCoordinationAuthority:
    lease_id: LeaseId
    generation: int
    revoked_at: datetime


type CoordinationAuthorityOperation = (
    AcquireCoordinationAuthority
    | RenewCoordinationAuthority
    | ReleaseCoordinationAuthority
    | RevokeCoordinationAuthority
)


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityDecision:
    before: CoordinationLeaseAuthority | None
    after: CoordinationLeaseAuthority


def _coordination_token(value: CoordinationLeaseAuthority) -> CoordinationCommandAuthority:
    return CoordinationCommandAuthority(
        value.host_epoch,
        value.task_id,
        value.host_id,
        value.lease_id,
        value.generation,
        value.expires_at,
    )


def decide_coordination_authority(  # noqa: C901, PLR0912
    retained: CoordinationLeaseAuthority | None,
    operation: CoordinationAuthorityOperation,
) -> CoordinationAuthorityDecision | DecisionFailure:
    match operation:
        case AcquireCoordinationAuthority(
            host_epoch=host_epoch,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Coordination authority requires a positive bounded interval.",
                )
            if (
                retained is not None
                and retained.state == CoordinationLeaseStatus.ACTIVE
                and retained.expires_at > acquired_at
            ):
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_BUSY,
                    "Another task retains live coordination authority.",
                )
            generation = 1 if retained is None else retained.generation + 1
            return CoordinationAuthorityDecision(
                retained,
                CoordinationLeaseAuthority(
                    host_epoch,
                    task_id,
                    host_id,
                    lease_id,
                    generation,
                    acquired_at,
                    expires_at,
                    CoordinationLeaseStatus.ACTIVE,
                ),
            )
        case RenewCoordinationAuthority(authority=authority, renewed_at=renewed_at, expires_at=expires_at):
            if retained is None:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority does not exist.",
                )
            if retained.state != CoordinationLeaseStatus.ACTIVE or _coordination_token(retained) != authority:
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Coordination authority is fenced.")
            if retained.expires_at <= renewed_at:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority has expired.",
                )
            if expires_at <= renewed_at or expires_at <= retained.expires_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Coordination renewal must extend its bounded expiry.",
                )
            return CoordinationAuthorityDecision(retained, replace(retained, expires_at=expires_at))
        case ReleaseCoordinationAuthority(authority=authority, released_at=released_at):
            if retained is None:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority does not exist.",
                )
            if retained.state != CoordinationLeaseStatus.ACTIVE or _coordination_token(retained) != authority:
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Coordination authority is fenced.")
            if retained.expires_at <= released_at:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority has expired.",
                )
            return CoordinationAuthorityDecision(
                retained,
                replace(retained, expires_at=released_at, state=CoordinationLeaseStatus.RELEASED),
            )
        case RevokeCoordinationAuthority(lease_id=lease_id, generation=generation, revoked_at=revoked_at):
            if retained is None:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority does not exist.",
                )
            if (retained.lease_id, retained.generation) != (lease_id, generation):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Coordination authority is fenced.")
            return CoordinationAuthorityDecision(
                retained,
                replace(
                    retained,
                    generation=retained.generation + 1,
                    expires_at=revoked_at,
                    state=CoordinationLeaseStatus.REVOKED,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)


class AttemptLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AttemptLeaseAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class InactiveAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class AcquireInitialAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransferAttemptAuthority:
    current: InactiveAttemptAuthority
    coordination: CoordinationCommandAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewAttemptAuthority:
    current: CommandAttemptAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseAttemptAuthority:
    current: CommandAttemptAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeAttemptAuthority:
    attempt: AttemptId
    lease_id: LeaseId
    generation: int
    coordination: CoordinationCommandAuthority
    revoked_at: datetime


type AttemptAuthorityOperation = (
    AcquireInitialAttemptAuthority
    | TransferAttemptAuthority
    | RenewAttemptAuthority
    | ReleaseAttemptAuthority
    | RevokeAttemptAuthority
)


@dataclass(frozen=True, slots=True)
class AttemptAuthorityDecision:
    attempt: AttemptId
    counter_before: int
    counter_after: int
    current_before: AttemptLeaseAuthority | None
    current_after: AttemptLeaseAuthority
    fenced_task_uses: tuple[AttemptTaskUse, ...]


def _attempt_token(value: AttemptLeaseAuthority) -> CommandAttemptAuthority:
    return CommandAttemptAuthority(
        value.host_epoch,
        value.item,
        "",
        value.attempt,
        "",
        value.task_id,
        value.host_id,
        value.lease_id,
        value.generation,
        value.expires_at,
    )


def _same_attempt_token(retained: AttemptLeaseAuthority, supplied: CommandAttemptAuthority) -> bool:
    token = _attempt_token(retained)
    return (
        token.host_epoch,
        token.item,
        token.attempt,
        token.task_id,
        token.host_id,
        token.lease_id,
        token.generation,
        token.expires_at,
    ) == (
        supplied.host_epoch,
        supplied.item,
        supplied.attempt,
        supplied.task_id,
        supplied.host_id,
        supplied.lease_id,
        supplied.generation,
        supplied.expires_at,
    )


def _fenced_task_uses(
    uses: tuple[AttemptTaskUse, ...],
    attempt: AttemptId,
) -> tuple[AttemptTaskUse, ...]:
    current = tuple(
        value
        for value in uses
        if value.attempt_id == attempt
        and value.state == UseLeaseState.ACTIVE
        and value.generation_kind == UseLeaseGenerationKind.GRANT
        and not any(
            later.reservation_id == value.reservation_id and later.generation > value.generation for later in uses
        )
    )
    return tuple(sorted(current, key=_task_use_key))


def _task_use_key(value: AttemptTaskUse) -> tuple[str, int]:
    return str(value.reservation_id), value.generation


def decide_attempt_authority(  # noqa: C901, PLR0912
    retained: AttemptLeaseAuthority | None,
    counter: int,
    task_uses: tuple[AttemptTaskUse, ...],
    operation: AttemptAuthorityOperation,
    coordination: CoordinationLeaseAuthority | None,
    planned_intent_attempts: tuple[AttemptId, ...] = (),
    *,
    live_attempt: tuple[AttemptId, ItemId] | None = None,
    transferable_attempt: tuple[AttemptId, ItemId] | None = None,
    project_host_epoch: int | None = None,
    recovery_pending_attempts: tuple[AttemptId, ...] = (),
) -> AttemptAuthorityDecision | DecisionFailure:
    match operation:
        case AcquireInitialAttemptAuthority(
            host_epoch=host_epoch,
            attempt=attempt,
            item=item,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if live_attempt != (attempt, item) or project_host_epoch != host_epoch:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "Initial attempt authority requires the exact live attempt and project host epoch.",
                )
            if counter != 0 or (
                retained is not None and (retained.generation != 0 or retained.state != AttemptLeaseStatus.RELEASED)
            ):
                return DecisionFailure(
                    DecisionFailureCode.LEASE_FENCED, "Initial attempt authority is already claimed."
                )
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Attempt authority requires a positive bounded interval.",
                )
            after = AttemptLeaseAuthority(
                host_epoch,
                attempt,
                item,
                task_id,
                host_id,
                lease_id,
                1,
                acquired_at,
                expires_at,
                AttemptLeaseStatus.ACTIVE,
            )
            return AttemptAuthorityDecision(attempt, 0, 1, retained, after, ())
        case TransferAttemptAuthority(
            current=current,
            coordination=supplied_coordination,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if retained is None or transferable_attempt != (retained.attempt, retained.item):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "Attempt transfer requires the exact retained nonterminal attempt.",
                )
            if (failure := _validate_attempt_transfer(retained, current, acquired_at)) is not None:
                return failure
            if retained is not None and retained.attempt in planned_intent_attempts:
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED,
                    "Attempt authority cannot transfer with a planned mutation intent.",
                )
            if retained is not None and (
                retained.attempt in recovery_pending_attempts or _fenced_task_uses(task_uses, retained.attempt)
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
                    "Attempt transfer requires completed ordinary authority and resource recovery.",
                )
            if (failure := _validate_coordination(coordination, supplied_coordination, acquired_at)) is not None:
                return failure
            assert retained is not None
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Transferred attempt authority requires a positive bounded interval.",
                )
            after = replace(
                retained,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=counter + 1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=AttemptLeaseStatus.ACTIVE,
            )
            return AttemptAuthorityDecision(
                retained.attempt,
                counter,
                counter + 1,
                retained,
                after,
                _fenced_task_uses(task_uses, retained.attempt),
            )
        case RenewAttemptAuthority(current=current, renewed_at=renewed_at, expires_at=expires_at):
            if (failure := _validate_attempt_change(retained, current, renewed_at)) is not None:
                return failure
            assert retained is not None
            if expires_at <= retained.expires_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Attempt renewal must extend its bounded expiry.",
                )
            return AttemptAuthorityDecision(
                retained.attempt,
                counter,
                counter,
                retained,
                replace(retained, expires_at=expires_at),
                (),
            )
        case ReleaseAttemptAuthority(current=current, released_at=released_at):
            if (failure := _validate_attempt_change(retained, current, released_at)) is not None:
                return failure
            assert retained is not None
            if retained.attempt in planned_intent_attempts:
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED,
                    "Attempt authority cannot release with a planned mutation intent.",
                )
            after = replace(
                retained,
                generation=counter + 1,
                expires_at=released_at,
                state=AttemptLeaseStatus.RELEASED,
            )
            return AttemptAuthorityDecision(
                retained.attempt,
                counter,
                counter + 1,
                retained,
                after,
                _fenced_task_uses(task_uses, retained.attempt),
            )
        case RevokeAttemptAuthority(
            attempt=attempt,
            lease_id=lease_id,
            generation=generation,
            coordination=supplied_coordination,
            revoked_at=revoked_at,
        ):
            if (failure := _validate_coordination(coordination, supplied_coordination, revoked_at)) is not None:
                return failure
            if retained is None or (retained.attempt, retained.lease_id, retained.generation) != (
                attempt,
                lease_id,
                generation,
            ):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.")
            after = replace(
                retained,
                generation=counter + 1,
                expires_at=revoked_at,
                state=AttemptLeaseStatus.REVOKED,
            )
            return AttemptAuthorityDecision(
                attempt,
                counter,
                counter + 1,
                retained,
                after,
                _fenced_task_uses(task_uses, attempt),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _validate_attempt_change(
    retained: AttemptLeaseAuthority | None,
    current: CommandAttemptAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority does not exist.")
    if retained.state != AttemptLeaseStatus.ACTIVE or not _same_attempt_token(retained, current):
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.")
    if retained.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_EXPIRED, "Attempt authority has expired.")
    return None


def _validate_attempt_transfer(
    retained: AttemptLeaseAuthority | None,
    current: InactiveAttemptAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority does not exist.")
    if retained.state == AttemptLeaseStatus.ACTIVE and retained.expires_at > now:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority remains live.")
    expected_state = AttemptLeaseStatus.EXPIRED if retained.state == AttemptLeaseStatus.ACTIVE else retained.state
    if expected_state not in {
        AttemptLeaseStatus.RELEASED,
        AttemptLeaseStatus.REVOKED,
        AttemptLeaseStatus.EXPIRED,
    }:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not inactive.")
    expected = InactiveAttemptAuthority(
        retained.host_epoch,
        retained.attempt,
        retained.item,
        retained.task_id,
        retained.host_id,
        retained.lease_id,
        retained.generation,
        retained.expires_at,
        expected_state,
    )
    if current != expected:
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Attempt authority is fenced.")
    return None


def _validate_coordination(
    retained: CoordinationLeaseAuthority | None,
    current: CoordinationCommandAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if (
        retained is None
        or retained.state != CoordinationLeaseStatus.ACTIVE
        or _coordination_token(retained) != current
        or retained.expires_at <= now
    ):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Coordination authority is not current.")
    return None
