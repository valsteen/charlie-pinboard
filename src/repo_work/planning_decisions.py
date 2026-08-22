from dataclasses import dataclass, replace

from repo_work.decisions import AttemptChange, ItemChange
from repo_work.domain_errors import DecisionError, DecisionErrorCode
from repo_work.history import item_scope_digest
from repo_work.identifiers import ItemId
from repo_work.model import (
    AttemptState,
    ItemScope,
    LedgerSnapshot,
    PlanningDisposition,
    PlanningImpact,
    ScopeAnchor,
    WorkItem,
    WorkState,
)


@dataclass(frozen=True, slots=True)
class PlanningResolutionDecision:
    impact: PlanningImpact
    item_change: ItemChange | None
    attempt_change: AttemptChange | None


def advance_scope(previous: ScopeAnchor | None, item: ItemId, scope: ItemScope) -> ScopeAnchor:
    if scope.item_id != item:
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Scope item ID must match its owning item.")
    digest = item_scope_digest(scope)
    if previous is not None and previous.digest == digest:
        return previous
    return ScopeAnchor(item, 1 if previous is None else previous.revision + 1, digest, scope)


def _validate_impact_scopes(snapshot: LedgerSnapshot, impact: PlanningImpact) -> None:
    live = snapshot.items_by_id()
    if impact.source_scope_revision < 1 or not impact.source_scope_digest:
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact source scope must be an exact anchor."
        )
    if not impact.summary or not impact.evidence:
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact summary and evidence must be nonempty."
        )
    if not impact.obligations:
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impacts require at least one explicit target."
        )
    positions = [value.position for value in impact.obligations]
    targets = [value.target for value in impact.obligations]
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact target positions must be gapless."
        )
    if len(targets) != len(set(targets)) or any(target not in live for target in targets):
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact targets must be unique live items."
        )
    if any(value.observed_scope_revision < 1 or not value.observed_scope_digest for value in impact.obligations):
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_INVALID, "Every target requires an exact observed scope anchor."
        )
    scopes = {value.item: value for value in snapshot.scopes}
    source_scope = scopes.get(impact.source_item)
    if source_scope is not None and (source_scope.revision, source_scope.digest) != (
        impact.source_scope_revision,
        impact.source_scope_digest,
    ):
        raise DecisionError(
            DecisionErrorCode.PLANNING_ACTION_STALE, "Planning impact source scope changed before recording."
        )
    for obligation in impact.obligations:
        target_scope = scopes.get(obligation.target)
        if target_scope is not None and (target_scope.revision, target_scope.digest) != (
            obligation.observed_scope_revision,
            obligation.observed_scope_digest,
        ):
            raise DecisionError(
                DecisionErrorCode.PLANNING_ACTION_STALE, f"Target '{obligation.target}' scope changed before recording."
            )


def validate_planning_impact(snapshot: LedgerSnapshot, impact: PlanningImpact) -> None:
    live = snapshot.items_by_id()
    if impact.source_item not in live:
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact source must be a live item.")
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
            raise DecisionError(
                DecisionErrorCode.PLANNING_IMPACT_INVALID,
                "Planning impact source attempt must be live and owned by the source item.",
            )
    _validate_impact_scopes(snapshot, impact)


def _validate_resolution(
    evaluated_scope_revision: int,
    evaluated_scope_digest: str,
    disposition: PlanningDisposition,
    resulting_scope_revision: int | None,
    resulting_scope_digest: str | None,
    replacements: tuple[ItemId, ...],
    outcome_evidence: str | None,
) -> None:
    if disposition == PlanningDisposition.REVISED:
        if (
            resulting_scope_revision != evaluated_scope_revision + 1
            or not resulting_scope_digest
            or resulting_scope_digest == evaluated_scope_digest
        ):
            raise DecisionError(
                DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Revised disposition requires a newer target scope."
            )
    elif resulting_scope_revision is not None or resulting_scope_digest is not None:
        raise DecisionError(
            DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Only revised disposition accepts a resulting scope anchor."
        )
    if disposition == PlanningDisposition.SUPERSEDED:
        if not replacements or len(replacements) != len(set(replacements)):
            raise DecisionError(
                DecisionErrorCode.PLANNING_RESOLUTION_INVALID,
                "Superseded disposition requires ordered unique replacements.",
            )
    elif replacements:
        raise DecisionError(
            DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Only superseded disposition accepts replacements."
        )
    if disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        if outcome_evidence is None or not outcome_evidence.strip():
            raise DecisionError(
                DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Terminal disposition requires outcome evidence."
            )
    elif outcome_evidence is not None:
        raise DecisionError(
            DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Nonterminal disposition cannot carry outcome evidence."
        )


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
) -> PlanningImpact:
    validate_planning_impact(snapshot, impact)
    if not reason:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Resolution reason must be nonempty.")
    index = next((position for position, value in enumerate(impact.obligations) if value.target == target), None)
    if index is None:
        raise DecisionError(
            DecisionErrorCode.PLANNING_OBLIGATION_NOT_FOUND, f"Target '{target}' is not part of the impact."
        )
    current = impact.obligations[index]
    if current.disposition is not None:
        raise DecisionError(DecisionErrorCode.PLANNING_ACTION_STALE, f"Target '{target}' is already reconciled.")
    evaluated_scope = next((value for value in snapshot.scopes if value.item == target), None)
    evaluated_scope_revision = current.observed_scope_revision if evaluated_scope is None else evaluated_scope.revision
    evaluated_scope_digest = current.observed_scope_digest if evaluated_scope is None else evaluated_scope.digest
    _validate_resolution(
        evaluated_scope_revision,
        evaluated_scope_digest,
        disposition,
        resulting_scope_revision,
        resulting_scope_digest,
        replacements,
        outcome_evidence,
    )
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


def _item(snapshot: LedgerSnapshot, item_id: ItemId) -> WorkItem:
    item = snapshot.items_by_id().get(item_id)
    if item is None:
        raise DecisionError(DecisionErrorCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    return item


def decide_planning_resolution(
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
) -> PlanningResolutionDecision:
    item = _item(snapshot, target)
    if disposition == PlanningDisposition.DEFERRED and item.attempt is not None:
        raise DecisionError(
            DecisionErrorCode.PLANNING_RESOLUTION_INVALID,
            "A target with a retained attempt must be blocked, not deferred.",
        )
    if disposition == PlanningDisposition.SUPERSEDED:
        live = snapshot.items_by_id()
        if any(replacement not in live or replacement == target for replacement in replacements):
            raise DecisionError(
                DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Replacements must be distinct live items."
            )
    updated = resolve_planning_obligation(
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
            raise DecisionError(
                DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "The target cannot be deferred from its current state."
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
