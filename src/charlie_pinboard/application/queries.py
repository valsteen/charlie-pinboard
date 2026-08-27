from datetime import UTC, datetime
from enum import Enum

import msgspec

from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    AttemptLeaseState,
    ItemDependency,
    ItemResourceRequirement,
    ProposalEvidence,
    ProposalFreshness,
    ReservationState,
    ResourceMutationIntent,
    StoredAttempt,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredResourceUseLease,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.identifiers import ItemId, ResourceInstanceId
from charlie_pinboard.domain.model import AttemptState, MutationIntentState, WorkState


class QueryError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class PlanQueryError(QueryError):
    pass


def _resource_use_generation(value: StoredResourceUseLease) -> int:
    return value.generation


def _resource_intent_id(value: ResourceMutationIntent) -> str:
    return str(value.intent_id)


class DetailLevel(Enum):
    COMPACT = "compact"
    DETAILED = "detailed"


class ParallelOutcome(Enum):
    LAUNCHABLE = "launchable"
    REQUIRES_SELECTION = "requires-selection"
    EXCLUDED = "excluded"


class ParallelSelection(Enum):
    ALL_SAFE = "all-safe"
    SELECTED = "selected"


class ParallelReasonCode(Enum):
    ATTEMPT_OWNED = "attempt-owned"
    DEPENDENCY_LIVE = "dependency-live"
    RESOURCE_BUSY = "resource-busy"
    RESOURCE_CONFLICT = "resource-conflict"
    RESOURCE_SELECTION_REQUIRED = "resource-selection-required"
    STATE_NOT_LAUNCHABLE = "state-not-launchable"


class OverviewItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str


class WorkOverview(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    inbox: tuple[str, ...]
    immediate_options: tuple[str, ...]


class ParallelReason(msgspec.Struct, frozen=True):
    code: ParallelReasonCode
    message: str


class ParallelItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: WorkState
    attempt_id: str | None
    resources: tuple[str, ...]
    outcome: ParallelOutcome
    reasons: tuple[ParallelReason, ...] = ()


class ParallelPreview(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    host_id: str
    selection: ParallelSelection
    safe: bool
    launchable: tuple[ParallelItem, ...]
    requires_selection: tuple[ParallelItem, ...]
    excluded: tuple[ParallelItem, ...]


class ResourceUseDetail(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reservation_id: str
    instance_id: str
    reservation_generation: int
    attempt_id: str
    host_id: str
    instance_subject_revision: int
    observation_generation: int
    observation_digest: str
    task_id: str
    attempt_lease_id: str
    attempt_lease_generation: int
    lease_id: str
    generation: int
    generation_kind: str
    host_epoch: int
    acquired_at: str
    expires_at: str
    state: str


class ResourceIntentDetail(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    intent_id: str
    reservation_id: str
    reservation_generation: int
    instance_id: str
    attempt_id: str
    host_id: str
    resource_use_generation: int
    resource_use_lease_id: str
    task_id: str
    attempt_lease_id: str
    attempt_lease_generation: int
    start_instance_subject_revision: int
    start_observation_generation: int
    start_observation_digest: str
    policy_schema: str
    policy: msgspec.Raw
    policy_digest: str
    state: str
    recorded_at: str
    resolved_at: str | None
    result_observation_generation: int | None
    result_observation_digest: str | None
    evidence_schema: str | None
    evidence: msgspec.Raw | None
    evidence_digest: str | None
    disposition_task_id: str | None
    disposition_reason: str | None


class ResourceAuthorityDetail(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority: str
    lease_id: str
    task_id: str
    host_id: str
    generation: int
    acquired_at: str
    expires_at: str
    state: str


class ResourceHistoryDetail(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    project_revision: int
    action_id: str
    action_kind: str
    subject_id: str
    artifact_ref_id: int | None
    authorization: str
    actor_task_id: str | None
    actor_host_id: str | None
    input_schema: str
    input_payload: msgspec.Raw
    outcome_schema: str
    outcome_payload: msgspec.Raw
    committed_at: str


class ResourceConflictView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    detail: str
    instance_id: str
    resource_id: str
    state: str
    consequence: str
    locator: msgspec.Raw | None
    reservation_id: str | None
    attempt_id: str | None
    legal_actions: tuple[str, ...]
    definition_kind: str | None
    definition_description: str | None
    host_id: str | None
    discovery_kind: str | None
    discovery_fingerprint: str | None
    instance_subject_revision: int | None
    instance_recorded_at: str | None
    instance_updated_at: str | None
    locator_schema: str | None
    observation_generation: int | None
    observation_digest: str | None
    observed_at: str | None
    reservation_generation: int | None
    reservation_state: str | None
    item_id: str | None
    reservation_created_at: str | None
    reservation_ended_at: str | None
    task_uses: tuple[ResourceUseDetail, ...]
    mutation_intents: tuple[ResourceIntentDetail, ...]
    coordination_authority: ResourceAuthorityDetail | None
    attempt_authority: ResourceAuthorityDetail | None
    history: tuple[ResourceHistoryDetail, ...]
    recovery_required: bool


def _dependency_key(value: ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: ItemDependency) -> int:
    return value.position


def _item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _requirement_position(value: ItemResourceRequirement) -> int:
    return value.position


def _parallel_item_key(value: ParallelItem) -> str:
    return value.item_id


def _obligation_key(value: StoredPlanningObligation) -> tuple[str, str]:
    return str(value.impact_id), str(value.target_item_id)


def _replacement_position(value: StoredPlanningReplacement) -> int:
    return value.position


def _proposal_evidence_position(value: ProposalEvidence) -> int:
    return value.position


def _proposal_freshness_position(value: ProposalFreshness) -> int:
    return value.position


_TERMINAL_ITEM_STATES = {
    StoredWorkItemState.DONE,
    StoredWorkItemState.SUPERSEDED,
    StoredWorkItemState.DROPPED,
}


def _canonical_bytes(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


def _work_state(value: StoredWorkItemState) -> WorkState:
    try:
        return WorkState(value.value)
    except ValueError as error:
        raise QueryError("WORK_STATE_INVALID", f"Item state {value.value!r} is not live.") from error


def overview_from_state(state: StoredWorkState) -> WorkOverview:
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
    }
    dependencies = {
        item.item_id: tuple(
            str(link.dependency_id)
            for link in sorted(
                (candidate for candidate in state.lifecycle.dependencies if candidate.item_id == item.item_id),
                key=_dependency_key,
            )
        )
        for item in state.lifecycle.work_items
    }
    items = tuple(
        OverviewItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            item.timing.value if item.timing is not None else None,
            dependencies[item.item_id],
            str(attempts[item.item_id]) if item.item_id in attempts else None,
            item.next_action,
            item.notes or "",
        )
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if item.state not in _TERMINAL_ITEM_STATES
    )
    live_ids = frozenset(item.item_id for item in items)
    immediate = tuple(
        item.item_id
        for item in items
        if item.state in {WorkState.INTAKE, WorkState.READY, WorkState.DEFERRED}
        or (
            item.state in {WorkState.PAUSED, WorkState.BLOCKED}
            and not any(dependency in live_ids for dependency in item.depends_on)
        )
    )
    inbox = tuple(
        str(proposal.proposal_id)
        for proposal in sorted(state.proposals.proposals, key=_proposal_key)
        if proposal.disposition is None
    )
    return WorkOverview(
        "repo-work-overview/v1",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(state.focus.item_id) if state.focus.item_id is not None else None,
        str(state.focus.attempt_id) if state.focus.attempt_id is not None else None,
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == AttemptState.ACTIVE
        ),
        items,
        inbox,
        immediate,
    )


def read_overview(store: WorkStore) -> WorkOverview:
    return overview_from_state(store.snapshot())


def _preview_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise QueryError("PARALLEL_TIME_INVALID", "Preview time must be timezone-aware.")
    return current.astimezone(UTC)


def _parallel_reasons(
    state: StoredWorkState,
    item_id: ItemId,
    live_items: frozenset[str],
    host_id: str,
    current: datetime,
) -> tuple[ParallelReason, ...]:
    item = next(value for value in state.lifecycle.work_items if value.item_id == item_id)
    if item.state not in {StoredWorkItemState.READY, StoredWorkItemState.ACTIVE}:
        return (
            ParallelReason(
                ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    live_dependencies = tuple(
        str(link.dependency_id)
        for link in sorted(state.lifecycle.dependencies, key=_dependency_position)
        if link.item_id == item_id and str(link.dependency_id) in live_items
    )
    if live_dependencies:
        return (
            ParallelReason(
                ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    attempt = next(
        (
            candidate
            for candidate in state.lifecycle.attempts
            if candidate.item_id == item_id and candidate.state == AttemptState.ACTIVE
        ),
        None,
    )
    if attempt is not None:
        lease = next(
            (candidate for candidate in state.authority.attempt_leases if candidate.attempt_id == attempt.attempt_id),
            None,
        )
        if lease is not None and lease.state == AttemptLeaseState.ACTIVE and current < lease.expires_at:
            return (
                ParallelReason(
                    ParallelReasonCode.ATTEMPT_OWNED,
                    f"Active attempt '{attempt.attempt_id}' is owned until {lease.expires_at.isoformat()}.",
                ),
            )
    required = {
        requirement.resource_id for requirement in state.resources.requirements if requirement.item_id == item_id
    }
    busy = tuple(
        reservation
        for reservation in state.resources.reservations
        if reservation.resource_id in required
        and reservation.host_id == host_id
        and reservation.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
        and reservation.item_id != item_id
    )
    return tuple(
        ParallelReason(
            ParallelReasonCode.RESOURCE_BUSY,
            f"Resource '{reservation.resource_id}' on '{host_id}' is retained by attempt '{reservation.attempt_id}'.",
        )
        for reservation in busy
    )


def preview_parallel(
    store: WorkStore,
    host_id: str,
    *,
    selected: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ParallelPreview:
    if not host_id:
        raise QueryError("PARALLEL_HOST_INVALID", "A host identity is required.")
    state = store.snapshot()
    current = _preview_time(now)
    live = tuple(item for item in state.lifecycle.work_items if item.state not in _TERMINAL_ITEM_STATES)
    by_id = {str(item.item_id): item for item in live}
    if len(selected) != len(set(selected)) or any(item_id not in by_id for item_id in selected):
        raise QueryError("PARALLEL_SELECTION_INVALID", "Selected item identities must be unique current items.")
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else tuple(sorted(live, key=_item_key))
    live_ids = frozenset(by_id)
    launchable: list[ParallelItem] = []
    excluded: list[ParallelItem] = []
    for item in candidates:
        resources = tuple(
            str(value.resource_id)
            for value in sorted(state.resources.requirements, key=_requirement_position)
            if value.item_id == item.item_id
        )
        reasons = _parallel_reasons(state, item.item_id, live_ids, host_id, current)
        value = ParallelItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            str(
                next(
                    (
                        attempt.attempt_id
                        for attempt in state.lifecycle.attempts
                        if attempt.item_id == item.item_id
                        and attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
                    ),
                    "",
                )
            )
            or None,
            resources,
            ParallelOutcome.EXCLUDED if reasons else ParallelOutcome.LAUNCHABLE,
            reasons,
        )
        (excluded if reasons else launchable).append(value)
    by_resource: dict[str, list[str]] = {}
    for item in launchable:
        for resource in item.resources:
            by_resource.setdefault(resource, []).append(item.item_id)
    conflicts = {resource: ids for resource, ids in by_resource.items() if len(ids) > 1}
    requires_selection: list[ParallelItem] = []
    if conflicts:
        retained: list[ParallelItem] = []
        for item in launchable:
            shared = tuple(resource for resource in item.resources if resource in conflicts)
            if not shared:
                retained.append(item)
                continue
            reason = ParallelReason(
                ParallelReasonCode.RESOURCE_CONFLICT if selected else ParallelReasonCode.RESOURCE_SELECTION_REQUIRED,
                f"Selected items share host-local resources: {', '.join(shared)}."
                if selected
                else f"Multiple candidates need host-local resources: {', '.join(shared)}; select one explicitly.",
            )
            revised = msgspec.structs.replace(
                item,
                outcome=ParallelOutcome.EXCLUDED if selected else ParallelOutcome.REQUIRES_SELECTION,
                reasons=(reason,),
            )
            (excluded if selected else requires_selection).append(revised)
        launchable = retained
    return ParallelPreview(
        "repo-work-parallel-preview/v1",
        str(state.lifecycle.project.revision),
        host_id,
        ParallelSelection.SELECTED if selected else ParallelSelection.ALL_SAFE,
        not selected or not excluded,
        tuple(sorted(launchable, key=_parallel_item_key)),
        tuple(sorted(requires_selection, key=_parallel_item_key)),
        tuple(sorted(excluded, key=_parallel_item_key)),
    )


def read_resource_conflict(
    store: WorkStore,
    instance: ResourceInstanceId,
    detail: DetailLevel = DetailLevel.COMPACT,
) -> ResourceConflictView:
    state = store.snapshot()
    selected = next((value for value in state.resources.instances if value.instance_id == instance), None)
    if selected is None:
        raise QueryError("RESOURCE_INSTANCE_REQUIRED", f"Resource instance '{instance}' is not retained.")
    reservation = next(
        (
            value
            for value in state.resources.reservations
            if value.instance_id == instance
            and value.state in {ReservationState.ACTIVE, ReservationState.REVOKED_PENDING_RECOVERY}
        ),
        None,
    )
    locator = next((value for value in state.resources.locators if value.instance_id == instance), None)
    consequence = (
        f"Resource '{selected.resource_id}' is retained by attempt '{reservation.attempt_id}'."
        if reservation is not None
        else f"Resource '{selected.resource_id}' is available for explicit assignment."
    )
    planned_intent = reservation is not None and any(
        value.reservation_id == reservation.reservation_id and value.state == MutationIntentState.PLANNED
        for value in state.resources.mutation_intents
    )
    legal = (
        ("inspect", "resolve-fenced-resource-intent")
        if reservation is not None and reservation.state == ReservationState.REVOKED_PENDING_RECOVERY
        else ("inspect", "preserve", "revoke-reservation")
        if planned_intent
        else ("inspect", "release-reservation", "revoke-reservation")
        if reservation is not None
        else ("inspect", "assign")
    )
    detailed = detail == DetailLevel.DETAILED
    definition = next(
        (value for value in state.resources.definitions if value.resource_id == selected.resource_id),
        None,
    )
    task_uses = tuple(
        ResourceUseDetail(
            str(value.reservation_id),
            str(value.instance_id),
            value.reservation_generation,
            str(value.attempt_id),
            str(value.host_id),
            value.instance_subject_revision,
            value.observation_generation,
            value.observation_digest,
            str(value.task_id),
            str(value.attempt_lease_id),
            value.attempt_lease_generation,
            str(value.lease_id),
            value.generation,
            value.generation_kind.value,
            value.host_epoch,
            value.acquired_at.isoformat(),
            value.expires_at.isoformat(),
            value.state.value,
        )
        for value in sorted(state.resources.use_leases, key=_resource_use_generation)
        if detailed and value.instance_id == instance
    )
    intents = tuple(
        ResourceIntentDetail(
            str(value.intent_id),
            str(value.reservation_id),
            value.reservation_generation,
            str(value.instance_id),
            str(value.attempt_id),
            str(value.host_id),
            value.resource_use_generation,
            str(value.resource_use_lease_id),
            str(value.task_id),
            str(value.attempt_lease_id),
            value.attempt_lease_generation,
            value.start_instance_subject_revision,
            value.start_observation_generation,
            value.start_observation_digest,
            value.policy_schema,
            msgspec.Raw(bytes(value.policy)),
            value.policy_digest,
            value.state.value,
            value.recorded_at.isoformat(),
            value.resolved_at.isoformat() if value.resolved_at is not None else None,
            value.result_observation_generation,
            value.result_observation_digest,
            value.evidence_schema,
            msgspec.Raw(bytes(value.evidence)) if value.evidence is not None else None,
            value.evidence_digest,
            str(value.disposition_task_id) if value.disposition_task_id is not None else None,
            value.disposition_reason,
        )
        for value in sorted(state.resources.mutation_intents, key=_resource_intent_id)
        if detailed and value.instance_id == instance
    )
    coordination = state.authority.coordination if detailed else None
    coordination_detail = (
        ResourceAuthorityDetail(
            "coordination",
            str(coordination.lease_id),
            str(coordination.task_id),
            str(coordination.host_id),
            coordination.generation,
            coordination.acquired_at.isoformat(),
            coordination.expires_at.isoformat(),
            coordination.state.value,
        )
        if coordination is not None
        else None
    )
    attempt_detail: ResourceAuthorityDetail | None = None
    if detailed and reservation is not None:
        lease = next(
            (value for value in state.authority.attempt_leases if value.attempt_id == reservation.attempt_id),
            None,
        )
        anchor = (
            next(
                (
                    value
                    for value in state.authority.attempt_generations
                    if value.attempt_id == reservation.attempt_id
                    and lease is not None
                    and value.generation == lease.generation
                ),
                None,
            )
            if lease is not None
            else None
        )
        if lease is not None and anchor is not None:
            attempt_detail = ResourceAuthorityDetail(
                "attempt",
                str(anchor.lease_id),
                str(anchor.task_id),
                str(anchor.host_id),
                lease.generation,
                lease.acquired_at.isoformat(),
                lease.expires_at.isoformat(),
                lease.state.value,
            )
    subjects = {
        str(selected.resource_id),
        str(instance),
    }
    subjects.update(value.intent_id for value in intents)
    if reservation is not None:
        subjects.update((str(reservation.reservation_id), str(reservation.attempt_id)))
    history = tuple(
        ResourceHistoryDetail(
            int(value.history_id),
            value.project_revision,
            str(value.action_id),
            value.action_kind.value,
            str(value.subject_id),
            int(value.artifact_ref_id) if value.artifact_ref_id is not None else None,
            value.authorization.value,
            str(value.actor_task_id) if value.actor_task_id is not None else None,
            str(value.actor_host_id) if value.actor_host_id is not None else None,
            value.input_schema,
            msgspec.Raw(bytes(value.input_payload)),
            value.outcome_schema,
            msgspec.Raw(bytes(value.outcome_payload)),
            value.committed_at.isoformat(),
        )
        for value in state.history.receipts
        if detailed and str(value.subject_id) in subjects
    )
    return ResourceConflictView(
        "resource-conflict/v1",
        detail.value,
        str(instance),
        str(selected.resource_id),
        selected.state.value,
        consequence,
        msgspec.Raw(bytes(locator.locator)) if detailed and locator is not None else None,
        str(reservation.reservation_id) if detailed and reservation is not None else None,
        str(reservation.attempt_id) if detailed and reservation is not None else None,
        legal,
        definition.kind if detailed and definition is not None else None,
        definition.description if detailed and definition is not None else None,
        str(selected.host_id) if detailed else None,
        selected.discovery_kind if detailed else None,
        selected.discovery_fingerprint if detailed else None,
        selected.subject_revision if detailed else None,
        selected.recorded_at.isoformat() if detailed else None,
        selected.updated_at.isoformat() if detailed else None,
        locator.locator_schema if detailed and locator is not None else None,
        locator.observation_generation if detailed and locator is not None else None,
        locator.observation_digest if detailed and locator is not None else None,
        locator.observed_at.isoformat() if detailed and locator is not None else None,
        reservation.acquisition_generation if detailed and reservation is not None else None,
        reservation.state.value if detailed and reservation is not None else None,
        str(reservation.item_id) if detailed and reservation is not None else None,
        reservation.created_at.isoformat() if detailed and reservation is not None else None,
        reservation.ended_at.isoformat() if detailed and reservation is not None and reservation.ended_at else None,
        task_uses,
        intents,
        coordination_detail,
        attempt_detail,
        history,
        reservation is not None and reservation.state == ReservationState.REVOKED_PENDING_RECOVERY,
    )
