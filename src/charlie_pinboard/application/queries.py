import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

import msgspec

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    AttemptLeaseState,
    ItemDependency,
    ItemResourceRequirement,
    PlanningObligationState,
    ProposalEvidence,
    ProposalFreshness,
    ReservationState,
    ResourceMutationIntent,
    StoredAttempt,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredResourceUseLease,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.errors import DecisionFailure
from charlie_pinboard.domain.history import (
    ItemScopeRecord,
    ScopeArtifactRecord,
    ScopeDependencyRecord,
    ScopeResourceRequirementRecord,
    item_scope_bytes,
)
from charlie_pinboard.domain.identifiers import ItemId, ResourceInstanceId
from charlie_pinboard.domain.model import AttemptState, MutationIntentState, PlanningDisposition, WorkState


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


class ScopeAnchorView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    scope_digest: str
    scope_revision: int


class PlanItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    scope_revision: int
    scope_digest: str
    semantic: msgspec.Raw
    lifecycle_state: str
    outcome_evidence: str | None


class UnresolvedObligation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    impact_id: str
    source_item_id: str
    source_attempt_id: str | None
    source_scope: ScopeAnchorView
    target_item_id: str
    target_position: int
    target_scope: ScopeAnchorView
    summary: str
    evidence: str
    recorded_project_revision: int


class ReplacementRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    position: int
    item_id: str


class ResolvedObligation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    impact_id: str
    source_item_id: str
    source_attempt_id: str | None
    source_scope: ScopeAnchorView
    target_item_id: str
    target_position: int
    target_scope: ScopeAnchorView
    summary: str
    evidence: str
    recorded_project_revision: int
    evaluated_scope: ScopeAnchorView
    resulting_scope: ScopeAnchorView | None
    disposition: str
    reason: str
    outcome_evidence: str | None
    replacements: tuple[ReplacementRecord, ...]
    resolved_project_revision: int


class ProposalRelationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: str
    item_id: str | None


class ProposalEvidenceRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    position: int
    selector: str


class ProposalFreshnessRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    position: int
    assumption: str


class UndecidedProposal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: str
    source_task_id: str
    user_label: str
    trigger: str
    why_it_matters: str
    relation: ProposalRelationRecord
    effect: str
    unlock: str
    urgency_evidence: str
    evidence: tuple[ProposalEvidenceRecord, ...]
    freshness_assumptions: tuple[ProposalFreshnessRecord, ...]
    proposal_sha256: str


class _UndecidedProposalPreimage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    proposal_id: str
    source_task_id: str
    user_label: str
    trigger: str
    why_it_matters: str
    relation: ProposalRelationRecord
    effect: str
    unlock: str
    urgency_evidence: str
    evidence: tuple[ProposalEvidenceRecord, ...]
    freshness_assumptions: tuple[ProposalFreshnessRecord, ...]


class PlanSnapshot(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    application: str
    database_schema_version: int
    project_revision: int
    requested_roots: tuple[str, ...]
    include_undecided: bool
    status: str
    items: tuple[PlanItem, ...]
    unresolved_obligations: tuple[UnresolvedObligation, ...]
    resolved_obligations: tuple[ResolvedObligation, ...]
    undecided: tuple[UndecidedProposal, ...]
    manifest_sha256: str


class _PlanSnapshotPreimage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    application: str
    database_schema_version: int
    project_revision: int
    requested_roots: tuple[str, ...]
    include_undecided: bool
    status: str
    items: tuple[PlanItem, ...]
    unresolved_obligations: tuple[UnresolvedObligation, ...]
    resolved_obligations: tuple[ResolvedObligation, ...]
    undecided: tuple[UndecidedProposal, ...]


class ItemAnchorChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    before: ScopeAnchorView
    after: ScopeAnchorView


class ItemPresenceChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    scope: ScopeAnchorView


class ItemLifecycleChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    before_state: str
    before_outcome_evidence: str | None
    after_state: str
    after_outcome_evidence: str | None


class ItemDependencyChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    before: tuple[ScopeDependencyRecord, ...]
    after: tuple[ScopeDependencyRecord, ...]


class ItemResourceChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    before: tuple[ScopeResourceRequirementRecord, ...]
    after: tuple[ScopeResourceRequirementRecord, ...]


class ItemArtifactChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    before: tuple[ScopeArtifactRecord, ...]
    after: tuple[ScopeArtifactRecord, ...]


type PlanObligation = UnresolvedObligation | ResolvedObligation


class ReplacementChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    change: Literal["added"]
    impact_id: str
    target_item_id: str
    evaluated_scope: ScopeAnchorView
    replacements: tuple[ReplacementRecord, ...]
    resolved_project_revision: int


class ObligationEnteredScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    after: PlanObligation


class ObligationLeftScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    before: PlanObligation


class ObligationOpened(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    after: PlanObligation


class ObligationResolved(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    before: UnresolvedObligation | None
    after: ResolvedObligation


class UndecidedChange(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    change: Literal["added", "removed", "changed"]
    proposal_id: str
    before_proposal_sha256: str | None
    after_proposal_sha256: str | None


class PlanChanges(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    added: tuple[ItemPresenceChange, ...]
    removed: tuple[ItemPresenceChange, ...]
    scope_changed: tuple[ItemAnchorChange, ...]
    dependencies_changed: tuple[ItemDependencyChange, ...]
    resources_changed: tuple[ItemResourceChange, ...]
    artifacts_changed: tuple[ItemArtifactChange, ...]
    lifecycle_only: tuple[ItemLifecycleChange, ...]
    replacements: tuple[ReplacementChange, ...]
    obligations_entered_scope: tuple[ObligationEnteredScope, ...]
    obligations_left_scope: tuple[ObligationLeftScope, ...]
    obligations_opened: tuple[ObligationOpened, ...]
    obligations_resolved: tuple[ObligationResolved, ...]
    undecided_changed: tuple[UndecidedChange, ...]


class PlanChangeSet(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    before_manifest_sha256: str
    after_manifest_sha256: str
    requested_roots: tuple[str, ...]
    include_undecided: bool
    changes: PlanChanges
    change_set_sha256: str


class _PlanChangeSetPreimage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    before_manifest_sha256: str
    after_manifest_sha256: str
    requested_roots: tuple[str, ...]
    include_undecided: bool
    changes: PlanChanges


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


def _closure(state: StoredWorkState, roots: tuple[str, ...]) -> tuple[str, ...]:
    admitted = {str(item.item_id) for item in state.lifecycle.work_items}
    if not roots or len(roots) != len(set(roots)) or any(root not in admitted for root in roots):
        raise PlanQueryError("PLAN_SELECTION_INVALID", "Plan roots must be unique admitted item identities.")
    dependencies = {
        str(item.item_id): tuple(
            str(link.dependency_id)
            for link in sorted(state.lifecycle.dependencies, key=_dependency_position)
            if link.item_id == item.item_id
        )
        for item in state.lifecycle.work_items
    }
    selected = set(roots)
    pending = list(roots)
    while pending:
        for dependency in dependencies[pending.pop()]:
            if dependency not in admitted:
                raise PlanQueryError("WORK_STATE_INVALID", "A selected dependency is not admitted.")
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return tuple(sorted(selected))


def _scope_items(state: StoredWorkState, closure: tuple[str, ...]) -> tuple[PlanItem, ...]:
    snapshot = project_decision_snapshot(state)
    scopes = {str(scope.item): scope for scope in snapshot.scopes}
    items = {str(item.item_id): item for item in state.lifecycle.work_items}
    result: list[PlanItem] = []
    for item_id in closure:
        item = items[item_id]
        scope = scopes.get(item_id)
        if scope is None:
            raise PlanQueryError("WORK_STATE_INVALID", f"Selected item '{item_id}' has no current semantic scope.")
        encoded = item_scope_bytes(scope.scope)
        if isinstance(encoded, DecisionFailure):
            raise PlanQueryError(encoded.code.value, encoded.message)
        if hashlib.sha256(encoded).hexdigest() != item.scope_digest:
            raise PlanQueryError("WORK_STATE_INVALID", f"Selected item '{item_id}' has a mismatched scope digest.")
        result.append(
            PlanItem(
                item_id,
                item.scope_revision,
                item.scope_digest,
                msgspec.Raw(encoded.rstrip(b"\n")),
                item.state.value,
                item.outcome_evidence,
            )
        )
    return tuple(result)


def _impact_by_id(state: StoredWorkState) -> dict[str, StoredPlanningImpact]:
    return {str(impact.impact_id): impact for impact in state.planning.impacts}


def _obligation_base(
    impact: StoredPlanningImpact, obligation: StoredPlanningObligation
) -> tuple[str, str, str | None, ScopeAnchorView, str, int, ScopeAnchorView, str, str, int]:
    return (
        str(impact.impact_id),
        str(impact.source_item_id),
        str(impact.source_attempt_id) if impact.source_attempt_id is not None else None,
        ScopeAnchorView(impact.source_scope_digest, impact.source_scope_revision),
        str(obligation.target_item_id),
        obligation.position,
        ScopeAnchorView(obligation.observed_scope_digest, obligation.observed_scope_revision),
        impact.summary,
        impact.evidence,
        impact.recorded_project_revision,
    )


def _plan_obligations(
    state: StoredWorkState, closure: tuple[str, ...]
) -> tuple[tuple[UnresolvedObligation, ...], tuple[ResolvedObligation, ...]]:
    selected = set(closure)
    impacts = _impact_by_id(state)
    unresolved: list[UnresolvedObligation] = []
    resolved: list[ResolvedObligation] = []
    for obligation in sorted(state.planning.obligations, key=_obligation_key):
        impact = impacts[str(obligation.impact_id)]
        replacements = tuple(
            ReplacementRecord(value.position, str(value.replacement_item_id))
            for value in sorted(state.planning.replacements, key=_replacement_position)
            if value.impact_id == obligation.impact_id and value.target_item_id == obligation.target_item_id
        )
        relevant = (
            str(impact.source_item_id) in selected
            or str(obligation.target_item_id) in selected
            or any(replacement.item_id in selected for replacement in replacements)
        )
        if not relevant:
            continue
        base = _obligation_base(impact, obligation)
        if obligation.state == PlanningObligationState.UNRESOLVED:
            unresolved.append(UnresolvedObligation(*base))
            continue
        if (
            obligation.disposition is None
            or obligation.evaluated_scope_revision is None
            or obligation.evaluated_scope_digest is None
            or obligation.reason is None
            or obligation.resolved_project_revision is None
        ):
            raise PlanQueryError("WORK_STATE_INVALID", "A resolved planning obligation is incomplete.")
        resulting = (
            ScopeAnchorView(obligation.resulting_scope_digest, obligation.resulting_scope_revision)
            if obligation.resulting_scope_digest is not None and obligation.resulting_scope_revision is not None
            else None
        )
        resolved.append(
            ResolvedObligation(
                *base,
                ScopeAnchorView(obligation.evaluated_scope_digest, obligation.evaluated_scope_revision),
                resulting,
                obligation.disposition.value,
                obligation.reason,
                obligation.outcome_evidence,
                replacements,
                obligation.resolved_project_revision,
            )
        )
    return tuple(unresolved), tuple(resolved)


def _proposal(state: StoredWorkState, proposal: StoredProposal) -> UndecidedProposal:
    evidence = tuple(
        ProposalEvidenceRecord(value.position, value.selector)
        for value in sorted(state.proposals.evidence, key=_proposal_evidence_position)
        if value.proposal_id == proposal.proposal_id
    )
    freshness = tuple(
        ProposalFreshnessRecord(value.position, value.assumption)
        for value in sorted(state.proposals.freshness, key=_proposal_freshness_position)
        if value.proposal_id == proposal.proposal_id
    )
    preimage = _UndecidedProposalPreimage(
        str(proposal.proposal_id),
        str(proposal.source_task_id),
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        ProposalRelationRecord(
            proposal.relation.value,
            str(proposal.relation_item_id) if proposal.relation_item_id is not None else None,
        ),
        proposal.effect,
        proposal.unlock,
        proposal.urgency_evidence,
        evidence,
        freshness,
    )
    return UndecidedProposal(
        preimage.proposal_id,
        preimage.source_task_id,
        preimage.user_label,
        preimage.trigger,
        preimage.why_it_matters,
        preimage.relation,
        preimage.effect,
        preimage.unlock,
        preimage.urgency_evidence,
        preimage.evidence,
        preimage.freshness_assumptions,
        hashlib.sha256(_canonical_bytes(preimage)).hexdigest(),
    )


def _snapshot_preimage(value: PlanSnapshot) -> _PlanSnapshotPreimage:
    return _PlanSnapshotPreimage(
        value.schema,
        value.application,
        value.database_schema_version,
        value.project_revision,
        value.requested_roots,
        value.include_undecided,
        value.status,
        value.items,
        value.unresolved_obligations,
        value.resolved_obligations,
        value.undecided,
    )


def read_plan_snapshot(
    store: WorkStore,
    roots: tuple[ItemId, ...],
    include_undecided: bool = False,
    *,
    require_reconciled: bool = False,
) -> PlanSnapshot:
    state = store.snapshot()
    requested = tuple(sorted(str(root) for root in roots))
    closure = _closure(state, requested)
    items = _scope_items(state, closure)
    unresolved, resolved = _plan_obligations(state, closure)
    status = "unreconciled" if unresolved else "reconciled"
    if require_reconciled and unresolved:
        raise PlanQueryError("PLAN_UNRECONCILED", "The selected plan still has unresolved obligations.")
    undecided = (
        tuple(
            _proposal(state, proposal)
            for proposal in sorted(state.proposals.proposals, key=_proposal_key)
            if proposal.disposition is None
        )
        if include_undecided
        else ()
    )
    draft = PlanSnapshot(
        "plan-snapshot/v1",
        state.lifecycle.project.application,
        state.lifecycle.project.schema_version,
        state.lifecycle.project.revision,
        requested,
        include_undecided,
        status,
        items,
        unresolved,
        resolved,
        undecided,
        "",
    )
    return msgspec.structs.replace(
        draft,
        manifest_sha256=hashlib.sha256(_canonical_bytes(_snapshot_preimage(draft))).hexdigest(),
    )


def _validate_snapshot_identity(value: PlanSnapshot) -> None:
    expected = hashlib.sha256(_canonical_bytes(_snapshot_preimage(value))).hexdigest()
    if (
        value.schema != "plan-snapshot/v1"
        or value.application != "charlie-pinboard"
        or value.database_schema_version != 1
        or value.project_revision < 0
        or value.manifest_sha256 != expected
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan snapshot identity does not match its manifest.")
    if (
        not value.requested_roots
        or value.requested_roots != tuple(sorted(set(value.requested_roots)))
        or any(not root for root in value.requested_roots)
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan roots are not canonical.")
    if tuple(item.item_id for item in value.items) != tuple(sorted({item.item_id for item in value.items})):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan items are not canonically ordered and unique.")


def _validated_snapshot_scopes(value: PlanSnapshot) -> dict[str, ItemScopeRecord]:
    scopes: dict[str, ItemScopeRecord] = {}
    for item in value.items:
        try:
            semantic = msgspec.json.decode(bytes(item.semantic), type=ItemScopeRecord)
        except (msgspec.DecodeError, ValueError) as error:
            raise PlanQueryError(
                "PLAN_SNAPSHOT_INVALID", f"Item '{item.item_id}' has invalid semantic scope."
            ) from error
        canonical = _canonical_bytes(semantic)
        terminal = item.lifecycle_state in {
            StoredWorkItemState.DONE.value,
            StoredWorkItemState.DROPPED.value,
            StoredWorkItemState.SUPERSEDED.value,
        }
        if (
            not item.item_id
            or item.scope_revision < 1
            or semantic.item_id != item.item_id
            or bytes(item.semantic) != canonical.removesuffix(b"\n")
            or hashlib.sha256(canonical).hexdigest() != item.scope_digest
            or item.lifecycle_state not in {state.value for state in StoredWorkItemState}
            or terminal != (item.outcome_evidence is not None)
            or item.outcome_evidence == ""
        ):
            raise PlanQueryError("PLAN_SNAPSHOT_INVALID", f"Item '{item.item_id}' contradicts its semantic scope.")
        scopes[item.item_id] = semantic
    if any(root not in scopes for root in value.requested_roots):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "A requested root is absent from the manifest items.")
    closure = set(value.requested_roots)
    pending = list(value.requested_roots)
    while pending:
        for dependency in scopes[pending.pop()].dependencies:
            if dependency.dependency_id not in scopes:
                raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "A selected dependency is absent from the manifest.")
            if dependency.dependency_id not in closure:
                closure.add(dependency.dependency_id)
                pending.append(dependency.dependency_id)
    if closure != set(scopes):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan items do not equal the requested prerequisite closure.")
    return scopes


def _validate_resolved_obligation(value: ResolvedObligation, project_revision: int) -> tuple[str, ...]:
    positions = tuple(replacement.position for replacement in value.replacements)
    identities = tuple(replacement.item_id for replacement in value.replacements)
    terminal = value.disposition in {"dropped", "superseded"}
    if (
        value.recorded_project_revision < 1
        or value.resolved_project_revision < value.recorded_project_revision
        or value.resolved_project_revision > project_revision
        or positions != tuple(range(len(positions)))
        or len(identities) != len(set(identities))
        or any(not identity for identity in identities)
        or value.disposition not in {disposition.value for disposition in PlanningDisposition}
        or (value.disposition == "superseded") != bool(value.replacements)
        or terminal != (value.outcome_evidence is not None)
        or value.outcome_evidence == ""
        or (value.disposition == "revised") != (value.resulting_scope is not None)
        or not value.reason
        or (
            value.evaluated_scope.scope_revision < value.target_scope.scope_revision
            or (
                value.evaluated_scope.scope_revision == value.target_scope.scope_revision
                and value.evaluated_scope.scope_digest != value.target_scope.scope_digest
            )
        )
        or (
            value.resulting_scope is not None
            and (
                value.resulting_scope.scope_revision != value.evaluated_scope.scope_revision + 1
                or value.resulting_scope.scope_digest == value.evaluated_scope.scope_digest
            )
        )
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "A resolved obligation contradicts its resolution facts.")
    return identities


def _valid_scope_anchor(value: ScopeAnchorView) -> bool:
    return (
        value.scope_revision >= 1
        and len(value.scope_digest) == 64
        and all(character in "0123456789abcdef" for character in value.scope_digest)
    )


def _validate_snapshot_obligations(value: PlanSnapshot, selected: frozenset[str]) -> None:
    obligations: tuple[PlanObligation, ...] = (*value.unresolved_obligations, *value.resolved_obligations)
    impact_owners: dict[str, PlanObligation] = {}
    impact_positions: dict[str, set[int]] = {}
    unresolved_identities = tuple(_obligation_identity(item) for item in value.unresolved_obligations)
    resolved_identities = tuple(_obligation_identity(item) for item in value.resolved_obligations)
    if (
        unresolved_identities != tuple(sorted(set(unresolved_identities)))
        or resolved_identities != tuple(sorted(set(resolved_identities)))
        or set(unresolved_identities).intersection(resolved_identities)
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Planning obligations are not canonical and unique.")
    for obligation in obligations:
        owner = impact_owners.setdefault(obligation.impact_id, obligation)
        positions = impact_positions.setdefault(obligation.impact_id, set())
        if (
            not obligation.impact_id
            or not obligation.source_item_id
            or obligation.source_attempt_id == ""
            or not obligation.target_item_id
            or not obligation.summary
            or not obligation.evidence
            or obligation.target_position < 0
            or obligation.recorded_project_revision < 1
            or obligation.recorded_project_revision > value.project_revision
            or not _valid_scope_anchor(obligation.source_scope)
            or not _valid_scope_anchor(obligation.target_scope)
            or obligation.target_position in positions
            or owner.source_item_id != obligation.source_item_id
            or owner.source_attempt_id != obligation.source_attempt_id
            or owner.source_scope != obligation.source_scope
            or owner.summary != obligation.summary
            or owner.evidence != obligation.evidence
            or owner.recorded_project_revision != obligation.recorded_project_revision
        ):
            raise PlanQueryError(
                "PLAN_SNAPSHOT_INVALID", "A planning obligation carries invalid identity or revision facts."
            )
        positions.add(obligation.target_position)
        endpoints = {obligation.source_item_id, obligation.target_item_id}
        if isinstance(obligation, ResolvedObligation):
            if not _valid_scope_anchor(obligation.evaluated_scope) or (
                obligation.resulting_scope is not None and not _valid_scope_anchor(obligation.resulting_scope)
            ):
                raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "A resolved obligation carries an invalid scope anchor.")
            endpoints.update(_validate_resolved_obligation(obligation, value.project_revision))
        if not endpoints.intersection(selected):
            raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "An obligation is unrelated to the selected plan.")
    for impact_id, positions in impact_positions.items():
        if impact_owners[impact_id].source_item_id in selected and positions != set(range(len(positions))):
            raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "A selected planning impact has incomplete target order.")
    expected_status = "unreconciled" if value.unresolved_obligations else "reconciled"
    if value.status != expected_status:
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan reconciliation status contradicts its obligations.")


def _proposal_preimage(value: UndecidedProposal) -> _UndecidedProposalPreimage:
    return _UndecidedProposalPreimage(
        value.proposal_id,
        value.source_task_id,
        value.user_label,
        value.trigger,
        value.why_it_matters,
        value.relation,
        value.effect,
        value.unlock,
        value.urgency_evidence,
        value.evidence,
        value.freshness_assumptions,
    )


def _validate_undecided_proposal(proposal: UndecidedProposal) -> None:
    evidence_positions = tuple(record.position for record in proposal.evidence)
    freshness_positions = tuple(record.position for record in proposal.freshness_assumptions)
    evidence_values = tuple(record.selector for record in proposal.evidence)
    freshness_values = tuple(record.assumption for record in proposal.freshness_assumptions)
    scalars = (
        proposal.proposal_id,
        proposal.source_task_id,
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        proposal.effect,
        proposal.unlock,
        proposal.urgency_evidence,
    )
    if (
        any(not scalar for scalar in scalars)
        or proposal.relation.kind not in {"independent", "prerequisite", "follow-up", "duplicate", "contradiction"}
        or proposal.relation.item_id == ""
        or evidence_positions != tuple(range(len(evidence_positions)))
        or freshness_positions != tuple(range(len(freshness_positions)))
        or len(evidence_values) != len(set(evidence_values))
        or len(freshness_values) != len(set(freshness_values))
        or any(not record.selector for record in proposal.evidence)
        or any(not record.assumption for record in proposal.freshness_assumptions)
        or hashlib.sha256(_canonical_bytes(_proposal_preimage(proposal))).hexdigest() != proposal.proposal_sha256
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "An undecided proposal is not canonical.")


def _validate_snapshot_undecided(value: PlanSnapshot) -> None:
    if not value.include_undecided and value.undecided:
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Undecided proposals require the explicit snapshot option.")
    if tuple(proposal.proposal_id for proposal in value.undecided) != tuple(
        sorted({proposal.proposal_id for proposal in value.undecided})
    ):
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Undecided proposals are not canonical and unique.")
    for proposal in value.undecided:
        _validate_undecided_proposal(proposal)


def _validate_snapshot(value: PlanSnapshot) -> None:
    _validate_snapshot_identity(value)
    scopes = _validated_snapshot_scopes(value)
    _validate_snapshot_obligations(
        value,
        frozenset(scopes),
    )
    _validate_snapshot_undecided(value)


def _anchor(item: PlanItem) -> ScopeAnchorView:
    return ScopeAnchorView(item.scope_digest, item.scope_revision)


def _semantic(value: PlanItem) -> ItemScopeRecord:
    try:
        return msgspec.json.decode(bytes(value.semantic), type=ItemScopeRecord)
    except (msgspec.DecodeError, ValueError) as error:  # pragma: no cover - snapshots are validated first
        raise PlanQueryError("PLAN_SNAPSHOT_INVALID", "Plan item semantic value is invalid.") from error


def _obligation_identity(value: UnresolvedObligation | ResolvedObligation) -> str:
    return f"{value.impact_id}:{value.target_item_id}"


def _change_set_preimage(value: PlanChangeSet) -> _PlanChangeSetPreimage:
    return _PlanChangeSetPreimage(
        value.schema,
        value.before_manifest_sha256,
        value.after_manifest_sha256,
        value.requested_roots,
        value.include_undecided,
        value.changes,
    )


@dataclass(frozen=True, slots=True)
class _ItemChangeGroups:
    added: tuple[ItemPresenceChange, ...]
    removed: tuple[ItemPresenceChange, ...]
    scope: tuple[ItemAnchorChange, ...]
    dependencies: tuple[ItemDependencyChange, ...]
    resources: tuple[ItemResourceChange, ...]
    artifacts: tuple[ItemArtifactChange, ...]
    lifecycle: tuple[ItemLifecycleChange, ...]


def _compare_plan_items(before: PlanSnapshot, after: PlanSnapshot) -> _ItemChangeGroups:
    before_items = {item.item_id: item for item in before.items}
    after_items = {item.item_id: item for item in after.items}
    added_ids = sorted(set(after_items) - set(before_items))
    removed_ids = sorted(set(before_items) - set(after_items))
    common_ids = sorted(set(before_items) & set(after_items))
    before_semantic = {item_id: _semantic(before_items[item_id]) for item_id in common_ids}
    after_semantic = {item_id: _semantic(after_items[item_id]) for item_id in common_ids}
    scope_changed = tuple(
        ItemAnchorChange(item_id, _anchor(before_items[item_id]), _anchor(after_items[item_id]))
        for item_id in common_ids
        if _anchor(before_items[item_id]) != _anchor(after_items[item_id])
    )
    dependency_changes = tuple(
        ItemDependencyChange(
            item_id,
            before_semantic[item_id].dependencies,
            after_semantic[item_id].dependencies,
        )
        for item_id in common_ids
        if before_semantic[item_id].dependencies != after_semantic[item_id].dependencies
    )
    resource_changes = tuple(
        ItemResourceChange(
            item_id,
            before_semantic[item_id].resource_requirements,
            after_semantic[item_id].resource_requirements,
        )
        for item_id in common_ids
        if before_semantic[item_id].resource_requirements != after_semantic[item_id].resource_requirements
    )
    artifact_changes = tuple(
        ItemArtifactChange(
            item_id,
            before_semantic[item_id].artifacts,
            after_semantic[item_id].artifacts,
        )
        for item_id in common_ids
        if before_semantic[item_id].artifacts != after_semantic[item_id].artifacts
    )
    lifecycle = tuple(
        ItemLifecycleChange(
            item_id,
            before_items[item_id].lifecycle_state,
            before_items[item_id].outcome_evidence,
            after_items[item_id].lifecycle_state,
            after_items[item_id].outcome_evidence,
        )
        for item_id in common_ids
        if (
            before_items[item_id].lifecycle_state,
            before_items[item_id].outcome_evidence,
        )
        != (
            after_items[item_id].lifecycle_state,
            after_items[item_id].outcome_evidence,
        )
        and before_items[item_id].scope_digest == after_items[item_id].scope_digest
    )
    return _ItemChangeGroups(
        tuple(ItemPresenceChange(item_id, _anchor(after_items[item_id])) for item_id in added_ids),
        tuple(ItemPresenceChange(item_id, _anchor(before_items[item_id])) for item_id in removed_ids),
        scope_changed,
        dependency_changes,
        resource_changes,
        artifact_changes,
        lifecycle,
    )


@dataclass(frozen=True, slots=True)
class _ObligationChangeGroups:
    replacements: tuple[ReplacementChange, ...] = ()
    entered: tuple[ObligationEnteredScope, ...] = ()
    left: tuple[ObligationLeftScope, ...] = ()
    opened: tuple[ObligationOpened, ...] = ()
    resolved: tuple[ObligationResolved, ...] = ()


def _obligation_endpoints(value: PlanObligation, revision: int) -> frozenset[str]:
    result = {value.source_item_id, value.target_item_id}
    if isinstance(value, ResolvedObligation) and value.resolved_project_revision <= revision:
        result.update(replacement.item_id for replacement in value.replacements)
    return frozenset(result)


def _common_obligation_facts(value: PlanObligation) -> tuple[str | int | ScopeAnchorView | None, ...]:
    return (
        value.impact_id,
        value.source_item_id,
        value.source_attempt_id,
        value.source_scope,
        value.target_item_id,
        value.target_position,
        value.target_scope,
        value.summary,
        value.evidence,
        value.recorded_project_revision,
    )


def _resolution_changes(
    value: ResolvedObligation,
    previous: UnresolvedObligation | None,
) -> _ObligationChangeGroups:
    replacement = (
        (
            ReplacementChange(
                "added",
                value.impact_id,
                value.target_item_id,
                value.evaluated_scope,
                value.replacements,
                value.resolved_project_revision,
            ),
        )
        if value.disposition == "superseded"
        else ()
    )
    return _ObligationChangeGroups(replacements=replacement, resolved=(ObligationResolved(previous, value),))


def _compare_obligation_pair(
    previous: PlanObligation | None,
    current: PlanObligation | None,
    before: PlanSnapshot,
    after: PlanSnapshot,
    before_selected: frozenset[str],
    after_selected: frozenset[str],
) -> _ObligationChangeGroups:
    if previous is None:
        assert current is not None
        before_phase = _obligation_endpoints(current, before.project_revision)
        if current.recorded_project_revision <= before.project_revision and before_phase.intersection(before_selected):
            raise PlanQueryError(
                "PLAN_SNAPSHOT_CONTRADICTION",
                "A previously relevant planning obligation is absent from the earlier manifest.",
            )
        opened = (ObligationOpened(current),) if current.recorded_project_revision > before.project_revision else ()
        entered = (
            (ObligationEnteredScope(current),)
            if not before_phase.intersection(before_selected)
            and _obligation_endpoints(current, after.project_revision).intersection(after_selected)
            else ()
        )
        resolution = (
            _resolution_changes(current, None)
            if isinstance(current, ResolvedObligation) and current.resolved_project_revision > before.project_revision
            else _ObligationChangeGroups()
        )
        return _ObligationChangeGroups(resolution.replacements, entered, (), opened, resolution.resolved)
    if current is None:
        if _obligation_endpoints(previous, before.project_revision).intersection(after_selected):
            raise PlanQueryError(
                "PLAN_SNAPSHOT_CONTRADICTION",
                "A still-relevant planning obligation disappeared from the later manifest.",
            )
        return _ObligationChangeGroups(left=(ObligationLeftScope(previous),))
    if isinstance(previous, UnresolvedObligation) and isinstance(current, ResolvedObligation):
        if current.resolved_project_revision <= before.project_revision:
            raise PlanQueryError(
                "PLAN_SNAPSHOT_CONTRADICTION",
                "A planning obligation resolution is backdated into the earlier unresolved manifest.",
            )
        if _common_obligation_facts(previous) != _common_obligation_facts(current):
            raise PlanQueryError(
                "PLAN_SNAPSHOT_CONTRADICTION",
                "A planning obligation changed immutable facts while resolving.",
            )
        return _resolution_changes(current, previous)
    if (
        isinstance(previous, UnresolvedObligation) and isinstance(current, UnresolvedObligation) and previous == current
    ) or (isinstance(previous, ResolvedObligation) and isinstance(current, ResolvedObligation) and previous == current):
        return _ObligationChangeGroups()
    raise PlanQueryError("PLAN_SNAPSHOT_CONTRADICTION", "A planning obligation changed immutable phase facts.")


def _compare_plan_obligations(before: PlanSnapshot, after: PlanSnapshot) -> _ObligationChangeGroups:
    before_values: dict[str, PlanObligation] = {
        _obligation_identity(value): value for value in (*before.unresolved_obligations, *before.resolved_obligations)
    }
    after_values: dict[str, PlanObligation] = {
        _obligation_identity(value): value for value in (*after.unresolved_obligations, *after.resolved_obligations)
    }
    before_selected = frozenset(item.item_id for item in before.items)
    after_selected = frozenset(item.item_id for item in after.items)
    groups = [
        _compare_obligation_pair(
            before_values.get(identity),
            after_values.get(identity),
            before,
            after,
            before_selected,
            after_selected,
        )
        for identity in sorted(set(before_values) | set(after_values))
    ]
    return _ObligationChangeGroups(
        tuple(change for group in groups for change in group.replacements),
        tuple(change for group in groups for change in group.entered),
        tuple(change for group in groups for change in group.left),
        tuple(change for group in groups for change in group.opened),
        tuple(change for group in groups for change in group.resolved),
    )


def _compare_undecided(before: PlanSnapshot, after: PlanSnapshot) -> tuple[UndecidedChange, ...]:
    before_values = {value.proposal_id: value for value in before.undecided}
    after_values = {value.proposal_id: value for value in after.undecided}
    changes: list[UndecidedChange] = []
    for proposal_id in sorted(set(before_values) | set(after_values)):
        previous = before_values.get(proposal_id)
        current = after_values.get(proposal_id)
        if previous is not None and current is not None and previous.proposal_sha256 == current.proposal_sha256:
            continue
        change: Literal["added", "removed", "changed"] = (
            "added" if previous is None else "removed" if current is None else "changed"
        )
        changes.append(
            UndecidedChange(
                change,
                proposal_id,
                previous.proposal_sha256 if previous is not None else None,
                current.proposal_sha256 if current is not None else None,
            )
        )
    return tuple(changes)


def compare_plan_snapshots(before: PlanSnapshot, after: PlanSnapshot) -> PlanChangeSet:
    _validate_snapshot(before)
    _validate_snapshot(after)
    if before.requested_roots != after.requested_roots or before.include_undecided != after.include_undecided:
        raise PlanQueryError("PLAN_SELECTION_MISMATCH", "Plan snapshots use different roots or undecided options.")
    if before.project_revision > after.project_revision:
        raise PlanQueryError("PLAN_COMPARISON_DIRECTION_INVALID", "Plan snapshots are ordered from newer to older.")
    if before.project_revision == after.project_revision and before.manifest_sha256 != after.manifest_sha256:
        raise PlanQueryError("PLAN_SNAPSHOT_CONTRADICTION", "Equal revisions carry different plan manifests.")
    items = _compare_plan_items(before, after)
    obligations = _compare_plan_obligations(before, after)
    changes = PlanChanges(
        items.added,
        items.removed,
        items.scope,
        items.dependencies,
        items.resources,
        items.artifacts,
        items.lifecycle,
        obligations.replacements,
        obligations.entered,
        obligations.left,
        obligations.opened,
        obligations.resolved,
        _compare_undecided(before, after),
    )
    draft = PlanChangeSet(
        "plan-change-set/v1",
        before.manifest_sha256,
        after.manifest_sha256,
        before.requested_roots,
        before.include_undecided,
        changes,
        "",
    )
    return msgspec.structs.replace(
        draft,
        change_set_sha256=hashlib.sha256(_canonical_bytes(_change_set_preimage(draft))).hexdigest(),
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
