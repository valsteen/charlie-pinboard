import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal, assert_never

import msgspec

from charlie_pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    HistoryRecordError,
    HistoryRecordErrorCode,
)
from charlie_pinboard.domain.work_models import (
    ArtifactRole,
    ItemScope,
    ScopeArtifact,
    ScopeDependency,
)

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type NonNegativeInteger = Annotated[int, msgspec.Meta(ge=0)]
type PositiveInteger = Annotated[int, msgspec.Meta(ge=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]
type SemanticArtifactRole = Literal["design", "plan", "requirements"]


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
            raise HistoryRecordError(
                HistoryRecordErrorCode.ARTIFACT_KIND_ROLE_MISMATCH,
                "Semantic artifact kind must equal its role.",
            )
        if not _canonical_selector(self.selector):
            raise HistoryRecordError(
                HistoryRecordErrorCode.ARTIFACT_SELECTOR_INVALID,
                "Artifact selector must be a canonical relative POSIX path.",
            )


class ItemScopeRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifacts: tuple[ScopeArtifactRecord, ...]
    dependencies: tuple[ScopeDependencyRecord, ...]
    effect: NonEmptyString | None
    item_id: NonEmptyString
    schema: Literal["item-scope/v2"]
    trigger: NonEmptyString | None
    unlock: NonEmptyString | None
    user_label: NonEmptyString
    why_it_matters: NonEmptyString | None

    def __post_init__(self) -> None:
        dependency_positions = tuple(value.position for value in self.dependencies)
        dependency_ids = tuple(value.dependency_id for value in self.dependencies)
        if not _canonical_positions(dependency_positions, dependency_ids):
            raise HistoryRecordError(
                HistoryRecordErrorCode.DEPENDENCIES_INVALID,
                "Dependency positions or identities are not canonical.",
            )
        artifact_positions: dict[SemanticArtifactRole, list[int]] = {}
        artifact_identities: set[tuple[str, str, int]] = set()
        artifact_order: list[tuple[str, int, str, str, int]] = []
        for artifact in self.artifacts:
            artifact_positions.setdefault(artifact.role, []).append(artifact.position)
            identity = (artifact.kind, artifact.key, artifact.revision)
            if identity in artifact_identities:
                raise HistoryRecordError(
                    HistoryRecordErrorCode.ARTIFACT_IDENTITY_DUPLICATE,
                    "Semantic artifact identity is duplicated.",
                )
            artifact_identities.add(identity)
            artifact_order.append((artifact.role, artifact.position, artifact.kind, artifact.key, artifact.revision))
        if any(positions != list(range(len(positions))) for positions in artifact_positions.values()):
            raise HistoryRecordError(
                HistoryRecordErrorCode.ARTIFACT_ROLE_POSITIONS_INVALID,
                "Artifact role positions are not canonical.",
            )
        if artifact_order != sorted(artifact_order):
            raise HistoryRecordError(
                HistoryRecordErrorCode.ARTIFACT_ORDER_INVALID,
                "Artifact order is not canonical.",
            )


class TransitionReceiptOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    evidence: str | None
    outcome: str | None
    candidate: str | None = None
    checkpoint: str | None = None


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
        "item-scope/v2",
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
