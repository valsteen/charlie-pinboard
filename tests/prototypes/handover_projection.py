"""Deferred tool-neutral handover projection prototype.

This module preserves design evidence for the deferred handover-export item. It is
test-owned, excluded from the installed package, and is not a supported Pinboard
entry point. Move behavior into ``src`` only with an accepted product brief and a
real command-line consumer.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal

import msgspec

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.queries import QueryError
from charlie_pinboard.application.stored_state import (
    ItemDependency,
    PlanningObligationState,
    ProposalEvidence,
    ProposalFreshness,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
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
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import PlanningDisposition


class PlanQueryError(QueryError):
    pass


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


def _dependency_position(value: ItemDependency) -> int:
    return value.position


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _obligation_key(value: StoredPlanningObligation) -> tuple[str, str]:
    return str(value.impact_id), str(value.target_item_id)


def _replacement_position(value: StoredPlanningReplacement) -> int:
    return value.position


def _proposal_evidence_position(value: ProposalEvidence) -> int:
    return value.position


def _proposal_freshness_position(value: ProposalFreshness) -> int:
    return value.position


def _canonical_bytes(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


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
