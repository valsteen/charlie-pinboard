from dataclasses import dataclass

from charlie_pinboard.domain.identifiers import HostId, LeaseId, ReservationId, ResourceId
from charlie_pinboard.domain.model import (
    ResourceReservation,
    ResourceReservationCounter,
    ResourceUseLease,
    UseLeaseGenerationKind,
    UseLeaseState,
)


@dataclass(frozen=True, slots=True)
class ResourceToken:
    resource_id: ResourceId
    host_id: HostId
    lease_id: LeaseId
    generation: int


@dataclass(frozen=True, slots=True)
class ReservationChange:
    before: ResourceReservation | None
    after: ResourceReservation


@dataclass(frozen=True, slots=True)
class ResourceUseLeaseChange:
    before: ResourceUseLease
    after: ResourceUseLease


@dataclass(frozen=True, slots=True)
class ResourceReservationCounterChange:
    before: ResourceReservationCounter
    after: ResourceReservationCounter


def _use_lease_generation(value: ResourceUseLease) -> int:
    return value.generation


def current_authorizing_grant(
    use_leases: tuple[ResourceUseLease, ...],
    reservation_id: ReservationId,
) -> ResourceUseLease | None:
    retained = tuple(value for value in use_leases if value.reservation_id == reservation_id)
    if not retained:
        return None
    latest = max(retained, key=_use_lease_generation)
    if latest.state != UseLeaseState.ACTIVE or latest.generation_kind != UseLeaseGenerationKind.GRANT:
        return None
    return latest
