from dataclasses import replace
from datetime import datetime
from typing import assert_never

from pinboard.domain import work_models
from pinboard.domain.authority_models import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AcquireInitialPreparationAuthority,
    AttemptAuthorityDecision,
    AttemptAuthorityOperation,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    CoordinationAuthorityDecision,
    CoordinationAuthorityOperation,
    InactiveAttemptAuthority,
    InactivePreparationAuthority,
    PreparationAuthorityDecision,
    PreparationAuthorityOperation,
    PreparationLeaseAuthority,
    PreparationLeaseStatus,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    ReleasePreparationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RenewPreparationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    RevokePreparationAuthority,
    TransferAttemptAuthority,
    TransferPreparationAuthority,
)
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, ItemId
from pinboard.domain.ledger import LedgerSnapshot


def _coordination_token(value: work_models.CoordinationLeaseAuthority) -> work_models.CoordinationCommandAuthority:
    return work_models.CoordinationCommandAuthority(
        value.host_epoch,
        value.task_id,
        value.host_id,
        value.lease_id,
        value.generation,
        value.expires_at,
    )


def decide_coordination_authority(  # noqa: C901, PLR0912
    retained: work_models.CoordinationLeaseAuthority | None,
    operation: CoordinationAuthorityOperation,
) -> DecisionResult[CoordinationAuthorityDecision]:
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
                and retained.state == work_models.CoordinationLeaseStatus.ACTIVE
                and retained.expires_at > acquired_at
            ):
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_BUSY,
                    "Another task retains live coordination authority.",
                )
            generation = 1 if retained is None else retained.generation + 1
            return CoordinationAuthorityDecision(
                retained,
                work_models.CoordinationLeaseAuthority(
                    host_epoch,
                    task_id,
                    host_id,
                    lease_id,
                    generation,
                    acquired_at,
                    expires_at,
                    work_models.CoordinationLeaseStatus.ACTIVE,
                ),
            )
        case RenewCoordinationAuthority(authority=authority, renewed_at=renewed_at, expires_at=expires_at):
            if retained is None:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority does not exist.",
                )
            if (
                retained.state != work_models.CoordinationLeaseStatus.ACTIVE
                or _coordination_token(retained) != authority
            ):
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
            if (
                retained.state != work_models.CoordinationLeaseStatus.ACTIVE
                or _coordination_token(retained) != authority
            ):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Coordination authority is fenced.")
            if retained.expires_at <= released_at:
                return DecisionFailure(
                    DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                    "Coordination authority has expired.",
                )
            return CoordinationAuthorityDecision(
                retained,
                replace(retained, expires_at=released_at, state=work_models.CoordinationLeaseStatus.RELEASED),
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
                    state=work_models.CoordinationLeaseStatus.REVOKED,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _attempt_token(value: AttemptLeaseAuthority) -> work_models.CommandAttemptAuthority:
    return work_models.CommandAttemptAuthority(
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


def _same_attempt_token(retained: AttemptLeaseAuthority, supplied: work_models.CommandAttemptAuthority) -> bool:
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


def decide_attempt_authority(  # noqa: C901, PLR0912
    retained: AttemptLeaseAuthority | None,
    counter: int,
    operation: AttemptAuthorityOperation,
    coordination: work_models.CoordinationLeaseAuthority | None,
    *,
    live_attempt: tuple[AttemptId, ItemId] | None = None,
    transferable_attempt: tuple[AttemptId, ItemId] | None = None,
    project_host_epoch: int | None = None,
) -> DecisionResult[AttemptAuthorityDecision]:
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
            return AttemptAuthorityDecision(attempt, 0, 1, retained, after)
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
            )
        case ReleaseAttemptAuthority(current=current, released_at=released_at):
            if (failure := _validate_attempt_change(retained, current, released_at)) is not None:
                return failure
            assert retained is not None
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
            )
        case _ as unreachable:
            assert_never(unreachable)


def _validate_attempt_change(
    retained: AttemptLeaseAuthority | None,
    current: work_models.CommandAttemptAuthority,
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
    retained: work_models.CoordinationLeaseAuthority | None,
    current: work_models.CoordinationCommandAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if (
        retained is None
        or retained.state != work_models.CoordinationLeaseStatus.ACTIVE
        or _coordination_token(retained) != current
        or retained.expires_at <= now
    ):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Coordination authority is not current.")
    return None


def _preparation_token(value: PreparationLeaseAuthority) -> work_models.PreparationCommandAuthority:
    return work_models.PreparationCommandAuthority(
        value.host_epoch,
        value.item,
        value.definition_revision,
        value.definition_digest,
        value.task_id,
        value.host_id,
        value.lease_id,
        value.generation,
        value.expires_at,
    )


def _validate_preparation_change(
    retained: PreparationLeaseAuthority | None,
    current: work_models.PreparationCommandAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority does not exist.")
    if retained.state != PreparationLeaseStatus.ACTIVE or _preparation_token(retained) != current:
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.")
    if retained.expires_at <= now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority has expired.")
    return None


def _validate_preparation_transfer(
    retained: PreparationLeaseAuthority | None,
    current: InactivePreparationAuthority,
    now: datetime,
) -> DecisionFailure | None:
    if retained is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority does not exist.")
    if retained.state == PreparationLeaseStatus.ACTIVE and retained.expires_at > now:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority remains live.")
    state = PreparationLeaseStatus.EXPIRED if retained.state == PreparationLeaseStatus.ACTIVE else retained.state
    expected = InactivePreparationAuthority(
        retained.host_epoch,
        retained.item,
        retained.definition_revision,
        retained.definition_digest,
        retained.task_id,
        retained.host_id,
        retained.lease_id,
        retained.generation,
        retained.expires_at,
        state,
    )
    if (
        state
        not in {
            PreparationLeaseStatus.EXPIRED,
            PreparationLeaseStatus.RELEASED,
            PreparationLeaseStatus.REVOKED,
        }
        or current != expected
    ):
        return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.")
    return None


def decide_preparation_authority(  # noqa: C901, PLR0912
    retained: PreparationLeaseAuthority | None,
    counter: int,
    operation: PreparationAuthorityOperation,
    snapshot: LedgerSnapshot | None,
    now: datetime,
) -> DecisionResult[PreparationAuthorityDecision]:
    match operation:
        case AcquireInitialPreparationAuthority(
            host_epoch=host_epoch,
            item=item,
            expected_project_revision=expected_project_revision,
            expected_item_subject_revision=expected_item_subject_revision,
            expected_definition_revision=expected_definition_revision,
            expected_definition_digest=expected_definition_digest,
            coordination=supplied_coordination,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if snapshot is None:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation requires ledger state.")
            item_value = snapshot.item(item)
            definition = snapshot.definition(item)
            if (
                snapshot.host_epoch != host_epoch
                or snapshot.revision != expected_project_revision
                or snapshot.subject_revision(item) != expected_item_subject_revision
                or item_value is None
                or item_value.state != work_models.WorkState.READY
                or any(dependency in snapshot.items_by_id() for dependency in item_value.depends_on)
                or definition is None
                or (definition.revision, definition.digest)
                != (expected_definition_revision, expected_definition_digest)
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Initial preparation requires the exact dependency-satisfied ready item and definition.",
                )
            if (failure := _validate_coordination(snapshot.coordination_lease, supplied_coordination, now)) is not None:
                return failure
            if counter != 0 or retained is not None:
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Initial preparation is already claimed.")
            if expires_at <= acquired_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Preparation authority requires a positive bounded interval.",
                )
            after = PreparationLeaseAuthority(
                host_epoch,
                item,
                definition.revision,
                definition.digest,
                task_id,
                host_id,
                lease_id,
                1,
                acquired_at,
                expires_at,
                PreparationLeaseStatus.ACTIVE,
            )
            return PreparationAuthorityDecision(item, 0, 1, None, after)
        case TransferPreparationAuthority(
            current=current,
            coordination=supplied_coordination,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ):
            if snapshot is None or retained is None:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation transfer is unavailable.")
            if (failure := _validate_preparation_transfer(retained, current, now)) is not None:
                return failure
            if (failure := _validate_coordination(snapshot.coordination_lease, supplied_coordination, now)) is not None:
                return failure
            item_value = snapshot.item(retained.item)
            definition = snapshot.definition(retained.item)
            if (
                item_value is None
                or item_value.state != work_models.WorkState.READY
                or any(dependency in snapshot.items_by_id() for dependency in item_value.depends_on)
                or definition is None
                or expires_at <= acquired_at
            ):
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation cannot be transferred.")
            after = replace(
                retained,
                definition_revision=definition.revision,
                definition_digest=definition.digest,
                task_id=task_id,
                host_id=host_id,
                lease_id=lease_id,
                generation=counter + 1,
                acquired_at=acquired_at,
                expires_at=expires_at,
                state=PreparationLeaseStatus.ACTIVE,
            )
            return PreparationAuthorityDecision(retained.item, counter, counter + 1, retained, after)
        case RenewPreparationAuthority(current=current, renewed_at=renewed_at, expires_at=expires_at):
            if (failure := _validate_preparation_change(retained, current, now)) is not None:
                return failure
            assert retained is not None
            if expires_at <= renewed_at or expires_at <= retained.expires_at:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Preparation renewal must extend its bounded expiry.",
                )
            return PreparationAuthorityDecision(
                retained.item, counter, counter, retained, replace(retained, expires_at=expires_at)
            )
        case ReleasePreparationAuthority(current=current, released_at=released_at):
            if (failure := _validate_preparation_change(retained, current, now)) is not None:
                return failure
            assert retained is not None
            return PreparationAuthorityDecision(
                retained.item,
                counter,
                counter + 1,
                retained,
                replace(
                    retained,
                    generation=counter + 1,
                    expires_at=released_at,
                    state=PreparationLeaseStatus.RELEASED,
                ),
            )
        case RevokePreparationAuthority(
            item=item,
            lease_id=lease_id,
            generation=generation,
            coordination=supplied_coordination,
            revoked_at=revoked_at,
        ):
            if snapshot is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation revocation is unavailable."
                )
            if (failure := _validate_coordination(snapshot.coordination_lease, supplied_coordination, now)) is not None:
                return failure
            if retained is None or (retained.item, retained.lease_id, retained.generation) != (
                item,
                lease_id,
                generation,
            ):
                return DecisionFailure(DecisionFailureCode.LEASE_FENCED, "Preparation authority is fenced.")
            return PreparationAuthorityDecision(
                item,
                counter,
                counter + 1,
                retained,
                replace(
                    retained,
                    generation=counter + 1,
                    expires_at=revoked_at,
                    state=PreparationLeaseStatus.REVOKED,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
