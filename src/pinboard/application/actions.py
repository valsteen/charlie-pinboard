from datetime import datetime

from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.ports import WorkStore
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, LeaseId


def _worker_attempts(
    store_state: stored_state.StoredWorkState,
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
        and lease.state == authority_models.AttemptLeaseStatus.ACTIVE
        and lease.expires_at > now
        and (anchor := anchors.get((lease.attempt_id, lease.generation))) is not None
        and anchor.lease_id == lease_id
    )


def discover_actions(
    store: WorkStore,
    role: decision_models.Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
    now: datetime,
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    state = store.snapshot()
    snapshot = project_decision_snapshot(state, now)
    current = now
    selected_generation = generation if generation is not None else snapshot.generation
    match role:
        case decision_models.Role.OBSERVER:
            actor = decision_models.ObserverActorAuthority()
        case decision_models.Role.COORDINATOR:
            coordination = state.authority.coordination
            if lease_id is not None:
                if (
                    coordination is None
                    or coordination.state != work_models.CoordinationLeaseStatus.ACTIVE
                    or coordination.lease_id != lease_id
                    or coordination.generation != selected_generation
                    or coordination.expires_at <= current
                ):
                    return DecisionFailure(
                        DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                        "The coordination lease is not current.",
                    )
                authorization = decision_models.AuthorizationKind.COORDINATION
            else:
                authorization = decision_models.AuthorizationKind.COORDINATOR
            actor = decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, authorization, selected_generation, lease_id
            )
        case decision_models.Role.WORKER:
            attempts = _worker_attempts(state, lease_id, selected_generation, current)
            actor = decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                decision_models.AuthorizationKind.ATTEMPT,
                selected_generation,
                lease_id,
                attempts,
                False,
            )
        case decision_models.Role.PREPARER:
            if lease_id is None:
                preparations = ()
            else:
                anchors = {
                    (value.item_id, value.generation): value for value in state.authority.preparation_generations
                }
                preparations = tuple(
                    lease.item_id
                    for lease in state.authority.preparation_leases
                    if lease.generation == selected_generation
                    and lease.state == authority_models.PreparationLeaseStatus.ACTIVE
                    and lease.expires_at > current
                    and (anchor := anchors.get((lease.item_id, lease.generation))) is not None
                    and anchor.lease_id == lease_id
                )
            actor = decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                decision_models.AuthorizationKind.PREPARATION,
                selected_generation,
                lease_id,
                preparations=preparations,
            )
    return available_actions(snapshot, actor)
