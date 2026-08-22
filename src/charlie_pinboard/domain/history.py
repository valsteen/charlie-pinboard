import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast, overload

from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
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

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    outcome_schema: str
    payload: bytes


def _nonempty(value: str, field: str) -> None:
    if not value:
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty.")


def _positioned(positions: list[int], identities: list[str], field: str) -> None:
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} positions must be zero-based and gapless.")
    if len(identities) != len(set(identities)) or any(not value for value in identities):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} identities must be unique and nonempty.")


def _semantic_artifacts(scope: ItemScope) -> tuple[dict[str, int | str], ...]:
    semantic_roles = {ArtifactRole.REQUIREMENTS, ArtifactRole.PLAN, ArtifactRole.DESIGN}
    artifacts = tuple(value for value in scope.artifacts if value.role in semantic_roles)
    identities: set[tuple[str, str, int]] = set()
    role_positions: dict[ArtifactRole, list[int]] = {}
    for artifact in artifacts:
        if artifact.position < 0:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact positions must be non-negative.")
        if artifact.kind != artifact.role.value:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Semantic artifact kind must equal its role.")
        if artifact.revision < 1:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact revisions must be positive.")
        for field, value in (
            ("artifact key", artifact.key),
            ("artifact selector", artifact.selector),
            ("artifact content digest", artifact.content_sha256),
        ):
            _nonempty(value, field)
        if len(artifact.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in artifact.content_sha256
        ):
            raise DecisionError(
                DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact content digest must be lowercase SHA-256."
            )
        selector_parts = artifact.selector.split("/")
        if artifact.selector.startswith("/") or any(part in {"", ".", ".."} for part in selector_parts):
            raise DecisionError(
                DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact selector must be a canonical relative POSIX path."
            )
        identity = (artifact.kind, artifact.key, artifact.revision)
        if identity in identities:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Semantic artifact identities must be unique.")
        identities.add(identity)
        role_positions.setdefault(artifact.role, []).append(artifact.position)
    for role, positions in role_positions.items():
        ordered = sorted(positions)
        if ordered != list(range(len(ordered))) or len(ordered) != len(set(ordered)):
            raise DecisionError(
                DecisionErrorCode.ITEM_SCOPE_INVALID, f"Artifact positions for role '{role.value}' must be gapless."
            )
    return tuple(
        {
            "content_sha256": artifact.content_sha256,
            "key": artifact.key,
            "kind": artifact.kind,
            "position": artifact.position,
            "revision": artifact.revision,
            "role": artifact.role.value,
            "selector": artifact.selector,
        }
        for artifact in sorted(artifacts, key=_artifact_sort_key)
    )


def _artifact_sort_key(artifact: ScopeArtifact) -> tuple[str, int, str, str, int]:
    return (artifact.role.value, artifact.position, artifact.kind, artifact.key, artifact.revision)


def _dependency_sort_key(value: ScopeDependency) -> tuple[int, str]:
    return (value.position, value.dependency_id)


def _requirement_sort_key(value: ResourceRequirement) -> tuple[int, str]:
    return (value.position, value.resource_id)


def _obligation_sort_key(value: PlanningObligation) -> tuple[int, str]:
    return (value.position, value.target)


def item_scope_bytes(scope: ItemScope) -> bytes:
    _nonempty(scope.item_id, "item ID")
    _nonempty(scope.user_label, "user label")
    for field, value in (
        ("trigger", scope.trigger),
        ("why it matters", scope.why_it_matters),
        ("effect", scope.effect),
        ("unlock", scope.unlock),
    ):
        if value == "":
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty or null.")
    _positioned(
        [value.position for value in scope.dependencies],
        [value.dependency_id for value in scope.dependencies],
        "Dependency",
    )
    _positioned(
        [value.position for value in scope.resource_requirements],
        [value.resource_id for value in scope.resource_requirements],
        "Resource requirement",
    )
    value = {
        "artifacts": _semantic_artifacts(scope),
        "dependencies": tuple(
            {"dependency_id": dependency.dependency_id, "position": dependency.position}
            for dependency in sorted(scope.dependencies, key=_dependency_sort_key)
        ),
        "effect": scope.effect,
        "item_id": scope.item_id,
        "resource_requirements": tuple(
            {"position": requirement.position, "resource_id": requirement.resource_id}
            for requirement in sorted(
                scope.resource_requirements,
                key=_requirement_sort_key,
            )
        ),
        "schema": "item-scope/v1",
        "trigger": scope.trigger,
        "unlock": scope.unlock,
        "user_label": scope.user_label,
        "why_it_matters": scope.why_it_matters,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def item_scope_digest(scope: ItemScope) -> str:
    return hashlib.sha256(item_scope_bytes(scope)).hexdigest()


def _anchor_value(revision: int, digest: str) -> dict[str, JsonValue]:
    if revision < 1 or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID,
            "Scope anchors require a positive revision and lowercase SHA-256.",
        )
    return {"scope_digest": digest, "scope_revision": revision}


def _scope_snapshot_value(anchor: ScopeAnchor) -> dict[str, JsonValue]:
    semantic = cast(JsonValue, json.loads(item_scope_bytes(anchor.scope)))
    return {
        "scope_digest": anchor.digest,
        "scope_revision": anchor.revision,
        "semantic": semantic,
    }


def _history_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def item_scope_change_outcome(before: ScopeAnchor | None, after: ScopeAnchor) -> HistoryOutcome:
    if after.digest != item_scope_digest(after.scope):
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "After scope digest does not match its semantic value."
        )
    if before is not None:
        if before.item != after.item or before.revision + 1 != after.revision or before.digest == after.digest:
            raise DecisionError(
                DecisionErrorCode.HISTORY_OUTCOME_INVALID,
                "Scope changes require consecutive unequal anchors for one item.",
            )
        if before.digest != item_scope_digest(before.scope):
            raise DecisionError(
                DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Before scope digest does not match its semantic value."
            )
    elif after.revision != 1:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "An initial scope starts at revision one.")
    payload: dict[str, JsonValue] = {
        "after": _scope_snapshot_value(after),
        "before": None if before is None else _scope_snapshot_value(before),
        "item_id": after.item,
    }
    outcome = HistoryOutcome("item-scope-change/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def planning_impact_outcome(impact: PlanningImpact) -> HistoryOutcome:
    targets: list[JsonValue] = [
        {
            "item_id": obligation.target,
            "position": obligation.position,
            "scope": _anchor_value(obligation.observed_scope_revision, obligation.observed_scope_digest),
        }
        for obligation in sorted(impact.obligations, key=_obligation_sort_key)
    ]
    payload: dict[str, JsonValue] = {
        "evidence": impact.evidence,
        "impact_id": impact.impact_id,
        "source": {
            "attempt_id": impact.source_attempt,
            "item_id": impact.source_item,
            "scope": _anchor_value(impact.source_scope_revision, impact.source_scope_digest),
        },
        "summary": impact.summary,
        "targets": targets,
    }
    outcome = HistoryOutcome("planning-impact/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def planning_resolution_outcome(impact: PlanningImpact, target: ItemId) -> HistoryOutcome:
    obligation = next((value for value in impact.obligations if value.target == target), None)
    if obligation is None or obligation.disposition is None or obligation.reason is None:
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning resolution must name one resolved obligation."
        )
    if obligation.evaluated_scope_revision is None or obligation.evaluated_scope_digest is None:
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning resolution requires its evaluated scope."
        )
    resulting_scope = None
    if obligation.resulting_scope_revision is not None and obligation.resulting_scope_digest is not None:
        resulting_scope = _anchor_value(obligation.resulting_scope_revision, obligation.resulting_scope_digest)
    replacements: list[JsonValue] = [
        {"item_id": item_id, "position": position} for position, item_id in enumerate(obligation.replacements)
    ]
    payload: dict[str, JsonValue] = {
        "disposition": obligation.disposition.value,
        "evaluated_scope": _anchor_value(
            obligation.evaluated_scope_revision,
            obligation.evaluated_scope_digest,
        ),
        "impact_id": impact.impact_id,
        "observed_scope": _anchor_value(obligation.observed_scope_revision, obligation.observed_scope_digest),
        "outcome_evidence": obligation.outcome_evidence,
        "reason": obligation.reason,
        "replacements": replacements,
        "resulting_scope": resulting_scope,
        "target_item_id": target,
    }
    outcome = HistoryOutcome("planning-impact-resolution/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def _outcome_mapping(value: JsonValue, keys: frozenset[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome records must be JSON objects.")
    result = value
    if set(result) != keys or any(not isinstance(key, str) for key in result):
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome record members do not match the schema."
        )
    return result


def _outcome_array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome collection must be a JSON array.")
    return value


@overload
def _outcome_string(value: JsonValue, *, nullable: Literal[False] = False) -> str: ...


@overload
def _outcome_string(value: JsonValue, *, nullable: Literal[True]) -> str | None: ...


def _outcome_string(value: JsonValue, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome string must be nonempty.")
    return value


def _outcome_integer(value: JsonValue, *, positive: bool = False) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (positive and value < 1)
        or (not positive and value < 0)
    ):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome integer has an invalid type or range.")
    return value


def _validate_anchor_record(value: JsonValue) -> tuple[int, str]:
    record = _outcome_mapping(value, frozenset({"scope_revision", "scope_digest"}))
    revision = _outcome_integer(record["scope_revision"], positive=True)
    digest = _outcome_string(record["scope_digest"])
    if digest is None or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope digest must be lowercase SHA-256.")
    return revision, digest


def _validate_positioned_records(
    records: list[dict[str, JsonValue]],
    *,
    identity: Callable[[dict[str, JsonValue]], str],
) -> None:
    positions = [_outcome_integer(record["position"]) for record in records]
    identities = [identity(record) for record in records]
    if positions != list(range(len(records))) or len(identities) != len(set(identities)):
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome positions or identities are not canonical."
        )


def _validate_semantic_scope(value: JsonValue) -> tuple[str, str]:
    keys = frozenset(
        {
            "schema",
            "item_id",
            "user_label",
            "trigger",
            "why_it_matters",
            "effect",
            "unlock",
            "dependencies",
            "resource_requirements",
            "artifacts",
        }
    )
    record = _outcome_mapping(value, keys)
    if record["schema"] != "item-scope/v1":
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic scope schema is not item-scope/v1.")
    item_id = _outcome_string(record["item_id"])
    _outcome_string(record["user_label"])
    for field in ("trigger", "why_it_matters", "effect", "unlock"):
        _outcome_string(record[field], nullable=True)
    dependencies = [
        _outcome_mapping(value, frozenset({"position", "dependency_id"}))
        for value in _outcome_array(record["dependencies"])
    ]
    _validate_positioned_records(dependencies, identity=lambda value: _outcome_string(value["dependency_id"]) or "")
    requirements = [
        _outcome_mapping(value, frozenset({"position", "resource_id"}))
        for value in _outcome_array(record["resource_requirements"])
    ]
    _validate_positioned_records(requirements, identity=lambda value: _outcome_string(value["resource_id"]) or "")
    artifacts = [
        _outcome_mapping(
            value,
            frozenset({"role", "position", "kind", "key", "revision", "selector", "content_sha256"}),
        )
        for value in _outcome_array(record["artifacts"])
    ]
    artifact_positions: dict[str, list[int]] = {}
    artifact_identities: set[tuple[str, str, int]] = set()
    artifact_order: list[tuple[str, int, str, str, int]] = []
    for artifact in artifacts:
        role = _outcome_string(artifact["role"])
        kind = _outcome_string(artifact["kind"])
        key = _outcome_string(artifact["key"])
        revision = _outcome_integer(artifact["revision"], positive=True)
        position = _outcome_integer(artifact["position"])
        selector = _outcome_string(artifact["selector"])
        digest = _outcome_string(artifact["content_sha256"])
        if role not in {"requirements", "plan", "design"} or kind != role or selector is None or digest is None:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic artifact identity is invalid.")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact digest must be lowercase SHA-256.")
        artifact_positions.setdefault(role, []).append(position)
        identity = (kind, key, revision)
        if identity in artifact_identities:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic artifact identity is duplicated.")
        artifact_identities.add(identity)
        artifact_order.append((role, position, kind, key, revision))
    if any(positions != list(range(len(positions))) for positions in artifact_positions.values()):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact role positions are not canonical.")
    if artifact_order != sorted(artifact_order):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact order is not canonical.")
    if item_id is None:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope item ID is missing.")
    digest = hashlib.sha256(_history_bytes(record)).hexdigest()
    return item_id, digest


def _validate_scope_snapshot(value: JsonValue) -> tuple[str, int, str]:
    record = _outcome_mapping(value, frozenset({"scope_revision", "scope_digest", "semantic"}))
    revision, digest = _validate_anchor_record(
        {"scope_revision": record["scope_revision"], "scope_digest": record["scope_digest"]}
    )
    item_id, computed = _validate_semantic_scope(record["semantic"])
    if digest != computed:
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope digest does not match its semantic value."
        )
    return item_id, revision, digest


def _validate_scope_change(value: dict[str, JsonValue]) -> None:
    record = _outcome_mapping(value, frozenset({"item_id", "before", "after"}))
    item_id = _outcome_string(record["item_id"])
    after_item, after_revision, after_digest = _validate_scope_snapshot(record["after"])
    if item_id != after_item:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope outcome item IDs do not match.")
    if record["before"] is None:
        if after_revision != 1:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Initial scope revision must be one.")
        return
    before_item, before_revision, before_digest = _validate_scope_snapshot(record["before"])
    if before_item != item_id or before_revision + 1 != after_revision or before_digest == after_digest:
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope outcome anchors are not a semantic change."
        )


def _validate_planning_impact_outcome(value: dict[str, JsonValue]) -> None:
    record = _outcome_mapping(value, frozenset({"impact_id", "source", "summary", "evidence", "targets"}))
    _outcome_string(record["impact_id"])
    _outcome_string(record["summary"])
    _outcome_string(record["evidence"])
    source = _outcome_mapping(record["source"], frozenset({"item_id", "attempt_id", "scope"}))
    _outcome_string(source["item_id"])
    _outcome_string(source["attempt_id"], nullable=True)
    _validate_anchor_record(source["scope"])
    targets = [
        _outcome_mapping(value, frozenset({"item_id", "position", "scope"}))
        for value in _outcome_array(record["targets"])
    ]
    if not targets:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning impact targets cannot be empty.")
    for target in targets:
        _validate_anchor_record(target["scope"])
    _validate_positioned_records(targets, identity=lambda value: _outcome_string(value["item_id"]) or "")


def _validate_planning_resolution_outcome(value: dict[str, JsonValue]) -> None:
    keys = frozenset(
        {
            "impact_id",
            "target_item_id",
            "observed_scope",
            "evaluated_scope",
            "resulting_scope",
            "disposition",
            "reason",
            "outcome_evidence",
            "replacements",
        }
    )
    record = _outcome_mapping(value, keys)
    _outcome_string(record["impact_id"])
    _outcome_string(record["target_item_id"])
    _outcome_string(record["reason"])
    _validate_anchor_record(record["observed_scope"])
    evaluated_revision, evaluated_digest = _validate_anchor_record(record["evaluated_scope"])
    disposition_text = _outcome_string(record["disposition"])
    try:
        disposition = PlanningDisposition(disposition_text)
    except ValueError as error:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning disposition is invalid.") from error
    replacements = [
        _outcome_mapping(value, frozenset({"position", "item_id"})) for value in _outcome_array(record["replacements"])
    ]
    _validate_positioned_records(replacements, identity=lambda value: _outcome_string(value["item_id"]) or "")
    outcome_evidence = _outcome_string(record["outcome_evidence"], nullable=True)
    resulting = record["resulting_scope"]
    if disposition == PlanningDisposition.REVISED:
        resulting_revision, resulting_digest = _validate_anchor_record(resulting)
        if resulting_revision != evaluated_revision + 1 or resulting_digest == evaluated_digest:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Revised scope anchor is invalid.")
    elif resulting is not None:
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Only revised resolution may carry a resulting scope."
        )
    terminal = disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}
    if terminal != (outcome_evidence is not None):
        raise DecisionError(
            DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Terminal outcome evidence does not match disposition."
        )
    if (disposition == PlanningDisposition.SUPERSEDED) != bool(replacements):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Replacement records do not match disposition.")


def validate_history_outcome(outcome_schema: str, payload: bytes) -> None:
    if not payload.endswith(b"\n"):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome JSON requires one final LF.")
    try:
        decoded = cast(JsonValue, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome is not valid UTF-8 JSON.") from error
    if not isinstance(decoded, dict):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome root must be a JSON object.")
    record = decoded
    if any(not isinstance(key, str) for key in record):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome member names must be strings.")
    if _history_bytes(record) != payload:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome JSON is not canonical.")
    match outcome_schema:
        case "item-scope-change/v1":
            _validate_scope_change(record)
        case "planning-impact/v1":
            _validate_planning_impact_outcome(record)
        case "planning-impact-resolution/v1":
            _validate_planning_resolution_outcome(record)
        case _:
            raise DecisionError(
                DecisionErrorCode.HISTORY_OUTCOME_INVALID, f"Unsupported outcome schema '{outcome_schema}'."
            )
