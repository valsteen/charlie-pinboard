from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import assert_never

from charlie_pinboard.domain.authority_decisions import (
    AcquireTaskUseAuthority,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    TaskUseAuthorityDecision,
    decide_task_use_authority,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    HostId,
    LeaseId,
    MutationIntentId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    CanonicalJson,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    LedgerSnapshot,
    MutationIntent,
    MutationIntentState,
    MutationReservation,
    MutationUseLease,
    ReservationState,
    ResourceAuthority,
    ResourceDefinition,
    ResourceInstance,
    ResourceIntentCapability,
    ResourceMutationCapability,
    ResourceObservation,
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
class ResourceReservationCounterChange:
    before: ResourceReservationCounter
    after: ResourceReservationCounter


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    kind: ResourceDecisionKind
    changes: tuple[ReservationChange, ...]
    counter_changes: tuple[ResourceReservationCounterChange, ...] = ()
    use_lease_changes: tuple[ResourceUseLeaseChange, ...] = ()


class ResolverEvidenceDecision(Enum):
    ACCEPTED = "accepted"
    RECOVERY_REQUIRED = "recovery-required"
    POST_INTERRUPTION_PROOF_UNSUPPORTED = "post-interruption-proof-unsupported"


class IntentDecisionKind(Enum):
    REGISTER = "register"
    ADVANCE_OBSERVATION = "advance-observation"
    ABANDON = "abandon"
    RECONCILE = "reconcile"
    PRESERVE = "preserve"
    RESOLVE_FENCED = "resolve-fenced"


class AbandonmentForm(Enum):
    LIVE_OWNER = "live-owner"
    CLEAN_INTERRUPTION = "clean-interruption"


class FencedIntentDisposition(Enum):
    UNCHANGED = "unchanged"
    RECONCILE = "reconcile"
    HUMAN_PRESERVE = "human-preserve"


@dataclass(frozen=True, slots=True)
class ObservedResource:
    instance_id: ResourceInstanceId
    host_id: HostId
    resource_kind: str
    discovery_fingerprint: str
    locator_schema: str
    locator: CanonicalJson
    digest: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MutationIntentChange:
    before: MutationIntent | None
    after: MutationIntent


@dataclass(frozen=True, slots=True)
class MutationObservationChange:
    before: ResourceObservation
    after: ResourceObservation


@dataclass(frozen=True, slots=True)
class MutationInstanceRevisionChange:
    instance_id: ResourceInstanceId
    before: int
    after: int


@dataclass(frozen=True, slots=True)
class MutationUseLeaseChange:
    before: MutationUseLease | None
    after: MutationUseLease


@dataclass(frozen=True, slots=True)
class MutationReservationChange:
    before: MutationReservation
    after: MutationReservation


@dataclass(frozen=True, slots=True)
class ResourceIntentDecision:
    kind: IntentDecisionKind
    intent_change: MutationIntentChange
    observation_change: MutationObservationChange | None = None
    instance_revision_change: MutationInstanceRevisionChange | None = None
    use_lease_changes: tuple[MutationUseLeaseChange, ...] = ()
    reservation_change: MutationReservationChange | None = None


@dataclass(frozen=True, slots=True)
class RegisterMutationIntentInput:
    capability: ResourceMutationCapability
    intent_id: MutationIntentId
    policy_schema: str
    policy: CanonicalJson
    policy_digest: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class AdvanceResourceObservationInput:
    intent: ResourceIntentCapability
    observation: ObservedResource
    evidence_schema: str
    evidence: CanonicalJson
    evidence_digest: str
    resolver_decision: ResolverEvidenceDecision
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class AbandonMutationIntentInput:
    intent: ResourceIntentCapability
    attempt_authority: CommandAttemptAuthority
    observation: ObservedResource
    form: AbandonmentForm
    reason: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileInterruptedObservationInput:
    intent: ResourceIntentCapability
    attempt_authority: CommandAttemptAuthority
    observation: ObservedResource
    evidence_schema: str
    evidence: CanonicalJson
    evidence_digest: str
    resolver_decision: ResolverEvidenceDecision
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class PreserveResourceStateInput:
    intent: ResourceIntentCapability
    coordination_authority: CoordinationCommandAuthority
    attempt_authority: CommandAttemptAuthority
    observation: ObservedResource
    fence_lease_id: LeaseId
    reason: str
    evidence_schema: str | None
    evidence: CanonicalJson | None
    evidence_digest: str | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class ResolveFencedIntentInput:
    intent: ResourceIntentCapability
    coordination_authority: CoordinationCommandAuthority
    observation: ObservedResource
    reservation_counter_generation: int
    disposition: FencedIntentDisposition
    reason: str
    evidence_schema: str | None
    evidence: CanonicalJson | None
    evidence_digest: str | None
    resolver_decision: ResolverEvidenceDecision | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class AssignReservationOperation:
    authority: CommandAttemptAuthority
    reservation_id: ReservationId
    resource_id: ResourceId
    instance_id: ResourceInstanceId
    attempt: AttemptId
    generation: int
    observation: ResourceObservation
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseReservationOperation:
    authority: CommandAttemptAuthority | CoordinationCommandAuthority
    reservation_id: ReservationId
    reservation_generation: int
    observation: ResourceObservation
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ReallocateReservationOperation:
    authority: CommandAttemptAuthority
    reservation_id: ReservationId
    reservation_generation: int
    replacement_id: ReservationId
    instance_id: ResourceInstanceId
    generation: int
    observation: ResourceObservation
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeReservationOperation:
    authority: CoordinationCommandAuthority
    reservation_id: ReservationId
    reservation_generation: int
    counter_generation: int
    observation: ResourceObservation
    changed_at: datetime


type ReservationOperation = (
    AssignReservationOperation
    | ReleaseReservationOperation
    | ReallocateReservationOperation
    | RevokeReservationOperation
)


@dataclass(frozen=True, slots=True)
class ClaimResourceOperation:
    definition: ResourceDefinition
    selected_instance: ResourceInstance
    observation: ResourceObservation
    attempt_authority: CommandAttemptAuthority
    requested_use_lease: MutationUseLease
    acquired_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimResourceDecision:
    reservation: ResourceDecision | None
    task_use: TaskUseAuthorityDecision


def _use_lease_generation(value: ResourceUseLease) -> int:
    return value.generation


def _mutation_use_lease_generation(value: MutationUseLease) -> int:
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


def validate_mutation_resources(
    snapshot: LedgerSnapshot,
    attempt: AttemptId,
    required_resources: tuple[ResourceId, ...],
    tokens: tuple[ResourceToken, ...],
) -> DecisionFailure | None:
    authorities = tuple(value for value in snapshot.attempt_authorities if value.attempt == attempt)
    if len(authorities) != 1 or authorities[0].lease_id is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Mutation requires one current attempt authority."
        )
    authority = authorities[0]
    if len(required_resources) != len(set(required_resources)):
        return DecisionFailure(DecisionFailureCode.RESOURCE_REQUIREMENT_INVALID, "Required resources must be unique.")
    token_by_resource = {token.resource_id: token for token in tokens}
    if set(token_by_resource) != set(required_resources) or len(token_by_resource) != len(tokens):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
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
            return DecisionFailure(
                DecisionFailureCode.RESOURCE_RESERVATION_STALE,
                f"Resource '{resource_id}' is not reserved by this attempt.",
            )
        instance = instances.get(reservation.instance_id)
        token = token_by_resource[resource_id]
        if instance is None or instance.host_id != token.host_id:
            return DecisionFailure(
                DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
                f"Resource '{resource_id}' has no matching host-local instance.",
            )
        if (
            ResourceAuthority(token.resource_id, token.host_id, token.lease_id, token.generation)
            not in authority.resources
        ):
            return DecisionFailure(
                DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
                f"Resource '{resource_id}' is not held by this attempt authority.",
            )
        use_lease = current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)
        if (
            use_lease is None
            or use_lease.lease_id != token.lease_id
            or use_lease.generation != token.generation
            or use_lease.attempt_lease_id != authority.lease_id
            or use_lease.attempt_generation != authority.generation
        ):
            return DecisionFailure(
                DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
                f"Resource '{resource_id}' has no current mutation lease.",
            )
    return None


def _advance_counter(counter: ResourceReservationCounter) -> ResourceReservationCounterChange:
    return ResourceReservationCounterChange(
        counter,
        replace(counter, generation_high_water=counter.generation_high_water + 1),
    )


def assign_resource(
    snapshot: LedgerSnapshot,
    *,
    reservation_id: ReservationId,
    resource_id: ResourceId,
    instance_id: ResourceInstanceId,
    attempt: AttemptId,
    generation: int,
) -> ResourceDecision | DecisionFailure:
    definitions = {value.resource_id for value in snapshot.resource_definitions}
    instance = next((value for value in snapshot.resource_instances if value.instance_id == instance_id), None)
    if resource_id not in definitions or instance is None or instance.resource_id != resource_id:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
            "Assignment requires a matching definition and instance.",
        )
    if generation < 1 or not reservation_id:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Reservation identity and generation must be current.",
        )
    exclusive = tuple(
        value
        for value in snapshot.resource_reservations
        if value.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
    )
    if any(value.instance_id == instance_id for value in exclusive):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_RESERVED,
            f"Instance '{instance_id}' is already reserved.",
        )
    if any(value.attempt == attempt and value.resource_id == resource_id for value in exclusive):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_RESERVED,
            "The attempt already has this resource requirement assigned.",
        )
    item = snapshot.item_for_attempt(attempt)
    scope = None if item is None else next((value for value in snapshot.scopes if value.item == item.item), None)
    if scope is None or all(
        requirement.resource_id != resource_id for requirement in scope.scope.resource_requirements
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_REQUIREMENT_INVALID,
            "Assignment requires an unmet resource requirement owned by the attempt item.",
        )
    counter = snapshot.resource_reservation_counter(instance_id)
    if counter is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            f"Instance '{instance_id}' requires one reservation counter.",
        )
    counter_change = _advance_counter(counter)
    if generation != counter_change.after.generation_high_water:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Assignment generation must advance the instance reservation counter exactly once.",
        )
    reservation = ResourceReservation(
        reservation_id,
        resource_id,
        instance_id,
        attempt,
        generation,
        ReservationState.ACTIVE,
    )
    return ResourceDecision(
        ResourceDecisionKind.ASSIGN,
        (ReservationChange(None, reservation),),
        (counter_change,),
    )


def release_resource(
    snapshot: LedgerSnapshot,
    reservation_id: ReservationId,
) -> ResourceDecision | DecisionFailure:
    reservation = snapshot.resource_reservation(reservation_id)
    if reservation is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            f"Reservation '{reservation_id}' does not exist.",
        )
    if reservation.state != ReservationState.ACTIVE:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Only an active reservation can be released.",
        )
    released = replace(reservation, state=ReservationState.RELEASED)
    use = current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)
    use_changes = () if use is None else (ResourceUseLeaseChange(use, replace(use, state=UseLeaseState.RELEASED)),)
    return ResourceDecision(ResourceDecisionKind.RELEASE, (ReservationChange(reservation, released),), (), use_changes)


def revoke_resource(
    snapshot: LedgerSnapshot,
    reservation_id: ReservationId,
    *,
    unresolved_intent: bool,
) -> ResourceDecision | DecisionFailure:
    reservation = snapshot.resource_reservation(reservation_id)
    if reservation is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            f"Reservation '{reservation_id}' does not exist.",
        )
    if reservation.state != ReservationState.ACTIVE:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Only an active reservation can be revoked.",
        )
    state = ReservationState.REVOKED_PENDING_RECOVERY if unresolved_intent else ReservationState.REVOKED
    revoked = replace(reservation, state=state)
    counter = snapshot.resource_reservation_counter(reservation.instance_id)
    if counter is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            f"Instance '{reservation.instance_id}' requires one reservation counter.",
        )
    counter_change = _advance_counter(counter)
    use = current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)
    use_changes = () if use is None else (ResourceUseLeaseChange(use, replace(use, state=UseLeaseState.REVOKED)),)
    return ResourceDecision(
        ResourceDecisionKind.REVOKE,
        (ReservationChange(reservation, revoked),),
        (counter_change,),
        use_changes,
    )


def reallocate_resource(
    snapshot: LedgerSnapshot,
    reservation_id: ReservationId,
    *,
    replacement_id: ReservationId,
    instance_id: ResourceInstanceId,
    generation: int,
) -> ResourceDecision | DecisionFailure:
    previous = snapshot.resource_reservation(reservation_id)
    if previous is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            f"Reservation '{reservation_id}' does not exist.",
        )
    result = release_resource(snapshot, reservation_id)
    match result:
        case DecisionFailure():
            return result
        case ResourceDecision(changes=changes, use_lease_changes=use_changes):
            released = changes[0]
    remaining = replace(
        snapshot,
        resource_reservations=tuple(
            value for value in snapshot.resource_reservations if value.reservation_id != reservation_id
        ),
    )
    result = assign_resource(
        remaining,
        reservation_id=replacement_id,
        resource_id=previous.resource_id,
        instance_id=instance_id,
        attempt=previous.attempt,
        generation=generation,
    )
    match result:
        case DecisionFailure():
            return result
        case assigned_decision:
            pass
    assigned = assigned_decision.changes[0]
    return ResourceDecision(
        ResourceDecisionKind.REALLOCATE,
        (released, assigned),
        assigned_decision.counter_changes,
        use_changes,
    )


def decide_reservation_operation(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    operation: ReservationOperation,
) -> ResourceDecision | DecisionFailure:
    if snapshot.resource_observation(operation.observation.instance_id) != operation.observation:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
            "Reservation changes require the exact selected locator observation.",
        )
    match operation:
        case AssignReservationOperation(authority=authority):
            if (
                authority not in snapshot.command_attempt_authorities
                or authority.expires_at <= operation.changed_at
                or authority.attempt != operation.attempt
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Reservation assignment requires exact live attempt authority.",
                )
            if operation.observation.instance_id != operation.instance_id:
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
                    "Reservation assignment locator facts are cross-wired.",
                )
            return assign_resource(
                snapshot,
                reservation_id=operation.reservation_id,
                resource_id=operation.resource_id,
                instance_id=operation.instance_id,
                attempt=operation.attempt,
                generation=operation.generation,
            )
        case ReleaseReservationOperation(authority=authority):
            reservation = snapshot.resource_reservation(operation.reservation_id)
            if (
                reservation is None
                or reservation.generation != operation.reservation_generation
                or operation.observation.instance_id != reservation.instance_id
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_RESERVATION_STALE,
                    "Reservation release facts are stale or cross-wired.",
                )
            if isinstance(authority, CommandAttemptAuthority):
                authorized = (
                    authority in snapshot.command_attempt_authorities
                    and authority.expires_at > operation.changed_at
                    and reservation is not None
                    and authority.attempt == reservation.attempt
                )
            else:
                authorized = (
                    snapshot.coordination_authority == authority and authority.expires_at > operation.changed_at
                )
            if not authorized:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Reservation release requires exact live attempt or coordination authority.",
                )
            if any(
                value.reservation_id == operation.reservation_id and value.state == MutationIntentState.PLANNED
                for value in snapshot.mutation_intents
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED,
                    "Reservation release requires every mutation intent to be resolved.",
                )
            return release_resource(snapshot, operation.reservation_id)
        case ReallocateReservationOperation(authority=authority):
            reservation = snapshot.resource_reservation(operation.reservation_id)
            if (
                authority not in snapshot.command_attempt_authorities
                or authority.expires_at <= operation.changed_at
                or reservation is None
                or authority.attempt != reservation.attempt
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Reservation reallocation requires exact live attempt authority.",
                )
            if (
                reservation.generation != operation.reservation_generation
                or operation.observation.instance_id != operation.instance_id
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_RESERVATION_STALE,
                    "Reservation reallocation facts are stale or cross-wired.",
                )
            if any(
                value.reservation_id == operation.reservation_id and value.state == MutationIntentState.PLANNED
                for value in snapshot.mutation_intents
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED,
                    "Reservation reallocation requires every mutation intent to be resolved.",
                )
            return reallocate_resource(
                snapshot,
                operation.reservation_id,
                replacement_id=operation.replacement_id,
                instance_id=operation.instance_id,
                generation=operation.generation,
            )
        case RevokeReservationOperation(authority=authority):
            if snapshot.coordination_authority != authority or authority.expires_at <= operation.changed_at:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Reservation revocation requires exact live coordination authority.",
                )
            reservation = snapshot.resource_reservation(operation.reservation_id)
            counter = None if reservation is None else snapshot.resource_reservation_counter(reservation.instance_id)
            if (
                reservation is None
                or reservation.generation != operation.reservation_generation
                or operation.observation.instance_id != reservation.instance_id
                or counter is None
                or counter.generation_high_water != operation.counter_generation
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_RESERVATION_STALE,
                    "Reservation revocation facts are stale or cross-wired.",
                )
            unresolved = any(
                value.reservation_id == operation.reservation_id and value.state == MutationIntentState.PLANNED
                for value in snapshot.mutation_intents
            )
            return revoke_resource(snapshot, operation.reservation_id, unresolved_intent=unresolved)
        case _ as unreachable:
            assert_never(unreachable)


def decide_claim_resource(
    snapshot: LedgerSnapshot,
    operation: ClaimResourceOperation,
) -> ClaimResourceDecision | DecisionFailure:
    authority = operation.attempt_authority
    requested = operation.requested_use_lease
    if (
        operation.definition not in snapshot.resource_definitions
        or operation.selected_instance not in snapshot.resource_instances
        or operation.selected_instance.resource_id != operation.definition.resource_id
        or snapshot.resource_observation(operation.selected_instance.instance_id) != operation.observation
        or authority not in snapshot.command_attempt_authorities
        or authority.expires_at <= operation.acquired_at
        or requested.instance_id != operation.selected_instance.instance_id
        or requested.instance_subject_revision != operation.selected_instance.subject_revision
        or requested.observation_generation != operation.observation.generation
        or requested.observation_digest != operation.observation.digest
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Resource claim selection or authority is stale or cross-wired.",
        )
    reservation = next(
        (
            value
            for value in snapshot.mutation_reservations
            if value.resource_id == operation.definition.resource_id
            and value.attempt_id == authority.attempt
            and value.state == ReservationState.ACTIVE
        ),
        None,
    )
    reservation_decision: ResourceDecision | None = None
    if reservation is None:
        result = assign_resource(
            snapshot,
            reservation_id=requested.reservation_id,
            resource_id=operation.definition.resource_id,
            instance_id=operation.selected_instance.instance_id,
            attempt=authority.attempt,
            generation=requested.reservation_generation,
        )
        if isinstance(result, DecisionFailure):
            return result
        reservation_decision = result
        item_revision = snapshot.subject_revision(authority.item)
        if item_revision is None:
            return DecisionFailure(DecisionFailureCode.WORK_STATE_INVALID, "Claimed item has no subject revision.")
        reservation = MutationReservation(
            requested.reservation_id,
            requested.instance_id,
            operation.definition.resource_id,
            requested.host_id,
            requested.reservation_generation,
            authority.attempt,
            authority.item,
            ReservationState.ACTIVE,
            int(item_revision),
        )
    retained_uses = tuple(
        value for value in snapshot.mutation_use_leases if value.reservation_id == reservation.reservation_id
    )
    retained_use = max(retained_uses, key=_mutation_use_lease_generation) if retained_uses else None
    retained_attempt = AttemptLeaseAuthority(
        authority.host_epoch,
        authority.attempt,
        authority.item,
        authority.task_id,
        authority.host_id,
        authority.lease_id,
        authority.generation,
        operation.acquired_at,
        authority.expires_at,
        AttemptLeaseStatus.ACTIVE,
    )
    result = decide_task_use_authority(
        retained_use,
        AcquireTaskUseAuthority(requested, authority, operation.acquired_at),
        reservation,
        retained_attempt,
        snapshot.coordination_lease,
    )
    if isinstance(result, DecisionFailure):
        return result
    return ClaimResourceDecision(reservation_decision, result)


def _current_attempt_authority(
    snapshot: LedgerSnapshot,
    supplied: CommandAttemptAuthority,
    now: datetime,
) -> CommandAttemptAuthority | DecisionFailure:
    if supplied.host_epoch != snapshot.host_epoch or supplied.expires_at <= now:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Mutation requires current host and attempt authority.",
        )
    if supplied not in snapshot.command_attempt_authorities:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Mutation requires the exact current attempt authority.",
        )
    return supplied


def _current_coordination_authority(
    snapshot: LedgerSnapshot,
    supplied: CoordinationCommandAuthority,
    now: datetime,
) -> CoordinationCommandAuthority | DecisionFailure:
    if (
        supplied.host_epoch != snapshot.host_epoch
        or supplied.expires_at <= now
        or supplied != snapshot.coordination_authority
    ):
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Recovery requires the exact current coordination authority.",
        )
    return supplied


def _mutation_records(
    snapshot: LedgerSnapshot,
    capability: ResourceMutationCapability,
    now: datetime,
) -> tuple[CommandAttemptAuthority, MutationReservation, MutationUseLease, ResourceObservation] | DecisionFailure:
    use_lease = snapshot.mutation_use_lease(capability.task_use_lease_id, capability.task_use_generation)
    if use_lease is None:
        return DecisionFailure(DecisionFailureCode.RESOURCE_USE_LEASE_STALE, "The task-use lease is not retained.")
    authority = next(
        (
            value
            for value in snapshot.command_attempt_authorities
            if value.attempt == use_lease.attempt_id
            and value.lease_id == capability.attempt_lease_id
            and value.generation == capability.attempt_lease_generation
        ),
        None,
    )
    if authority is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "The task-use lease has no exact current attempt authority.",
        )
    result = _current_attempt_authority(snapshot, authority, now)
    match result:
        case DecisionFailure():
            return result
        case CommandAttemptAuthority() as authority:
            pass
    reservation = snapshot.mutation_reservation(capability.reservation_id)
    if reservation is None or reservation.state != ReservationState.ACTIVE:
        return DecisionFailure(DecisionFailureCode.RESOURCE_RESERVATION_STALE, "The reservation is not active.")
    instance = next(
        (value for value in snapshot.resource_instances if value.instance_id == capability.instance_id),
        None,
    )
    observation = next(
        (value for value in snapshot.resource_observations if value.instance_id == capability.instance_id),
        None,
    )
    latest_generation = max(
        (
            value.generation
            for value in snapshot.mutation_use_leases
            if value.reservation_id == capability.reservation_id
        ),
        default=0,
    )
    if (
        instance is None
        or observation is None
        or use_lease.state != UseLeaseState.ACTIVE
        or use_lease.generation_kind != UseLeaseGenerationKind.GRANT
        or use_lease.expires_at <= now
        or use_lease.generation != latest_generation
    ):
        return DecisionFailure(DecisionFailureCode.RESOURCE_USE_LEASE_STALE, "The mutation grant is not current.")
    actual = ResourceMutationCapability(
        reservation.resource_id,
        reservation.reservation_id,
        reservation.acquisition_generation,
        reservation.instance_id,
        instance.subject_revision,
        observation.generation,
        observation.digest,
        use_lease.lease_id,
        use_lease.generation,
        use_lease.task_id,
        use_lease.host_id,
        use_lease.host_epoch,
        use_lease.attempt_lease_id,
        use_lease.attempt_lease_generation,
    )
    if actual != capability:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "The mutation capability does not match the current reservation, instance, observation, and leases.",
        )
    return authority, reservation, use_lease, observation


def _planned_intent(
    snapshot: LedgerSnapshot,
    capability: ResourceIntentCapability,
) -> MutationIntent | DecisionFailure:
    intent = snapshot.mutation_intent(capability.intent_id)
    if (
        intent is None
        or intent.policy_digest != capability.policy_digest
        or intent.state != capability.state
        or intent.state != MutationIntentState.PLANNED
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "The exact planned mutation intent is not current.",
        )
    resource = capability.resource
    if (
        intent.reservation_id != resource.reservation_id
        or intent.reservation_generation != resource.reservation_generation
        or intent.instance_id != resource.instance_id
        or intent.resource_use_lease_id != resource.task_use_lease_id
        or intent.resource_use_generation != resource.task_use_generation
        or intent.attempt_lease_id != resource.attempt_lease_id
        or intent.attempt_lease_generation != resource.attempt_lease_generation
        or intent.task_id != resource.task_id
        or intent.host_id != resource.host_id
        or intent.start_instance_subject_revision != resource.instance_subject_revision
        or intent.start_observation_generation != resource.locator_observation_generation
        or intent.start_observation_digest != resource.locator_observation_digest
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "The mutation intent is cross-wired to different authority.",
        )
    return intent


def _validate_observation_identity(
    snapshot: LedgerSnapshot,
    observation: ObservedResource,
) -> ResourceObservation | DecisionFailure:
    instance = snapshot.resource_instance(observation.instance_id)
    accepted = snapshot.resource_observation(observation.instance_id)
    definition = snapshot.resource_definition(instance.resource_id) if instance is not None else None
    if (
        instance is None
        or accepted is None
        or definition is None
        or instance.host_id != observation.host_id
        or definition.kind != observation.resource_kind
        or instance.discovery_fingerprint != observation.discovery_fingerprint
        or accepted.locator_schema != observation.locator_schema
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
            "The resolver observation does not identify the accepted resource instance.",
        )
    return accepted


def _observation_is_unchanged(accepted: ResourceObservation, observed: ObservedResource) -> bool:
    return (
        accepted.host_id,
        accepted.locator_schema,
        accepted.locator,
        accepted.digest,
    ) == (
        observed.host_id,
        observed.locator_schema,
        observed.locator,
        observed.digest,
    )


def _require_evidence(schema: str, evidence: CanonicalJson, digest: str) -> DecisionFailure | None:
    if not schema.strip() or not evidence or not digest.strip():
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Canonical resolver evidence identity must be complete.",
        )
    return None


def _advance_intent(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
    observed: ObservedResource,
    *,
    state: MutationIntentState,
    kind: IntentDecisionKind,
    resolved_at: datetime,
    evidence_schema: str | None,
    evidence: CanonicalJson | None,
    evidence_digest: str | None,
    update_use_lease: MutationUseLease | None,
    disposition_task_id: TaskId | None = None,
    disposition_reason: str | None = None,
) -> ResourceIntentDecision | DecisionFailure:
    result = _validate_observation_identity(snapshot, observed)
    match result:
        case DecisionFailure():
            return result
        case accepted:
            pass
    instance = snapshot.resource_instance(observed.instance_id)
    if instance is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_INSTANCE_REQUIRED,
            "The resolver observation does not identify the accepted resource instance.",
        )
    next_observation = ResourceObservation(
        accepted.instance_id,
        accepted.host_id,
        accepted.locator_schema,
        observed.locator,
        accepted.generation + 1,
        observed.digest,
        observed.observed_at,
    )
    resolved = replace(
        intent,
        state=state,
        resolved_at=resolved_at,
        result_observation_generation=next_observation.generation,
        result_observation_digest=next_observation.digest,
        evidence_schema=evidence_schema,
        evidence=evidence,
        evidence_digest=evidence_digest,
        disposition_task_id=disposition_task_id,
        disposition_reason=disposition_reason,
    )
    use_changes: tuple[MutationUseLeaseChange, ...] = ()
    if update_use_lease is not None:
        use_changes = (
            MutationUseLeaseChange(
                update_use_lease,
                replace(
                    update_use_lease,
                    instance_subject_revision=instance.subject_revision + 1,
                    observation_generation=next_observation.generation,
                    observation_digest=next_observation.digest,
                ),
            ),
        )
    return ResourceIntentDecision(
        kind,
        MutationIntentChange(intent, resolved),
        MutationObservationChange(accepted, next_observation),
        MutationInstanceRevisionChange(instance.instance_id, instance.subject_revision, instance.subject_revision + 1),
        use_changes,
    )


def register_mutation_intent(
    snapshot: LedgerSnapshot,
    value: RegisterMutationIntentInput,
) -> ResourceIntentDecision | DecisionFailure:
    result = _mutation_records(
        snapshot,
        value.capability,
        value.recorded_at,
    )
    match result:
        case DecisionFailure():
            return result
        case (authority, reservation, _use_lease, observation):
            pass
    if not value.intent_id or not value.policy_schema.strip() or not value.policy or not value.policy_digest.strip():
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Mutation intent identity and canonical policy must be complete.",
        )
    if any(candidate.intent_id == value.intent_id for candidate in snapshot.mutation_intents) or any(
        candidate.reservation_id == reservation.reservation_id and candidate.state == MutationIntentState.PLANNED
        for candidate in snapshot.mutation_intents
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "The reservation already has this or another planned mutation intent.",
        )
    intent = MutationIntent(
        value.intent_id,
        reservation.reservation_id,
        reservation.acquisition_generation,
        reservation.instance_id,
        authority.attempt,
        authority.host_id,
        value.capability.task_use_generation,
        value.capability.task_use_lease_id,
        authority.task_id,
        authority.lease_id,
        authority.generation,
        value.capability.instance_subject_revision,
        observation.generation,
        observation.digest,
        value.policy_schema,
        value.policy,
        value.policy_digest,
        MutationIntentState.PLANNED,
        value.recorded_at,
    )
    return ResourceIntentDecision(IntentDecisionKind.REGISTER, MutationIntentChange(None, intent))


def advance_resource_observation(
    snapshot: LedgerSnapshot,
    value: AdvanceResourceObservationInput,
) -> ResourceIntentDecision | DecisionFailure:
    if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Only accepted deterministic evidence can advance a live observation.",
        )
    if (failure := _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)) is not None:
        return failure
    result = _mutation_records(
        snapshot,
        value.intent.resource,
        value.resolved_at,
    )
    match result:
        case DecisionFailure():
            return result
        case (_authority, _reservation, use_lease, _observation):
            pass
    result = _planned_intent(snapshot, value.intent)
    match result:
        case DecisionFailure():
            return result
        case intent:
            pass
    return _advance_intent(
        snapshot,
        intent,
        value.observation,
        state=MutationIntentState.ACCEPTED,
        kind=IntentDecisionKind.ADVANCE_OBSERVATION,
        resolved_at=value.resolved_at,
        evidence_schema=value.evidence_schema,
        evidence=value.evidence,
        evidence_digest=value.evidence_digest,
        update_use_lease=use_lease,
    )


def _interrupted_use_lease(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
) -> MutationUseLease | DecisionFailure:
    use_lease = snapshot.mutation_use_lease(intent.resource_use_lease_id, intent.resource_use_generation)
    if use_lease is None:
        return DecisionFailure(DecisionFailureCode.RESOURCE_USE_LEASE_STALE, "The interrupted grant is not retained.")
    if any(
        candidate.reservation_id == intent.reservation_id and candidate.generation > use_lease.generation
        for candidate in snapshot.mutation_use_leases
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "A later task-use generation already exists.",
        )
    if any(
        candidate.intent_id != intent.intent_id
        and candidate.reservation_id == intent.reservation_id
        and (
            candidate.resource_use_generation > intent.resource_use_generation
            or candidate.recorded_at > intent.recorded_at
        )
        for candidate in snapshot.mutation_intents
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "A later mutation intent already exists.",
        )
    return use_lease


def _require_intent_start_observation(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
) -> DecisionFailure | None:
    instance = next(
        (value for value in snapshot.resource_instances if value.instance_id == intent.instance_id),
        None,
    )
    observation = next(
        (value for value in snapshot.resource_observations if value.instance_id == intent.instance_id),
        None,
    )
    if (
        instance is None
        or observation is None
        or instance.subject_revision != intent.start_instance_subject_revision
        or observation.generation != intent.start_observation_generation
        or observation.digest != intent.start_observation_digest
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Interruption recovery requires the intent's exact starting observation.",
        )
    return None


def _require_active_intent_reservation(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
) -> DecisionFailure | None:
    reservation = snapshot.mutation_reservation(intent.reservation_id)
    if reservation is None or reservation.state != ReservationState.ACTIVE:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Resource recovery requires the intent's exact active reservation.",
        )
    return None


def _validate_recovery_attempt(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
    supplied: CommandAttemptAuthority,
    now: datetime,
) -> CommandAttemptAuthority | DecisionFailure:
    result = _current_attempt_authority(snapshot, supplied, now)
    match result:
        case DecisionFailure():
            return result
        case CommandAttemptAuthority() as authority:
            pass
    reservation = snapshot.mutation_reservation(intent.reservation_id)
    if (
        reservation is None
        or reservation.attempt_id != authority.attempt
        or reservation.item_id != authority.item
        or reservation.acquisition_generation != intent.reservation_generation
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Recovery authority is cross-wired to another reservation or attempt.",
        )
    if (
        authority.lease_id == intent.attempt_lease_id
        and authority.generation == intent.attempt_lease_generation
        and authority.task_id == intent.task_id
    ):
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Interruption recovery requires fresh attempt authority.",
        )
    return authority


def abandon_mutation_intent(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    value: AbandonMutationIntentInput,
) -> ResourceIntentDecision | DecisionFailure:
    result = _planned_intent(snapshot, value.intent)
    match result:
        case DecisionFailure():
            return result
        case intent:
            pass
    if not value.reason.strip():
        return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, "Abandonment requires a reason.")
    result = _validate_observation_identity(snapshot, value.observation)
    match result:
        case DecisionFailure():
            return result
        case accepted:
            pass
    if not _observation_is_unchanged(accepted, value.observation):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "A changed observation cannot be abandoned as unused.",
        )
    match value.form:
        case AbandonmentForm.LIVE_OWNER:
            result = _mutation_records(
                snapshot,
                value.intent.resource,
                value.decided_at,
            )
            match result:
                case DecisionFailure():
                    return result
                case (authority, _reservation, _use, _observation):
                    pass
            if authority != value.attempt_authority:
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Live abandonment requires the exact intent owner.",
                )
        case AbandonmentForm.CLEAN_INTERRUPTION:
            result = _interrupted_use_lease(snapshot, intent)
            match result:
                case DecisionFailure():
                    return result
                case old_use:
                    pass
            if old_use.state not in {UseLeaseState.RELEASED, UseLeaseState.EXPIRED}:
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
                    "Clean interruption requires released or expired prior use authority.",
                )
            if (failure := _require_intent_start_observation(snapshot, intent)) is not None:
                return failure
            if (failure := _require_active_intent_reservation(snapshot, intent)) is not None:
                return failure
            result = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.decided_at)
            match result:
                case DecisionFailure():
                    return result
                case CommandAttemptAuthority():
                    pass
        case _ as unreachable:
            assert_never(unreachable)
    abandoned = replace(
        intent,
        state=MutationIntentState.ABANDONED,
        resolved_at=value.decided_at,
        disposition_task_id=value.attempt_authority.task_id,
        disposition_reason=value.reason,
    )
    return ResourceIntentDecision(IntentDecisionKind.ABANDON, MutationIntentChange(intent, abandoned))


def reconcile_interrupted_observation(
    snapshot: LedgerSnapshot,
    value: ReconcileInterruptedObservationInput,
) -> ResourceIntentDecision | DecisionFailure:
    if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Post-interruption reconciliation requires supported deterministic proof.",
        )
    if (failure := _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)) is not None:
        return failure
    result = _planned_intent(snapshot, value.intent)
    match result:
        case DecisionFailure():
            return result
        case intent:
            pass
    result = _interrupted_use_lease(snapshot, intent)
    match result:
        case DecisionFailure():
            return result
        case old_use:
            pass
    if old_use.state not in {UseLeaseState.RELEASED, UseLeaseState.EXPIRED}:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Reconciliation requires interrupted prior use authority.",
        )
    if (failure := _require_intent_start_observation(snapshot, intent)) is not None:
        return failure
    if (failure := _require_active_intent_reservation(snapshot, intent)) is not None:
        return failure
    result = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.resolved_at)
    match result:
        case DecisionFailure():
            return result
        case CommandAttemptAuthority():
            pass
    return _advance_intent(
        snapshot,
        intent,
        value.observation,
        state=MutationIntentState.RECONCILED,
        kind=IntentDecisionKind.RECONCILE,
        resolved_at=value.resolved_at,
        evidence_schema=value.evidence_schema,
        evidence=value.evidence,
        evidence_digest=value.evidence_digest,
        update_use_lease=None,
    )


def preserve_resource_state(  # noqa: PLR0912
    snapshot: LedgerSnapshot,
    value: PreserveResourceStateInput,
) -> ResourceIntentDecision | DecisionFailure:
    result = _current_coordination_authority(snapshot, value.coordination_authority, value.resolved_at)
    match result:
        case DecisionFailure():
            return result
        case CoordinationCommandAuthority():
            pass
    result = _planned_intent(snapshot, value.intent)
    match result:
        case DecisionFailure():
            return result
        case intent:
            pass
    result = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.resolved_at)
    match result:
        case DecisionFailure():
            return result
        case authority:
            pass
    if (failure := _require_active_intent_reservation(snapshot, intent)) is not None:
        return failure
    if not value.reason.strip() or not value.fence_lease_id:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Human preserve requires an explicit reason and fence identity.",
        )
    if (value.evidence_schema, value.evidence, value.evidence_digest).count(None) not in {0, 3}:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Human preserve evidence must be wholly present or absent.",
        )
    result = _interrupted_use_lease(snapshot, intent)
    match result:
        case DecisionFailure():
            return result
        case old_use:
            pass
    fence = replace(
        old_use,
        lease_id=value.fence_lease_id,
        generation=old_use.generation + 1,
        generation_kind=UseLeaseGenerationKind.FENCE,
        state=UseLeaseState.REVOKED,
    )
    if old_use.state == UseLeaseState.ACTIVE:
        revoked = replace(old_use, state=UseLeaseState.REVOKED)
        use_changes = (MutationUseLeaseChange(old_use, revoked), MutationUseLeaseChange(None, fence))
    else:
        use_changes = (MutationUseLeaseChange(None, fence),)
    result = _advance_intent(
        snapshot,
        intent,
        value.observation,
        state=MutationIntentState.HUMAN_PRESERVED,
        kind=IntentDecisionKind.PRESERVE,
        resolved_at=value.resolved_at,
        evidence_schema=value.evidence_schema,
        evidence=value.evidence,
        evidence_digest=value.evidence_digest,
        update_use_lease=None,
        disposition_task_id=authority.task_id,
        disposition_reason=value.reason,
    )
    match result:
        case DecisionFailure():
            return result
        case decision:
            pass
    return replace(decision, use_lease_changes=use_changes)


def resolve_fenced_resource_intent(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    value: ResolveFencedIntentInput,
) -> ResourceIntentDecision | DecisionFailure:
    result = _current_coordination_authority(snapshot, value.coordination_authority, value.resolved_at)
    match result:
        case DecisionFailure():
            return result
        case CoordinationCommandAuthority():
            pass
    result = _planned_intent(snapshot, value.intent)
    match result:
        case DecisionFailure():
            return result
        case intent:
            pass
    reservation = snapshot.mutation_reservation(intent.reservation_id)
    if reservation is None:
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_RESERVATION_STALE,
            "Resource recovery requires the intent's exact reservation.",
        )
    old_use = snapshot.mutation_use_lease(intent.resource_use_lease_id, intent.resource_use_generation)
    fence = (
        next(
            (
                candidate
                for candidate in snapshot.mutation_use_leases
                if candidate.reservation_id == intent.reservation_id
                and candidate.generation == intent.resource_use_generation + 1
                and candidate.generation_kind == UseLeaseGenerationKind.FENCE
                and candidate.state == UseLeaseState.REVOKED
            ),
            None,
        )
        if old_use is not None
        else None
    )
    later_grant = any(
        candidate.reservation_id == intent.reservation_id and candidate.generation > intent.resource_use_generation + 1
        for candidate in snapshot.mutation_use_leases
    )
    counter = next(
        (
            candidate.generation_high_water
            for candidate in snapshot.resource_reservation_counters
            if candidate.instance_id == reservation.instance_id
        ),
        None,
    )
    task_fenced = old_use is not None and old_use.state == UseLeaseState.REVOKED and fence is not None
    reservation_fenced = (
        reservation.state == ReservationState.REVOKED_PENDING_RECOVERY
        and counter == value.reservation_counter_generation
        and value.reservation_counter_generation > reservation.acquisition_generation
    )
    if later_grant or not (task_fenced or reservation_fenced):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            "Fenced intent resolution requires the exact fence or recovery quarantine with no later grant.",
        )
    result = _validate_observation_identity(snapshot, value.observation)
    match result:
        case DecisionFailure():
            return result
        case accepted:
            pass
    unchanged = _observation_is_unchanged(accepted, value.observation)
    reservation_change = (
        MutationReservationChange(reservation, replace(reservation, state=ReservationState.REVOKED))
        if reservation.state == ReservationState.REVOKED_PENDING_RECOVERY
        else None
    )
    if unchanged:
        if value.disposition != FencedIntentDisposition.UNCHANGED:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "An unchanged fenced intent requires the unchanged disposition.",
            )
        if not value.reason.strip():
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Unchanged fenced intent resolution requires a reason.",
            )
        resolved = replace(
            intent,
            state=MutationIntentState.ABANDONED,
            resolved_at=value.resolved_at,
            disposition_task_id=value.coordination_authority.task_id,
            disposition_reason=value.reason,
        )
        return ResourceIntentDecision(
            IntentDecisionKind.RESOLVE_FENCED,
            MutationIntentChange(intent, resolved),
            reservation_change=reservation_change,
        )
    if value.disposition == FencedIntentDisposition.RECONCILE:
        if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
            return DecisionFailure(
                DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
                "Changed fenced state requires supported deterministic proof or human preserve.",
            )
        state = MutationIntentState.RECONCILED
        if value.evidence_schema is None or value.evidence is None or value.evidence_digest is None:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Mechanical reconciliation requires canonical evidence.",
            )
        if (failure := _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)) is not None:
            return failure
    elif value.disposition == FencedIntentDisposition.HUMAN_PRESERVE:
        if not value.reason.strip():
            return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, "Human preserve requires a reason.")
        state = MutationIntentState.HUMAN_PRESERVED
        if (value.evidence_schema, value.evidence, value.evidence_digest).count(None) not in {0, 3}:
            return DecisionFailure(
                DecisionFailureCode.TRANSITION_INPUT_INVALID,
                "Human preserve evidence must be wholly present or absent.",
            )
    else:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "Changed fenced state cannot use the unchanged disposition.",
        )
    result = _advance_intent(
        snapshot,
        intent,
        value.observation,
        state=state,
        kind=IntentDecisionKind.RESOLVE_FENCED,
        resolved_at=value.resolved_at,
        evidence_schema=value.evidence_schema,
        evidence=value.evidence,
        evidence_digest=value.evidence_digest,
        update_use_lease=None,
        disposition_task_id=value.coordination_authority.task_id,
        disposition_reason=value.reason,
    )
    match result:
        case DecisionFailure():
            return result
        case decision:
            pass
    return replace(decision, reservation_change=reservation_change)
