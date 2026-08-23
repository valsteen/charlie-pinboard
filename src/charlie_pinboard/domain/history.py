import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal, assert_never

import msgspec

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import (
    ArtifactRole,
    ItemScope,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    ResourceRequirement,
    ScopeAnchor,
    ScopeArtifact,
    ScopeDependency,
)

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type NonNegativeInteger = Annotated[int, msgspec.Meta(ge=0)]
type PositiveInteger = Annotated[int, msgspec.Meta(ge=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]
type SemanticArtifactRole = Literal["design", "plan", "requirements"]


class HistoryOutcomeError(ValueError):
    code = DecisionFailureCode.HISTORY_OUTCOME_INVALID

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code.value}: {message}")


def _encoded_record(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


def _canonical_positions(positions: tuple[int, ...], identities: tuple[str, ...]) -> bool:
    return positions == tuple(range(len(positions))) and len(identities) == len(set(identities))


def _canonical_selector(selector: str) -> bool:
    parts = selector.split("/")
    return not selector.startswith("/") and all(part not in {"", ".", ".."} for part in parts)


class ScopeDependencyRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: NonEmptyString
    position: NonNegativeInteger


class ScopeResourceRequirementRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    position: NonNegativeInteger
    resource_id: NonEmptyString


class ScopeArtifactRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    content_sha256: Sha256
    key: NonEmptyString
    kind: SemanticArtifactRole
    position: NonNegativeInteger
    revision: PositiveInteger
    role: SemanticArtifactRole
    selector: NonEmptyString

    def __post_init__(self) -> None:
        if self.kind != self.role:
            raise ValueError("Semantic artifact kind must equal its role.")
        if not _canonical_selector(self.selector):
            raise ValueError("Artifact selector must be a canonical relative POSIX path.")


class ItemScopeRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifacts: tuple[ScopeArtifactRecord, ...]
    dependencies: tuple[ScopeDependencyRecord, ...]
    effect: NonEmptyString | None
    item_id: NonEmptyString
    resource_requirements: tuple[ScopeResourceRequirementRecord, ...]
    schema: Literal["item-scope/v1"]
    trigger: NonEmptyString | None
    unlock: NonEmptyString | None
    user_label: NonEmptyString
    why_it_matters: NonEmptyString | None

    def __post_init__(self) -> None:
        dependency_positions = tuple(value.position for value in self.dependencies)
        dependency_ids = tuple(value.dependency_id for value in self.dependencies)
        if not _canonical_positions(dependency_positions, dependency_ids):
            raise ValueError("Dependency positions or identities are not canonical.")
        requirement_positions = tuple(value.position for value in self.resource_requirements)
        resource_ids = tuple(value.resource_id for value in self.resource_requirements)
        if not _canonical_positions(requirement_positions, resource_ids):
            raise ValueError("Resource requirement positions or identities are not canonical.")
        artifact_positions: dict[SemanticArtifactRole, list[int]] = {}
        artifact_identities: set[tuple[str, str, int]] = set()
        artifact_order: list[tuple[str, int, str, str, int]] = []
        for artifact in self.artifacts:
            artifact_positions.setdefault(artifact.role, []).append(artifact.position)
            identity = (artifact.kind, artifact.key, artifact.revision)
            if identity in artifact_identities:
                raise ValueError("Semantic artifact identity is duplicated.")
            artifact_identities.add(identity)
            artifact_order.append((artifact.role, artifact.position, artifact.kind, artifact.key, artifact.revision))
        if any(positions != list(range(len(positions))) for positions in artifact_positions.values()):
            raise ValueError("Artifact role positions are not canonical.")
        if artifact_order != sorted(artifact_order):
            raise ValueError("Artifact order is not canonical.")


class ScopeAnchorRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    scope_digest: Sha256
    scope_revision: PositiveInteger


class ScopeSnapshotRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    scope_digest: Sha256
    scope_revision: PositiveInteger
    semantic: ItemScopeRecord

    def __post_init__(self) -> None:
        computed = hashlib.sha256(_encoded_record(self.semantic)).hexdigest()
        if self.scope_digest != computed:
            raise ValueError("Scope digest does not match its semantic value.")


class ItemScopeChangeOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    after: ScopeSnapshotRecord
    before: ScopeSnapshotRecord | None
    item_id: NonEmptyString

    def __post_init__(self) -> None:
        if self.item_id != self.after.semantic.item_id:
            raise ValueError("Scope outcome item IDs do not match.")
        if self.before is None:
            if self.after.scope_revision != 1:
                raise ValueError("Initial scope revision must be one.")
            return
        if (
            self.before.semantic.item_id != self.item_id
            or self.before.scope_revision + 1 != self.after.scope_revision
            or self.before.scope_digest == self.after.scope_digest
        ):
            raise ValueError("Scope outcome anchors are not a semantic change.")


class PlanningImpactSourceRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: NonEmptyString | None
    item_id: NonEmptyString
    scope: ScopeAnchorRecord


class PlanningImpactTargetRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: NonEmptyString
    position: NonNegativeInteger
    scope: ScopeAnchorRecord


class PlanningImpactOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyString
    impact_id: NonEmptyString
    source: PlanningImpactSourceRecord
    summary: NonEmptyString
    targets: tuple[PlanningImpactTargetRecord, ...]

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("Planning impact targets cannot be empty.")
        positions = tuple(value.position for value in self.targets)
        identities = tuple(value.item_id for value in self.targets)
        if not _canonical_positions(positions, identities):
            raise ValueError("Planning target positions or identities are not canonical.")


class PlanningReplacementRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: NonEmptyString
    position: NonNegativeInteger


class PlanningResolutionOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    disposition: PlanningDisposition
    evaluated_scope: ScopeAnchorRecord
    impact_id: NonEmptyString
    observed_scope: ScopeAnchorRecord
    outcome_evidence: NonEmptyString | None
    reason: NonEmptyString
    replacements: tuple[PlanningReplacementRecord, ...]
    resulting_scope: ScopeAnchorRecord | None
    target_item_id: NonEmptyString

    def __post_init__(self) -> None:
        positions = tuple(value.position for value in self.replacements)
        identities = tuple(value.item_id for value in self.replacements)
        if not _canonical_positions(positions, identities):
            raise ValueError("Planning replacement positions or identities are not canonical.")
        if self.disposition == PlanningDisposition.REVISED:
            if self.resulting_scope is None:
                raise ValueError("Revised resolution requires a resulting scope.")
            if (
                self.resulting_scope.scope_revision != self.evaluated_scope.scope_revision + 1
                or self.resulting_scope.scope_digest == self.evaluated_scope.scope_digest
            ):
                raise ValueError("Revised scope anchor is invalid.")
        elif self.resulting_scope is not None:
            raise ValueError("Only revised resolution may carry a resulting scope.")
        terminal = self.disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}
        if terminal != (self.outcome_evidence is not None):
            raise ValueError("Terminal outcome evidence does not match disposition.")
        if (self.disposition == PlanningDisposition.SUPERSEDED) != bool(self.replacements):
            raise ValueError("Replacement records do not match disposition.")


class TransitionReceiptOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    evidence: str | None
    outcome: str | None
    candidate: str | None = None
    checkpoint: str | None = None


type DecodedHistoryOutcome = ItemScopeChangeOutcome | PlanningImpactOutcome | PlanningResolutionOutcome


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    outcome_schema: str
    payload: bytes


def encode_transition_receipt_outcome(
    *,
    evidence: str | None,
    outcome: str | None,
    candidate: str | None = None,
    checkpoint: str | None = None,
) -> bytes:
    return msgspec.json.encode(
        TransitionReceiptOutcome(evidence, outcome, candidate, checkpoint),
        order="sorted",
    )


def decode_history_outcome(outcome_schema: str, payload: bytes) -> DecodedHistoryOutcome:
    if not payload.endswith(b"\n"):
        raise HistoryOutcomeError("Outcome JSON requires one final LF.")
    try:
        decoded: DecodedHistoryOutcome
        match outcome_schema:
            case "item-scope-change/v1":
                decoded = msgspec.json.decode(payload, type=ItemScopeChangeOutcome)
            case "planning-impact/v1":
                decoded = msgspec.json.decode(payload, type=PlanningImpactOutcome)
            case "planning-impact-resolution/v1":
                decoded = msgspec.json.decode(payload, type=PlanningResolutionOutcome)
            case _:
                raise HistoryOutcomeError(f"Unsupported outcome schema '{outcome_schema}'.")
    except msgspec.DecodeError as error:
        raise HistoryOutcomeError(f"Cannot decode history outcome: {error}") from error
    if _encoded_record(decoded) != payload:
        raise HistoryOutcomeError("Outcome JSON is not canonical.")
    return decoded


def _nonempty(value: str, field: str) -> DecisionFailure | None:
    if not value:
        return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty.")
    return None


def _positioned(positions: list[int], identities: list[str], field: str) -> DecisionFailure | None:
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        return DecisionFailure(
            DecisionFailureCode.ITEM_SCOPE_INVALID,
            f"{field} positions must be zero-based and gapless.",
        )
    if len(identities) != len(set(identities)) or any(not value for value in identities):
        return DecisionFailure(
            DecisionFailureCode.ITEM_SCOPE_INVALID,
            f"{field} identities must be unique and nonempty.",
        )
    return None


def _artifact_sort_key(artifact: ScopeArtifact) -> tuple[str, int, str, str, int]:
    return (artifact.role.value, artifact.position, artifact.kind, artifact.key, artifact.revision)


def _dependency_sort_key(value: ScopeDependency) -> tuple[int, str]:
    return (value.position, value.dependency_id)


def _requirement_sort_key(value: ResourceRequirement) -> tuple[int, str]:
    return (value.position, value.resource_id)


def _obligation_sort_key(value: PlanningObligation) -> tuple[int, str]:
    return (value.position, value.target)


def _semantic_role(role: ArtifactRole) -> SemanticArtifactRole | None:
    match role:
        case ArtifactRole.DESIGN:
            return "design"
        case ArtifactRole.PLAN:
            return "plan"
        case ArtifactRole.REQUIREMENTS:
            return "requirements"
        case ArtifactRole.EVIDENCE:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _semantic_artifacts(  # noqa: C901, PLR0912
    scope: ItemScope,
) -> tuple[ScopeArtifactRecord, ...] | DecisionFailure:
    semantic_roles = {ArtifactRole.REQUIREMENTS, ArtifactRole.PLAN, ArtifactRole.DESIGN}
    artifacts = tuple(value for value in scope.artifacts if value.role in semantic_roles)
    identities: set[tuple[str, str, int]] = set()
    role_positions: dict[ArtifactRole, list[int]] = {}
    for artifact in artifacts:
        if artifact.position < 0:
            return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, "Artifact positions must be non-negative.")
        if artifact.kind != artifact.role.value:
            return DecisionFailure(
                DecisionFailureCode.ITEM_SCOPE_INVALID,
                "Semantic artifact kind must equal its role.",
            )
        if artifact.revision < 1:
            return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, "Artifact revisions must be positive.")
        for field, value in (
            ("artifact key", artifact.key),
            ("artifact selector", artifact.selector),
            ("artifact content digest", artifact.content_sha256),
        ):
            if (failure := _nonempty(value, field)) is not None:
                return failure
        if len(artifact.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in artifact.content_sha256
        ):
            return DecisionFailure(
                DecisionFailureCode.ITEM_SCOPE_INVALID, "Artifact content digest must be lowercase SHA-256."
            )
        if not _canonical_selector(artifact.selector):
            return DecisionFailure(
                DecisionFailureCode.ITEM_SCOPE_INVALID, "Artifact selector must be a canonical relative POSIX path."
            )
        identity = (artifact.kind, artifact.key, artifact.revision)
        if identity in identities:
            return DecisionFailure(
                DecisionFailureCode.ITEM_SCOPE_INVALID,
                "Semantic artifact identities must be unique.",
            )
        identities.add(identity)
        role_positions.setdefault(artifact.role, []).append(artifact.position)
    for role, positions in role_positions.items():
        ordered = sorted(positions)
        if ordered != list(range(len(ordered))) or len(ordered) != len(set(ordered)):
            return DecisionFailure(
                DecisionFailureCode.ITEM_SCOPE_INVALID, f"Artifact positions for role '{role.value}' must be gapless."
            )
    records: list[ScopeArtifactRecord] = []
    for artifact in sorted(artifacts, key=_artifact_sort_key):
        role = _semantic_role(artifact.role)
        if role is None:
            continue
        records.append(
            ScopeArtifactRecord(
                artifact.content_sha256,
                artifact.key,
                role,
                artifact.position,
                artifact.revision,
                role,
                artifact.selector,
            )
        )
    return tuple(records)


def _item_scope_record(scope: ItemScope) -> ItemScopeRecord | DecisionFailure:
    for field, value in (("item ID", scope.item_id), ("user label", scope.user_label)):
        if (failure := _nonempty(value, field)) is not None:
            return failure
    for field, value in (
        ("trigger", scope.trigger),
        ("why it matters", scope.why_it_matters),
        ("effect", scope.effect),
        ("unlock", scope.unlock),
    ):
        if value == "":
            return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty or null.")
    if (
        failure := _positioned(
            [value.position for value in scope.dependencies],
            [value.dependency_id for value in scope.dependencies],
            "Dependency",
        )
    ) is not None:
        return failure
    if (
        failure := _positioned(
            [value.position for value in scope.resource_requirements],
            [value.resource_id for value in scope.resource_requirements],
            "Resource requirement",
        )
    ) is not None:
        return failure
    result = _semantic_artifacts(scope)
    match result:
        case DecisionFailure():
            return result
        case artifacts:
            pass
    return ItemScopeRecord(
        artifacts,
        tuple(
            ScopeDependencyRecord(dependency.dependency_id, dependency.position)
            for dependency in sorted(scope.dependencies, key=_dependency_sort_key)
        ),
        scope.effect,
        scope.item_id,
        tuple(
            ScopeResourceRequirementRecord(requirement.position, requirement.resource_id)
            for requirement in sorted(scope.resource_requirements, key=_requirement_sort_key)
        ),
        "item-scope/v1",
        scope.trigger,
        scope.unlock,
        scope.user_label,
        scope.why_it_matters,
    )


def item_scope_bytes(scope: ItemScope) -> bytes | DecisionFailure:
    result = _item_scope_record(scope)
    match result:
        case DecisionFailure():
            return result
        case record:
            return _encoded_record(record)


def item_scope_digest(scope: ItemScope) -> str | DecisionFailure:
    result = item_scope_bytes(scope)
    match result:
        case DecisionFailure():
            return result
        case payload:
            return hashlib.sha256(payload).hexdigest()


def _anchor_record(revision: int, digest: str) -> ScopeAnchorRecord | DecisionFailure:
    if revision < 1 or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID,
            "Scope anchors require a positive revision and lowercase SHA-256.",
        )
    return ScopeAnchorRecord(digest, revision)


def _scope_snapshot_record(anchor: ScopeAnchor) -> ScopeSnapshotRecord | DecisionFailure:
    result = _item_scope_record(anchor.scope)
    match result:
        case DecisionFailure():
            return result
        case semantic:
            return ScopeSnapshotRecord(anchor.digest, anchor.revision, semantic)


def item_scope_change_outcome(  # noqa: PLR0912
    before: ScopeAnchor | None,
    after: ScopeAnchor,
) -> HistoryOutcome | DecisionFailure:
    result = item_scope_digest(after.scope)
    match result:
        case DecisionFailure():
            return result
        case after_digest:
            pass
    if after.digest != after_digest:
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "After scope digest does not match its semantic value."
        )
    if before is not None:
        if before.item != after.item or before.revision + 1 != after.revision or before.digest == after.digest:
            return DecisionFailure(
                DecisionFailureCode.HISTORY_OUTCOME_INVALID,
                "Scope changes require consecutive unequal anchors for one item.",
            )
        result = item_scope_digest(before.scope)
        match result:
            case DecisionFailure():
                return result
            case before_digest:
                pass
        if before.digest != before_digest:
            return DecisionFailure(
                DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Before scope digest does not match its semantic value."
            )
    elif after.revision != 1:
        return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, "An initial scope starts at revision one.")
    result = _scope_snapshot_record(after)
    match result:
        case DecisionFailure():
            return result
        case after_record:
            pass
    before_record: ScopeSnapshotRecord | None = None
    if before is not None:
        result = _scope_snapshot_record(before)
        match result:
            case DecisionFailure():
                return result
            case record:
                before_record = record
    record = ItemScopeChangeOutcome(after_record, before_record, after.item)
    return HistoryOutcome("item-scope-change/v1", _encoded_record(record))


def planning_impact_outcome(impact: PlanningImpact) -> HistoryOutcome | DecisionFailure:
    for field, value in (("impact ID", impact.impact_id), ("summary", impact.summary), ("evidence", impact.evidence)):
        if not value:
            return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, f"{field} must be nonempty.")
    if impact.source_attempt == "":
        return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Source attempt must be nonempty or null.")
    if not impact.obligations:
        return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Planning impact targets cannot be empty.")
    if (
        failure := _positioned(
            [value.position for value in impact.obligations],
            [value.target for value in impact.obligations],
            "Planning target",
        )
    ) is not None:
        return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, failure.message)
    targets: list[PlanningImpactTargetRecord] = []
    for obligation in sorted(impact.obligations, key=_obligation_sort_key):
        result = _anchor_record(obligation.observed_scope_revision, obligation.observed_scope_digest)
        match result:
            case DecisionFailure():
                return result
            case scope:
                targets.append(PlanningImpactTargetRecord(obligation.target, obligation.position, scope))
    result = _anchor_record(impact.source_scope_revision, impact.source_scope_digest)
    match result:
        case DecisionFailure():
            return result
        case source_scope:
            pass
    record = PlanningImpactOutcome(
        impact.evidence,
        impact.impact_id,
        PlanningImpactSourceRecord(impact.source_attempt, impact.source_item, source_scope),
        impact.summary,
        tuple(targets),
    )
    return HistoryOutcome("planning-impact/v1", _encoded_record(record))


def planning_resolution_outcome(  # noqa: C901, PLR0912
    impact: PlanningImpact,
    target: ItemId,
) -> HistoryOutcome | DecisionFailure:
    obligation = next((value for value in impact.obligations if value.target == target), None)
    if obligation is None or obligation.disposition is None or obligation.reason is None:
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Planning resolution must name one resolved obligation."
        )
    if obligation.evaluated_scope_revision is None or obligation.evaluated_scope_digest is None:
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Planning resolution requires its evaluated scope."
        )
    if not impact.impact_id or not target or not obligation.reason:
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Planning resolution identities and reason must be nonempty."
        )
    resulting_scope: ScopeAnchorRecord | None = None
    if obligation.resulting_scope_revision is not None or obligation.resulting_scope_digest is not None:
        if obligation.resulting_scope_revision is None or obligation.resulting_scope_digest is None:
            return DecisionFailure(
                DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Resulting scope anchor must be complete."
            )
        result = _anchor_record(obligation.resulting_scope_revision, obligation.resulting_scope_digest)
        match result:
            case DecisionFailure():
                return result
            case scope:
                resulting_scope = scope
    result = _anchor_record(obligation.evaluated_scope_revision, obligation.evaluated_scope_digest)
    match result:
        case DecisionFailure():
            return result
        case evaluated_scope:
            pass
    result = _anchor_record(obligation.observed_scope_revision, obligation.observed_scope_digest)
    match result:
        case DecisionFailure():
            return result
        case observed_scope:
            pass
    if len(obligation.replacements) != len(set(obligation.replacements)) or any(
        not value for value in obligation.replacements
    ):
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Planning replacement identities must be unique and nonempty."
        )
    replacements = tuple(
        PlanningReplacementRecord(item_id, position) for position, item_id in enumerate(obligation.replacements)
    )
    disposition = obligation.disposition
    if disposition == PlanningDisposition.REVISED:
        if resulting_scope is None:
            return DecisionFailure(
                DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Revised resolution requires a resulting scope."
            )
        if (
            resulting_scope.scope_revision != evaluated_scope.scope_revision + 1
            or resulting_scope.scope_digest == evaluated_scope.scope_digest
        ):
            return DecisionFailure(DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Revised scope anchor is invalid.")
    elif resulting_scope is not None:
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Only revised resolution may carry a resulting scope."
        )
    terminal = disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}
    if terminal != (obligation.outcome_evidence is not None):
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Terminal outcome evidence does not match disposition."
        )
    if (disposition == PlanningDisposition.SUPERSEDED) != bool(replacements):
        return DecisionFailure(
            DecisionFailureCode.HISTORY_OUTCOME_INVALID, "Replacement records do not match disposition."
        )
    record = PlanningResolutionOutcome(
        disposition,
        evaluated_scope,
        impact.impact_id,
        observed_scope,
        obligation.outcome_evidence,
        obligation.reason,
        replacements,
        resulting_scope,
        target,
    )
    return HistoryOutcome("planning-impact-resolution/v1", _encoded_record(record))
