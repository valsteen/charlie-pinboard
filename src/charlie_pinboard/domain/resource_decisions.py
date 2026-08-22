from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
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
    attempt_authority: CommandAttemptAuthority
    observation: ObservedResource
    reservation_counter_generation: int
    disposition: FencedIntentDisposition
    reason: str
    evidence_schema: str | None
    evidence: CanonicalJson | None
    evidence_digest: str | None
    resolver_decision: ResolverEvidenceDecision | None
    resolved_at: datetime


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
        use_lease = current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)
        if (
            use_lease is None
            or use_lease.lease_id != token.lease_id
            or use_lease.generation != token.generation
            or use_lease.attempt_lease_id != authority.lease_id
            or use_lease.attempt_generation != authority.generation
        ):
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


def _reservation_counter(
    snapshot: LedgerSnapshot,
    instance_id: ResourceInstanceId,
) -> ResourceReservationCounter:
    counters = tuple(value for value in snapshot.resource_reservation_counters if value.instance_id == instance_id)
    if len(counters) != 1:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            f"Instance '{instance_id}' requires one reservation counter.",
        )
    return counters[0]


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
    exclusive = tuple(
        value
        for value in snapshot.resource_reservations
        if value.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
    )
    if any(value.instance_id == instance_id for value in exclusive):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_RESERVED,
            f"Instance '{instance_id}' is already reserved.",
        )
    if any(value.attempt == attempt and value.resource_id == resource_id for value in exclusive):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_RESERVED,
            "The attempt already has this resource requirement assigned.",
        )
    counter_change = _advance_counter(_reservation_counter(snapshot, instance_id))
    if generation != counter_change.after.generation_high_water:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
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
    revoked = replace(reservation, state=state)
    counter_change = _advance_counter(_reservation_counter(snapshot, reservation.instance_id))
    return ResourceDecision(
        ResourceDecisionKind.REVOKE,
        (ReservationChange(reservation, revoked),),
        (counter_change,),
    )


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
    assigned_decision = assign_resource(
        remaining,
        reservation_id=replacement_id,
        resource_id=previous.resource_id,
        instance_id=instance_id,
        attempt=previous.attempt,
        generation=generation,
    )
    assigned = assigned_decision.changes[0]
    return ResourceDecision(
        ResourceDecisionKind.REALLOCATE,
        (released, assigned),
        assigned_decision.counter_changes,
    )


def _current_attempt_authority(
    snapshot: LedgerSnapshot,
    supplied: CommandAttemptAuthority,
    now: datetime,
) -> CommandAttemptAuthority:
    if supplied.host_epoch != snapshot.host_epoch or supplied.expires_at <= now:
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Mutation requires current host and attempt authority.",
        )
    if supplied not in snapshot.command_attempt_authorities:
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Mutation requires the exact current attempt authority.",
        )
    return supplied


def _current_coordination_authority(
    snapshot: LedgerSnapshot,
    supplied: CoordinationCommandAuthority,
    now: datetime,
) -> CoordinationCommandAuthority:
    if (
        supplied.host_epoch != snapshot.host_epoch
        or supplied.expires_at <= now
        or supplied != snapshot.coordination_authority
    ):
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Recovery requires the exact current coordination authority.",
        )
    return supplied


def _mutation_records(
    snapshot: LedgerSnapshot,
    capability: ResourceMutationCapability,
    now: datetime,
) -> tuple[CommandAttemptAuthority, MutationReservation, MutationUseLease, ResourceObservation]:
    use_lease = next(
        (
            value
            for value in snapshot.mutation_use_leases
            if value.lease_id == capability.task_use_lease_id and value.generation == capability.task_use_generation
        ),
        None,
    )
    if use_lease is None:
        raise DecisionError(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, "The task-use lease is not retained.")
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
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "The task-use lease has no exact current attempt authority.",
        )
    _current_attempt_authority(snapshot, authority, now)
    reservation = next(
        (value for value in snapshot.mutation_reservations if value.reservation_id == capability.reservation_id),
        None,
    )
    if reservation is None or reservation.state != ReservationState.ACTIVE:
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, "The reservation is not active.")
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
        raise DecisionError(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, "The mutation grant is not current.")
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
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "The mutation capability does not match the current reservation, instance, observation, and leases.",
        )
    return authority, reservation, use_lease, observation


def _planned_intent(snapshot: LedgerSnapshot, capability: ResourceIntentCapability) -> MutationIntent:
    intent = next((value for value in snapshot.mutation_intents if value.intent_id == capability.intent_id), None)
    if (
        intent is None
        or intent.policy_digest != capability.policy_digest
        or intent.state != capability.state
        or intent.state != MutationIntentState.PLANNED
    ):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
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
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "The mutation intent is cross-wired to different authority.",
        )
    return intent


def _validate_observation_identity(
    snapshot: LedgerSnapshot,
    observation: ObservedResource,
) -> ResourceObservation:
    instance = next(
        (value for value in snapshot.resource_instances if value.instance_id == observation.instance_id),
        None,
    )
    accepted = next(
        (value for value in snapshot.resource_observations if value.instance_id == observation.instance_id),
        None,
    )
    definition = (
        next(
            (value for value in snapshot.resource_definitions if value.resource_id == instance.resource_id),
            None,
        )
        if instance is not None
        else None
    )
    if (
        instance is None
        or accepted is None
        or definition is None
        or instance.host_id != observation.host_id
        or definition.kind != observation.resource_kind
        or instance.discovery_fingerprint != observation.discovery_fingerprint
        or accepted.locator_schema != observation.locator_schema
    ):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_INSTANCE_REQUIRED,
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


def _require_evidence(schema: str, evidence: CanonicalJson, digest: str) -> None:
    if not schema.strip() or not evidence or not digest.strip():
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            "Canonical resolver evidence identity must be complete.",
        )


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
) -> ResourceIntentDecision:
    accepted = _validate_observation_identity(snapshot, observed)
    instance = next(value for value in snapshot.resource_instances if value.instance_id == observed.instance_id)
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
) -> ResourceIntentDecision:
    authority, reservation, _use_lease, observation = _mutation_records(
        snapshot,
        value.capability,
        value.recorded_at,
    )
    if not value.intent_id or not value.policy_schema.strip() or not value.policy or not value.policy_digest.strip():
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            "Mutation intent identity and canonical policy must be complete.",
        )
    if any(candidate.intent_id == value.intent_id for candidate in snapshot.mutation_intents) or any(
        candidate.reservation_id == reservation.reservation_id and candidate.state == MutationIntentState.PLANNED
        for candidate in snapshot.mutation_intents
    ):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
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
) -> ResourceIntentDecision:
    if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "Only accepted deterministic evidence can advance a live observation.",
        )
    _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)
    _authority, _reservation, use_lease, _observation = _mutation_records(
        snapshot,
        value.intent.resource,
        value.resolved_at,
    )
    intent = _planned_intent(snapshot, value.intent)
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


def _interrupted_use_lease(snapshot: LedgerSnapshot, intent: MutationIntent) -> MutationUseLease:
    use_lease = next(
        (
            value
            for value in snapshot.mutation_use_leases
            if value.lease_id == intent.resource_use_lease_id and value.generation == intent.resource_use_generation
        ),
        None,
    )
    if use_lease is None:
        raise DecisionError(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, "The interrupted grant is not retained.")
    if any(
        candidate.reservation_id == intent.reservation_id and candidate.generation > use_lease.generation
        for candidate in snapshot.mutation_use_leases
    ):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
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
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "A later mutation intent already exists.",
        )
    return use_lease


def _require_intent_start_observation(snapshot: LedgerSnapshot, intent: MutationIntent) -> None:
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
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "Interruption recovery requires the intent's exact starting observation.",
        )


def _require_active_intent_reservation(snapshot: LedgerSnapshot, intent: MutationIntent) -> None:
    reservation = next(
        (value for value in snapshot.mutation_reservations if value.reservation_id == intent.reservation_id),
        None,
    )
    if reservation is None or reservation.state != ReservationState.ACTIVE:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Ordinary interruption recovery requires the intent's exact active reservation.",
        )


def _validate_recovery_attempt(
    snapshot: LedgerSnapshot,
    intent: MutationIntent,
    supplied: CommandAttemptAuthority,
    now: datetime,
) -> CommandAttemptAuthority:
    authority = _current_attempt_authority(snapshot, supplied, now)
    reservation = next(
        (value for value in snapshot.mutation_reservations if value.reservation_id == intent.reservation_id),
        None,
    )
    if (
        reservation is None
        or reservation.attempt_id != authority.attempt
        or reservation.item_id != authority.item
        or reservation.acquisition_generation != intent.reservation_generation
    ):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            "Recovery authority is cross-wired to another reservation or attempt.",
        )
    if (
        authority.lease_id == intent.attempt_lease_id
        and authority.generation == intent.attempt_lease_generation
        and authority.task_id == intent.task_id
    ):
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Interruption recovery requires fresh attempt authority.",
        )
    return authority


def abandon_mutation_intent(
    snapshot: LedgerSnapshot,
    value: AbandonMutationIntentInput,
) -> ResourceIntentDecision:
    intent = _planned_intent(snapshot, value.intent)
    if not value.reason.strip():
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Abandonment requires a reason.")
    accepted = _validate_observation_identity(snapshot, value.observation)
    if not _observation_is_unchanged(accepted, value.observation):
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "A changed observation cannot be abandoned as unused.",
        )
    match value.form:
        case AbandonmentForm.LIVE_OWNER:
            authority, _reservation, _use, _observation = _mutation_records(
                snapshot,
                value.intent.resource,
                value.decided_at,
            )
            if authority != value.attempt_authority:
                raise DecisionError(
                    DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Live abandonment requires the exact intent owner.",
                )
        case AbandonmentForm.CLEAN_INTERRUPTION:
            old_use = _interrupted_use_lease(snapshot, intent)
            if old_use.state not in {UseLeaseState.RELEASED, UseLeaseState.EXPIRED}:
                raise DecisionError(
                    DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
                    "Clean interruption requires released or expired prior use authority.",
                )
            _require_intent_start_observation(snapshot, intent)
            _require_active_intent_reservation(snapshot, intent)
            _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.decided_at)
        case _ as unreachable:
            raise AssertionError(unreachable)
    abandoned = replace(
        intent,
        state=MutationIntentState.ABANDONED,
        resolved_at=value.decided_at,
        result_observation_generation=accepted.generation,
        result_observation_digest=accepted.digest,
        disposition_task_id=value.attempt_authority.task_id,
        disposition_reason=value.reason,
    )
    return ResourceIntentDecision(IntentDecisionKind.ABANDON, MutationIntentChange(intent, abandoned))


def reconcile_interrupted_observation(
    snapshot: LedgerSnapshot,
    value: ReconcileInterruptedObservationInput,
) -> ResourceIntentDecision:
    if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "Post-interruption reconciliation requires supported deterministic proof.",
        )
    _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)
    intent = _planned_intent(snapshot, value.intent)
    old_use = _interrupted_use_lease(snapshot, intent)
    if old_use.state not in {UseLeaseState.RELEASED, UseLeaseState.EXPIRED}:
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "Reconciliation requires interrupted prior use authority.",
        )
    _require_intent_start_observation(snapshot, intent)
    _require_active_intent_reservation(snapshot, intent)
    authority = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.resolved_at)
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
        disposition_task_id=authority.task_id,
    )


def preserve_resource_state(
    snapshot: LedgerSnapshot,
    value: PreserveResourceStateInput,
) -> ResourceIntentDecision:
    _current_coordination_authority(snapshot, value.coordination_authority, value.resolved_at)
    intent = _planned_intent(snapshot, value.intent)
    authority = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.resolved_at)
    if not value.reason.strip() or not value.fence_lease_id:
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            "Human preserve requires an explicit reason and fence identity.",
        )
    if (value.evidence_schema, value.evidence, value.evidence_digest).count(None) not in {0, 3}:
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            "Human preserve evidence must be wholly present or absent.",
        )
    old_use = _interrupted_use_lease(snapshot, intent)
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
    decision = _advance_intent(
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
    return replace(decision, use_lease_changes=use_changes)


def resolve_fenced_resource_intent(
    snapshot: LedgerSnapshot,
    value: ResolveFencedIntentInput,
) -> ResourceIntentDecision:
    _current_coordination_authority(snapshot, value.coordination_authority, value.resolved_at)
    intent = _planned_intent(snapshot, value.intent)
    authority = _validate_recovery_attempt(snapshot, intent, value.attempt_authority, value.resolved_at)
    reservation = next(
        candidate for candidate in snapshot.mutation_reservations if candidate.reservation_id == intent.reservation_id
    )
    old_use = next(
        (
            candidate
            for candidate in snapshot.mutation_use_leases
            if candidate.lease_id == intent.resource_use_lease_id
            and candidate.generation == intent.resource_use_generation
        ),
        None,
    )
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
        raise DecisionError(
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            "Fenced intent resolution requires the exact fence or recovery quarantine with no later grant.",
        )
    accepted = _validate_observation_identity(snapshot, value.observation)
    unchanged = _observation_is_unchanged(accepted, value.observation)
    reservation_change = (
        MutationReservationChange(reservation, replace(reservation, state=ReservationState.REVOKED))
        if reservation.state == ReservationState.REVOKED_PENDING_RECOVERY
        else None
    )
    if unchanged:
        if value.disposition != FencedIntentDisposition.UNCHANGED:
            raise DecisionError(
                DecisionErrorCode.TRANSITION_INPUT_INVALID,
                "An unchanged fenced intent requires the unchanged disposition.",
            )
        resolved = replace(
            intent,
            state=MutationIntentState.ABANDONED,
            resolved_at=value.resolved_at,
            result_observation_generation=accepted.generation,
            result_observation_digest=accepted.digest,
            disposition_task_id=authority.task_id,
            disposition_reason=value.reason,
        )
        return ResourceIntentDecision(
            IntentDecisionKind.RESOLVE_FENCED,
            MutationIntentChange(intent, resolved),
            reservation_change=reservation_change,
        )
    if value.disposition == FencedIntentDisposition.RECONCILE:
        if value.resolver_decision != ResolverEvidenceDecision.ACCEPTED:
            raise DecisionError(
                DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
                "Changed fenced state requires supported deterministic proof or human preserve.",
            )
        state = MutationIntentState.RECONCILED
        if value.evidence_schema is None or value.evidence is None or value.evidence_digest is None:
            raise DecisionError(
                DecisionErrorCode.TRANSITION_INPUT_INVALID,
                "Mechanical reconciliation requires canonical evidence.",
            )
        _require_evidence(value.evidence_schema, value.evidence, value.evidence_digest)
    elif value.disposition == FencedIntentDisposition.HUMAN_PRESERVE:
        if not value.reason.strip():
            raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Human preserve requires a reason.")
        state = MutationIntentState.HUMAN_PRESERVED
        if (value.evidence_schema, value.evidence, value.evidence_digest).count(None) not in {0, 3}:
            raise DecisionError(
                DecisionErrorCode.TRANSITION_INPUT_INVALID,
                "Human preserve evidence must be wholly present or absent.",
            )
    else:
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            "Changed fenced state cannot use the unchanged disposition.",
        )
    decision = _advance_intent(
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
        disposition_task_id=authority.task_id,
        disposition_reason=value.reason,
    )
    return replace(decision, reservation_change=reservation_change)
