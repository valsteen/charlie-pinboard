from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.domain.authority_decisions import InactiveAttemptAuthority
from charlie_pinboard.domain.decisions import AttemptAuthorityChange, AttemptChange, ItemChange
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import AttemptId, ItemId, PlanningImpactId
from charlie_pinboard.domain.model import (
    AttemptState,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    ItemScope,
    LedgerSnapshot,
    MutationIntentState,
    PlanningDisposition,
    PlanningImpact,
    ReservationState,
    ResourceReservation,
    ScopeAnchor,
    UseLeaseState,
    WorkState,
)
from charlie_pinboard.domain.resource_decisions import (
    ReservationChange,
    ResourceUseLeaseChange,
    current_authorizing_grant,
)


@dataclass(frozen=True, slots=True)
class RecordPlanningImpactOperation:
    impact: PlanningImpact
    authority: CommandAttemptAuthority | CoordinationCommandAuthority
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningImpactDecision:
    impact: PlanningImpact
    source_authority: CommandAttemptAuthority | CoordinationCommandAuthority


@dataclass(frozen=True, slots=True)
class NoAttemptPlanningAuthority:
    item: ItemId


@dataclass(frozen=True, slots=True)
class LivePlanningAttemptAuthority:
    authority: CommandAttemptAuthority


@dataclass(frozen=True, slots=True)
class InterruptedPlanningAttemptAuthority:
    authority: InactiveAttemptAuthority


type PlanningTargetAuthority = (
    NoAttemptPlanningAuthority | LivePlanningAttemptAuthority | InterruptedPlanningAttemptAuthority
)


@dataclass(frozen=True, slots=True)
class UnchangedPlanningDisposition:
    reason: str


@dataclass(frozen=True, slots=True)
class RevisedPlanningDisposition:
    reason: str
    next_scope: ItemScope


@dataclass(frozen=True, slots=True)
class BlockedPlanningDisposition:
    reason: str


@dataclass(frozen=True, slots=True)
class DeferredPlanningDisposition:
    reason: str


@dataclass(frozen=True, slots=True)
class DroppedPlanningDisposition:
    reason: str
    outcome_evidence: str


@dataclass(frozen=True, slots=True)
class SupersededPlanningDisposition:
    reason: str
    outcome_evidence: str
    replacements: tuple[ItemId, ...]


type PlanningDispositionInput = (
    UnchangedPlanningDisposition
    | RevisedPlanningDisposition
    | BlockedPlanningDisposition
    | DeferredPlanningDisposition
    | DroppedPlanningDisposition
    | SupersededPlanningDisposition
)


@dataclass(frozen=True, slots=True)
class ResolvePlanningObligationOperation:
    impact_id: PlanningImpactId
    target: ItemId
    disposition: PlanningDispositionInput
    coordination_authority: CoordinationCommandAuthority
    target_authority: PlanningTargetAuthority
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningResolutionDecision:
    impact: PlanningImpact
    item_change: ItemChange | None
    attempt_change: AttemptChange | None
    target_authority: PlanningTargetAuthority | None = None
    scope_change: ScopeAnchor | None = None
    attempt_authority_change: AttemptAuthorityChange | None = None
    reservation_changes: tuple[ReservationChange, ...] = ()
    resource_use_lease_changes: tuple[ResourceUseLeaseChange, ...] = ()


def advance_scope(previous: ScopeAnchor | None, item: ItemId, scope: ItemScope) -> ScopeAnchor | DecisionFailure:
    if scope.item_id != item:
        return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, "Scope item ID must match its owning item.")
    result = item_scope_digest(scope)
    match result:
        case DecisionFailure():
            return result
        case digest:
            pass
    if previous is not None and previous.digest == digest:
        return previous
    return ScopeAnchor(item, 1 if previous is None else previous.revision + 1, digest, scope)


def _validate_impact_scopes(snapshot: LedgerSnapshot, impact: PlanningImpact) -> DecisionFailure | None:
    live = snapshot.items_by_id()
    if impact.source_scope_revision < 1 or not impact.source_scope_digest:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Planning impact source scope must be an exact anchor."
        )
    if not impact.summary or not impact.evidence:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Planning impact summary and evidence must be nonempty."
        )
    if not impact.obligations:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Planning impacts require at least one explicit target."
        )
    positions = [value.position for value in impact.obligations]
    targets = [value.target for value in impact.obligations]
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Planning impact target positions must be gapless."
        )
    if any(target in snapshot.history_items for target in targets):
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_TARGET_TERMINAL,
            "Planning impact targets must not be terminal.",
        )
    if len(targets) != len(set(targets)) or any(target not in live for target in targets):
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Planning impact targets must be unique live items."
        )
    if any(value.observed_scope_revision < 1 or not value.observed_scope_digest for value in impact.obligations):
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID, "Every target requires an exact observed scope anchor."
        )
    scopes = {value.item: value for value in snapshot.scopes}
    source_scope = scopes.get(impact.source_item)
    if source_scope is not None and (source_scope.revision, source_scope.digest) != (
        impact.source_scope_revision,
        impact.source_scope_digest,
    ):
        return DecisionFailure(
            DecisionFailureCode.PLANNING_ACTION_STALE, "Planning impact source scope changed before recording."
        )
    for obligation in impact.obligations:
        target_scope = scopes.get(obligation.target)
        if target_scope is not None and (target_scope.revision, target_scope.digest) != (
            obligation.observed_scope_revision,
            obligation.observed_scope_digest,
        ):
            return DecisionFailure(
                DecisionFailureCode.PLANNING_ACTION_STALE,
                f"Target '{obligation.target}' scope changed before recording.",
            )
    return None


def validate_planning_impact(snapshot: LedgerSnapshot, impact: PlanningImpact) -> DecisionFailure | None:
    live = snapshot.items_by_id()
    if impact.source_item not in live:
        if impact.source_item in snapshot.history_items:
            return DecisionFailure(
                DecisionFailureCode.PLANNING_IMPACT_SOURCE_TERMINAL,
                "Planning impact source must not be terminal.",
            )
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID,
            "Planning impact source must be a live item.",
        )
    if impact.source_attempt is not None:
        attempt = snapshot.attempts_by_id().get(impact.source_attempt)
        if (
            attempt is None
            or attempt.item != impact.source_item
            or attempt.state
            in {
                AttemptState.DONE,
                AttemptState.CLOSED,
            }
        ):
            return DecisionFailure(
                DecisionFailureCode.PLANNING_IMPACT_INVALID,
                "Planning impact source attempt must be live and owned by the source item.",
            )
    return _validate_impact_scopes(snapshot, impact)


def decide_planning_impact(
    snapshot: LedgerSnapshot,
    operation: RecordPlanningImpactOperation,
) -> PlanningImpactDecision | DecisionFailure:
    impact = operation.impact
    if (failure := validate_planning_impact(snapshot, impact)) is not None:
        return failure
    match operation.authority:
        case CommandAttemptAuthority() as authority:
            if (
                authority not in snapshot.command_attempt_authorities
                or authority.expires_at <= operation.recorded_at
                or impact.source_attempt != authority.attempt
                or impact.source_item != authority.item
            ):
                return DecisionFailure(
                    DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                    "Planning impact recording requires the exact live source-attempt authority.",
                )
        case CoordinationCommandAuthority() as authority:
            if (
                snapshot.coordination_authority != authority
                or authority.expires_at <= operation.recorded_at
                or impact.source_attempt is not None
            ):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "Item-only planning impact recording requires exact live coordination authority.",
                )
    return PlanningImpactDecision(impact, operation.authority)


def _validate_resolution(
    evaluated_scope_revision: int,
    evaluated_scope_digest: str,
    disposition: PlanningDisposition,
    resulting_scope_revision: int | None,
    resulting_scope_digest: str | None,
    replacements: tuple[ItemId, ...],
    outcome_evidence: str | None,
) -> DecisionFailure | None:
    if disposition == PlanningDisposition.REVISED:
        if (
            resulting_scope_revision != evaluated_scope_revision + 1
            or not resulting_scope_digest
            or resulting_scope_digest == evaluated_scope_digest
        ):
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "Revised disposition requires a newer target scope."
            )
    elif resulting_scope_revision is not None or resulting_scope_digest is not None:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
            "Only revised disposition accepts a resulting scope anchor.",
        )
    if disposition == PlanningDisposition.SUPERSEDED:
        if not replacements or len(replacements) != len(set(replacements)):
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
                "Superseded disposition requires ordered unique replacements.",
            )
    elif replacements:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "Only superseded disposition accepts replacements."
        )
    if disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        if outcome_evidence is None or not outcome_evidence.strip():
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "Terminal disposition requires outcome evidence."
            )
    elif outcome_evidence is not None:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "Nonterminal disposition cannot carry outcome evidence."
        )
    return None


def resolve_planning_obligation(
    snapshot: LedgerSnapshot,
    impact: PlanningImpact,
    target: ItemId,
    disposition: PlanningDisposition,
    *,
    reason: str,
    resulting_scope_revision: int | None = None,
    resulting_scope_digest: str | None = None,
    replacements: tuple[ItemId, ...] = (),
    outcome_evidence: str | None = None,
) -> PlanningImpact | DecisionFailure:
    if (failure := validate_planning_impact(snapshot, impact)) is not None:
        return failure
    if not reason:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
            "Resolution reason must be nonempty.",
        )
    index = next((position for position, value in enumerate(impact.obligations) if value.target == target), None)
    if index is None:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_OBLIGATION_NOT_FOUND, f"Target '{target}' is not part of the impact."
        )
    current = impact.obligations[index]
    if current.disposition is not None:
        return DecisionFailure(DecisionFailureCode.PLANNING_ACTION_STALE, f"Target '{target}' is already reconciled.")
    evaluated_scope = next((value for value in snapshot.scopes if value.item == target), None)
    evaluated_scope_revision = current.observed_scope_revision if evaluated_scope is None else evaluated_scope.revision
    evaluated_scope_digest = current.observed_scope_digest if evaluated_scope is None else evaluated_scope.digest
    if (
        failure := _validate_resolution(
            evaluated_scope_revision,
            evaluated_scope_digest,
            disposition,
            resulting_scope_revision,
            resulting_scope_digest,
            replacements,
            outcome_evidence,
        )
    ) is not None:
        return failure
    obligations = list(impact.obligations)
    obligations[index] = replace(
        current,
        disposition=disposition,
        evaluated_scope_revision=evaluated_scope_revision,
        evaluated_scope_digest=evaluated_scope_digest,
        resulting_scope_revision=resulting_scope_revision,
        resulting_scope_digest=resulting_scope_digest,
        replacements=replacements,
        outcome_evidence=outcome_evidence,
        reason=reason,
    )
    return replace(impact, obligations=tuple(obligations))


def decide_planning_resolution(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    impact: PlanningImpact,
    target: ItemId,
    disposition: PlanningDisposition,
    *,
    reason: str,
    resulting_scope_revision: int | None = None,
    resulting_scope_digest: str | None = None,
    replacements: tuple[ItemId, ...] = (),
    outcome_evidence: str | None = None,
) -> PlanningResolutionDecision | DecisionFailure:
    item = snapshot.item(target)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{target}' does not exist.")
    if disposition == PlanningDisposition.DEFERRED and item.attempt is not None:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
            "A target with a retained attempt must be blocked, not deferred.",
        )
    if disposition == PlanningDisposition.BLOCKED:
        if item.attempt is None:
            legal_block = item.state in {WorkState.INTAKE, WorkState.READY}
        else:
            attempt = snapshot.attempt(item.attempt)
            legal_block = (
                item.state in {WorkState.ACTIVE, WorkState.REVIEW}
                and attempt is not None
                and attempt.state in {AttemptState.ACTIVE, AttemptState.REVIEW}
            )
        if not legal_block:
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
                "The target cannot be blocked from its current lifecycle state.",
            )
    if disposition == PlanningDisposition.SUPERSEDED:
        live = snapshot.items_by_id()
        if any(replacement not in live or replacement == target for replacement in replacements):
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "Replacements must be distinct live items."
            )
    result = resolve_planning_obligation(
        snapshot,
        impact,
        target,
        disposition,
        reason=reason,
        resulting_scope_revision=resulting_scope_revision,
        resulting_scope_digest=resulting_scope_digest,
        replacements=replacements,
        outcome_evidence=outcome_evidence,
    )
    match result:
        case DecisionFailure():
            return result
        case updated:
            pass
    item_change: ItemChange | None = None
    attempt_change: AttemptChange | None = None
    if disposition == PlanningDisposition.BLOCKED:
        item_change = ItemChange(item.item, item.state, WorkState.BLOCKED, item.attempt)
        if item.attempt is not None:
            attempt = snapshot.attempts_by_id().get(item.attempt)
            attempt_change = AttemptChange(
                item.attempt,
                None if attempt is None else attempt.state,
                AttemptState.BLOCKED,
            )
    elif disposition == PlanningDisposition.DEFERRED:
        if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED}:
            return DecisionFailure(
                DecisionFailureCode.PLANNING_RESOLUTION_INVALID, "The target cannot be deferred from its current state."
            )
        item_change = ItemChange(item.item, item.state, WorkState.DEFERRED)
    elif disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        item_change = ItemChange(item.item, item.state, None, item.attempt, outcome_evidence)
        if item.attempt is not None:
            attempt = snapshot.attempts_by_id().get(item.attempt)
            attempt_change = AttemptChange(
                item.attempt,
                None if attempt is None else attempt.state,
                AttemptState.CLOSED,
            )
    return PlanningResolutionDecision(updated, item_change, attempt_change)


def _planning_authority_failure(  # noqa: C901
    snapshot: LedgerSnapshot,
    item: ItemId,
    authority: PlanningTargetAuthority,
    expected_authority: PlanningTargetAuthority,
    now: datetime,
) -> DecisionFailure | None:
    work_item = snapshot.item(item)
    if work_item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item}' does not exist.")
    if authority != expected_authority:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Planning authority no longer matches the exact locked target proof.",
        )
    if work_item.attempt is None:
        if authority != NoAttemptPlanningAuthority(item):
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                "Planning resolution requires proof that the target has no retained attempt.",
            )
        return None
    attempt = snapshot.attempt(work_item.attempt)
    if attempt is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "The target attempt is missing.")
    if any(
        value.attempt_id == work_item.attempt and value.state == MutationIntentState.PLANNED
        for value in snapshot.mutation_intents
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED,
            "Planning resolution requires every target mutation intent to be resolved.",
        )
    if work_item.state in {WorkState.ACTIVE, WorkState.REVIEW}:
        if isinstance(authority, InterruptedPlanningAttemptAuthority):
            return None
        if not isinstance(authority, LivePlanningAttemptAuthority):
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                "Active or review planning resolution requires exact live attempt authority.",
            )
        supplied = authority.authority
        if (
            supplied.attempt != work_item.attempt
            or supplied.item != item
            or supplied.expires_at <= now
            or supplied not in snapshot.command_attempt_authorities
        ):
            return DecisionFailure(
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
                "The target attempt authority is stale or cross-wired.",
            )
        return None
    if not isinstance(authority, InterruptedPlanningAttemptAuthority):
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Interrupted planning resolution requires exact inactive attempt proof.",
        )
    if any(
        value.attempt_id == work_item.attempt and value.state == ReservationState.REVOKED_PENDING_RECOVERY
        for value in snapshot.mutation_reservations
    ):
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Inactive attempt proof no longer matches recovered retained authority.",
        )
    return None


def _reservation_key(value: ResourceReservation) -> str:
    return str(value.reservation_id)


def _planning_terminal_effects(
    snapshot: LedgerSnapshot,
    attempt_id: AttemptId | None,
) -> tuple[
    AttemptAuthorityChange | None,
    tuple[ReservationChange, ...],
    tuple[ResourceUseLeaseChange, ...],
]:
    if attempt_id is None:
        return None, (), ()
    reservations = tuple(
        sorted(
            (
                value
                for value in snapshot.resource_reservations
                if value.attempt == attempt_id and value.state == ReservationState.ACTIVE
            ),
            key=_reservation_key,
        )
    )
    reservation_changes = tuple(
        ReservationChange(value, replace(value, state=ReservationState.RELEASED)) for value in reservations
    )
    use_changes = tuple(
        ResourceUseLeaseChange(use, replace(use, state=UseLeaseState.RELEASED))
        for reservation in reservations
        if (use := current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)) is not None
    )
    authority = next((value for value in snapshot.attempt_authorities if value.attempt == attempt_id), None)
    authority_change = (
        None
        if authority is None
        else AttemptAuthorityChange(
            authority,
            replace(authority, lease_id=None, generation=authority.generation + 1, resources=()),
        )
    )
    return authority_change, reservation_changes, use_changes


def decide_planning_obligation_operation(  # noqa: C901, PLR0912, PLR0915
    snapshot: LedgerSnapshot,
    operation: ResolvePlanningObligationOperation,
    *,
    expected_target_authority: PlanningTargetAuthority,
) -> PlanningResolutionDecision | DecisionFailure:
    if (
        snapshot.coordination_authority != operation.coordination_authority
        or operation.coordination_authority.expires_at <= operation.resolved_at
    ):
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Planning resolution requires exact live coordination authority.",
        )
    impact = next((value for value in snapshot.planning_impacts if value.impact_id == operation.impact_id), None)
    if impact is None:
        return DecisionFailure(
            DecisionFailureCode.PLANNING_IMPACT_INVALID,
            f"Planning impact '{operation.impact_id}' does not exist.",
        )
    if (
        failure := _planning_authority_failure(
            snapshot,
            operation.target,
            operation.target_authority,
            expected_target_authority,
            operation.resolved_at,
        )
    ) is not None:
        return failure
    scope_change: ScopeAnchor | None = None
    match operation.disposition:
        case UnchangedPlanningDisposition(reason=reason):
            disposition = PlanningDisposition.UNCHANGED
            resulting_revision = None
            resulting_digest = None
            replacements = ()
            evidence = None
        case RevisedPlanningDisposition(reason=reason, next_scope=next_scope):
            disposition = PlanningDisposition.REVISED
            current = next((value for value in snapshot.scopes if value.item == operation.target), None)
            result = advance_scope(current, operation.target, next_scope)
            if isinstance(result, DecisionFailure):
                return result
            if current is not None and result == current:
                return DecisionFailure(
                    DecisionFailureCode.PLANNING_RESOLUTION_INVALID,
                    "Revised planning disposition must change semantic scope.",
                )
            scope_change = result
            resulting_revision = result.revision
            resulting_digest = result.digest
            replacements = ()
            evidence = None
        case BlockedPlanningDisposition(reason=reason):
            disposition = PlanningDisposition.BLOCKED
            resulting_revision = None
            resulting_digest = None
            replacements = ()
            evidence = None
        case DeferredPlanningDisposition(reason=reason):
            disposition = PlanningDisposition.DEFERRED
            resulting_revision = None
            resulting_digest = None
            replacements = ()
            evidence = None
        case DroppedPlanningDisposition(reason=reason, outcome_evidence=evidence):
            disposition = PlanningDisposition.DROPPED
            resulting_revision = None
            resulting_digest = None
            replacements = ()
        case SupersededPlanningDisposition(reason=reason, outcome_evidence=evidence, replacements=replacements):
            disposition = PlanningDisposition.SUPERSEDED
            resulting_revision = None
            resulting_digest = None
        case _ as unreachable:
            assert_never(unreachable)
    result = decide_planning_resolution(
        snapshot,
        impact,
        operation.target,
        disposition,
        reason=reason,
        resulting_scope_revision=resulting_revision,
        resulting_scope_digest=resulting_digest,
        replacements=replacements,
        outcome_evidence=evidence,
    )
    if isinstance(result, DecisionFailure):
        return result
    item = snapshot.item(operation.target)
    assert item is not None
    authority_change: AttemptAuthorityChange | None = None
    reservation_changes: tuple[ReservationChange, ...] = ()
    use_changes: tuple[ResourceUseLeaseChange, ...] = ()
    if disposition == PlanningDisposition.BLOCKED and item.attempt is not None:
        authority = next((value for value in snapshot.attempt_authorities if value.attempt == item.attempt), None)
        authority_change = (
            None
            if authority is None
            else AttemptAuthorityChange(
                authority,
                replace(authority, lease_id=None, generation=authority.generation + 1, resources=()),
            )
        )
        use_changes = tuple(
            ResourceUseLeaseChange(use, replace(use, state=UseLeaseState.REVOKED))
            for reservation in snapshot.resource_reservations
            if reservation.attempt == item.attempt
            if (use := current_authorizing_grant(snapshot.resource_use_leases, reservation.reservation_id)) is not None
        )
    elif disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        authority_change, reservation_changes, use_changes = _planning_terminal_effects(snapshot, item.attempt)
    return replace(
        result,
        target_authority=operation.target_authority,
        scope_change=scope_change,
        attempt_authority_change=authority_change,
        reservation_changes=reservation_changes,
        resource_use_lease_changes=use_changes,
    )
