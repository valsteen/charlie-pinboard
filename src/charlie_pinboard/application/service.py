from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.domain.decisions import (
    Action,
    ActorAuthority,
    AuthorizationKind,
    Role,
    TransitionCommand,
    TransitionReceipt,
    command_action,
    decide,
    rediscover_action,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import AttemptId
from charlie_pinboard.domain.model import LedgerSnapshot


def _actor_for(
    snapshot: LedgerSnapshot,
    action: Action,
    now: datetime,
) -> ActorAuthority | DecisionFailure:
    match action.authorization:
        case AuthorizationKind.COORDINATOR:
            return ActorAuthority(Role.COORDINATOR, action.authorization, action.coordinator_generation)
        case AuthorizationKind.COORDINATION:
            authority = snapshot.coordination_authority
            if (
                authority is None
                or action.lease_id != authority.lease_id
                or action.coordinator_generation != authority.generation
                or authority.expires_at <= now
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "The supplied coordination authority is no longer current.",
                )
            return ActorAuthority(
                Role.COORDINATOR,
                action.authorization,
                action.coordinator_generation,
                action.lease_id,
            )
        case AuthorizationKind.ATTEMPT:
            authority = action.command_authority
            if (
                authority is None
                or action.lease_id != authority.lease_id
                or action.coordinator_generation != authority.generation
                or authority.expires_at <= now
                or authority not in snapshot.command_attempt_authorities
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "The supplied attempt authority is no longer current.",
                )
            return ActorAuthority(
                Role.WORKER,
                action.authorization,
                action.coordinator_generation,
                action.lease_id,
                (AttemptId(action.subject),),
                False,
            )
        case AuthorizationKind.OBSERVER:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                "Observer actions cannot mutate repository work.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def execute(
    store: WorkStore,
    command: TransitionCommand,
    now: datetime,
) -> TransitionReceipt | DecisionFailure:
    """Rediscover, decide, and persist one lifecycle mutation under one write lock."""

    supplied = command_action(command)
    with store.write() as transaction:
        snapshot = project_decision_snapshot(transaction.snapshot())
        result = _actor_for(snapshot, supplied, now)
        match result:
            case DecisionFailure():
                return result
            case actor:
                pass
        result = rediscover_action(snapshot, actor, supplied)
        match result:
            case DecisionFailure():
                return result
            case Action():
                pass
        result = decide(snapshot, command, now)
        match result:
            case DecisionFailure():
                return result
            case decision:
                return transaction.commit(decision)
