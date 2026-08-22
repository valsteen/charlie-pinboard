from dataclasses import dataclass, replace
from enum import Enum

from repo_work.domain_errors import DecisionError, DecisionErrorCode
from repo_work.identifiers import (
    AttemptId,
    HostId,
    LeaseId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
)
from repo_work.model import (
    LedgerSnapshot,
    ReservationState,
    ResourceAuthority,
    ResourceReservation,
    ResourceUseLease,
    UseLeaseState,
)


@dataclass(frozen=True, slots=True)
class ResourceToken:
    resource_id: ResourceId
    host_id: HostId
    lease_id: LeaseId
    generation: int


class ResourceDecisionKind(Enum):
    ASSIGN = "assign"
    RELEASE = "release"
    REALLOCATE = "reallocate"
    REVOKE = "revoke"


@dataclass(frozen=True, slots=True)
class ReservationChange:
    before: ResourceReservation | None
    after: ResourceReservation


@dataclass(frozen=True, slots=True)
class ResourceUseLeaseChange:
    before: ResourceUseLease
    after: ResourceUseLease


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    kind: ResourceDecisionKind
    changes: tuple[ReservationChange, ...]


def validate_mutation_resources(
    snapshot: LedgerSnapshot,
    attempt: AttemptId,
    required_resources: tuple[ResourceId, ...],
    tokens: tuple[ResourceToken, ...],
) -> None:
    authorities = tuple(value for value in snapshot.attempt_authorities if value.attempt == attempt)
    if len(authorities) != 1 or authorities[0].lease_id is None:
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED, "Mutation requires one current attempt authority."
        )
    authority = authorities[0]
    if len(required_resources) != len(set(required_resources)):
        raise DecisionError(DecisionErrorCode.RESOURCE_REQUIREMENT_INVALID, "Required resources must be unique.")
    token_by_resource = {token.resource_id: token for token in tokens}
    if set(token_by_resource) != set(required_resources) or len(token_by_resource) != len(tokens):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Mutation requires one exact token per resource requirement.",
        )
    instances = {value.instance_id: value for value in snapshot.resource_instances}
    for resource_id in required_resources:
        reservation = next(
            (
                value
                for value in snapshot.resource_reservations
                if value.resource_id == resource_id
                and value.attempt == attempt
                and value.state == ReservationState.ACTIVE
            ),
            None,
        )
        if reservation is None:
            raise DecisionError(
                DecisionErrorCode.RESOURCE_RESERVATION_STALE,
                f"Resource '{resource_id}' is not reserved by this attempt.",
            )
        instance = instances.get(reservation.instance_id)
        token = token_by_resource[resource_id]
        if instance is None or instance.host_id != token.host_id:
            raise DecisionError(
                DecisionErrorCode.RESOURCE_INSTANCE_REQUIRED,
                f"Resource '{resource_id}' has no matching host-local instance.",
            )
        if (
            ResourceAuthority(token.resource_id, token.host_id, token.lease_id, token.generation)
            not in authority.resources
        ):
            raise DecisionError(
                DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
                f"Resource '{resource_id}' is not held by this attempt authority.",
            )
        use_lease = next(
            (
                value
                for value in snapshot.resource_use_leases
                if value.reservation_id == reservation.reservation_id
                and value.lease_id == token.lease_id
                and value.generation == token.generation
                and value.attempt_lease_id == authority.lease_id
                and value.attempt_generation == authority.generation
                and value.state == UseLeaseState.ACTIVE
            ),
            None,
        )
        if use_lease is None:
            raise DecisionError(
                DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
                f"Resource '{resource_id}' has no current mutation lease.",
            )


def _reservation(snapshot: LedgerSnapshot, reservation_id: ReservationId) -> ResourceReservation:
    reservation = next(
        (value for value in snapshot.resource_reservations if value.reservation_id == reservation_id),
        None,
    )
    if reservation is None:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            f"Reservation '{reservation_id}' does not exist.",
        )
    return reservation


def assign_resource(
    snapshot: LedgerSnapshot,
    *,
    reservation_id: ReservationId,
    resource_id: ResourceId,
    instance_id: ResourceInstanceId,
    attempt: AttemptId,
    generation: int,
) -> ResourceDecision:
    definitions = {value.resource_id for value in snapshot.resource_definitions}
    instance = next((value for value in snapshot.resource_instances if value.instance_id == instance_id), None)
    if resource_id not in definitions or instance is None or instance.resource_id != resource_id:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_REQUIRED,
            "Assignment requires a matching definition and instance.",
        )
    if generation < 1 or not reservation_id:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Reservation identity and generation must be current.",
        )
    active = tuple(value for value in snapshot.resource_reservations if value.state == ReservationState.ACTIVE)
    if any(value.instance_id == instance_id for value in active):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_RESERVED,
            f"Instance '{instance_id}' is already reserved.",
        )
    if any(value.attempt == attempt and value.resource_id == resource_id for value in active):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_RESERVED,
            "The attempt already has this resource requirement assigned.",
        )
    reservation = ResourceReservation(
        reservation_id,
        resource_id,
        instance_id,
        attempt,
        generation,
        ReservationState.ACTIVE,
    )
    return ResourceDecision(ResourceDecisionKind.ASSIGN, (ReservationChange(None, reservation),))


def release_resource(snapshot: LedgerSnapshot, reservation_id: ReservationId) -> ResourceDecision:
    reservation = _reservation(snapshot, reservation_id)
    if reservation.state != ReservationState.ACTIVE:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Only an active reservation can be released.",
        )
    released = replace(reservation, state=ReservationState.RELEASED)
    return ResourceDecision(ResourceDecisionKind.RELEASE, (ReservationChange(reservation, released),))


def revoke_resource(
    snapshot: LedgerSnapshot,
    reservation_id: ReservationId,
    *,
    unresolved_intent: bool,
) -> ResourceDecision:
    reservation = _reservation(snapshot, reservation_id)
    if reservation.state != ReservationState.ACTIVE:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Only an active reservation can be revoked.",
        )
    state = ReservationState.REVOKED_PENDING_RECOVERY if unresolved_intent else ReservationState.REVOKED
    revoked = replace(reservation, generation=reservation.generation + 1, state=state)
    return ResourceDecision(ResourceDecisionKind.REVOKE, (ReservationChange(reservation, revoked),))


def reallocate_resource(
    snapshot: LedgerSnapshot,
    reservation_id: ReservationId,
    *,
    replacement_id: ReservationId,
    instance_id: ResourceInstanceId,
    generation: int,
) -> ResourceDecision:
    previous = _reservation(snapshot, reservation_id)
    released = release_resource(snapshot, reservation_id).changes[0]
    remaining = replace(
        snapshot,
        resource_reservations=tuple(
            value for value in snapshot.resource_reservations if value.reservation_id != reservation_id
        ),
    )
    assigned = assign_resource(
        remaining,
        reservation_id=replacement_id,
        resource_id=previous.resource_id,
        instance_id=instance_id,
        attempt=previous.attempt,
        generation=generation,
    ).changes[0]
    return ResourceDecision(ResourceDecisionKind.REALLOCATE, (released, assigned))
