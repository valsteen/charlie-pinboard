import re
from dataclasses import dataclass
from datetime import datetime

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import ItemId, ResourceId
from charlie_pinboard.domain.model import CoordinationCommandAuthority, LedgerSnapshot, ResourceDefinition, ScopeAnchor


@dataclass(frozen=True, slots=True)
class PortableResourceDefinition:
    resource_id: ResourceId
    kind: str
    description: str


@dataclass(frozen=True, slots=True)
class ResourceDefinitionEditOperation:
    authority: CoordinationCommandAuthority
    current_definition_revision: int | None
    definition: PortableResourceDefinition
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ItemSubjectRevisionChange:
    item: ItemId
    before: int
    after: int


@dataclass(frozen=True, slots=True)
class ResourceDefinitionUnchanged:
    existing: PortableResourceDefinition


@dataclass(frozen=True, slots=True)
class ResourceDefinitionEditDecision:
    before_definition: PortableResourceDefinition | None
    after_definition: PortableResourceDefinition
    definition_revision_before: int | None
    definition_revision_after: int
    affected_item_revisions: tuple[ItemSubjectRevisionChange, ...]


type ResourceDefinitionResult = ResourceDefinitionUnchanged | ResourceDefinitionEditDecision


def _portable(value: ResourceDefinition) -> PortableResourceDefinition:
    return PortableResourceDefinition(value.resource_id, value.kind, value.description)


def _scope_key(value: ScopeAnchor) -> str:
    return str(value.item)


def decide_resource_definition_edit(
    snapshot: LedgerSnapshot,
    operation: ResourceDefinitionEditOperation,
) -> ResourceDefinitionResult | DecisionFailure:
    if snapshot.coordination_authority != operation.authority or operation.authority.expires_at <= operation.changed_at:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Resource definition editing requires exact live coordination authority.",
        )
    definition = operation.definition
    if (
        not definition.resource_id
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", definition.kind) is None
        or not definition.description.strip()
    ):
        return DecisionFailure(
            DecisionFailureCode.RESOURCE_DECLARATION_INVALID,
            "A resource definition requires identity, kind, and description.",
        )
    current = snapshot.resource_definition(definition.resource_id)
    if current is None:
        if operation.current_definition_revision is not None:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "The supplied resource definition revision is stale.",
            )
        return ResourceDefinitionEditDecision(None, definition, None, 1, ())
    if current.subject_revision != operation.current_definition_revision:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The supplied resource definition revision is stale.",
        )
    before = _portable(current)
    if before == definition:
        return ResourceDefinitionUnchanged(before)
    affected: list[ItemSubjectRevisionChange] = []
    for scope in sorted(snapshot.scopes, key=_scope_key):
        if any(value.resource_id == definition.resource_id for value in scope.scope.resource_requirements):
            revision = snapshot.subject_revision(scope.item)
            if revision is None:
                return DecisionFailure(
                    DecisionFailureCode.WORK_STATE_INVALID,
                    f"Requiring item '{scope.item}' has no subject revision.",
                )
            affected.append(ItemSubjectRevisionChange(scope.item, int(revision), int(revision) + 1))
    return ResourceDefinitionEditDecision(
        before,
        definition,
        current.subject_revision,
        current.subject_revision + 1,
        tuple(affected),
    )
