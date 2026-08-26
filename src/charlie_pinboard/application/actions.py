from datetime import UTC, datetime

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.errors import ActionQueryError
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.authority_models import AttemptLeaseStatus
from charlie_pinboard.domain.decision_models import (
    Action,
    ActorAuthority,
    AuthorizationKind,
    Role,
)
from charlie_pinboard.domain.decisions import available_actions
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import AttemptId, LeaseId
from charlie_pinboard.domain.work_models import CoordinationLeaseStatus


def _worker_attempts(
    store_state: StoredWorkState,
    lease_id: LeaseId | None,
    generation: int,
    now: datetime,
) -> tuple[AttemptId, ...]:
    if lease_id is None:
        return ()
    anchors = {(value.attempt_id, value.generation): value for value in store_state.authority.attempt_generations}
    return tuple(
        lease.attempt_id
        for lease in store_state.authority.attempt_leases
        if lease.generation == generation
        and lease.state == AttemptLeaseStatus.ACTIVE
        and lease.expires_at > now
        and (anchor := anchors.get((lease.attempt_id, lease.generation))) is not None
        and anchor.lease_id == lease_id
    )


def discover_actions(
    store: WorkStore,
    role: Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
    now: datetime | None = None,
) -> tuple[Action, ...]:
    state = store.snapshot()
    snapshot = project_decision_snapshot(state)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    selected_generation = generation if generation is not None else snapshot.generation
    match role:
        case Role.OBSERVER:
            actor = ActorAuthority(Role.OBSERVER, AuthorizationKind.OBSERVER, 0)
        case Role.COORDINATOR:
            coordination = state.authority.coordination
            if lease_id is not None:
                if (
                    coordination is None
                    or coordination.state != CoordinationLeaseStatus.ACTIVE
                    or coordination.lease_id != lease_id
                    or coordination.generation != selected_generation
                    or coordination.expires_at <= current
                ):
                    raise ActionQueryError(
                        DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                        "The coordination lease is not current.",
                    )
                authorization = AuthorizationKind.COORDINATION
            else:
                authorization = AuthorizationKind.COORDINATOR
            actor = ActorAuthority(Role.COORDINATOR, authorization, selected_generation, lease_id)
        case Role.WORKER:
            attempts = _worker_attempts(state, lease_id, selected_generation, current)
            actor = ActorAuthority(
                Role.WORKER,
                AuthorizationKind.ATTEMPT,
                selected_generation,
                lease_id,
                attempts,
                False,
            )
    result = available_actions(snapshot, actor)
    if isinstance(result, DecisionFailure):
        raise ActionQueryError(result.code, result.message)
    return result
