from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import (
    CoordinationCommandAuthority,
    ItemScope,
    LedgerSnapshot,
    ResourceRequirement,
    ScopeAnchor,
    ScopeDependency,
)


@dataclass(frozen=True, slots=True)
class ReplaceDependenciesOperation:
    authority: CoordinationCommandAuthority
    item: ItemId
    current_scope: ScopeAnchor
    dependencies: tuple[ScopeDependency, ...]
    next_scope: ItemScope
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ReplaceResourceRequirementsOperation:
    authority: CoordinationCommandAuthority
    item: ItemId
    current_scope: ScopeAnchor
    resource_requirements: tuple[ResourceRequirement, ...]
    next_scope: ItemScope
    changed_at: datetime


type ItemScopeEditOperation = ReplaceDependenciesOperation | ReplaceResourceRequirementsOperation


@dataclass(frozen=True, slots=True)
class ItemScopeEditDecision:
    item: ItemId
    before_scope: ScopeAnchor
    after_scope: ScopeAnchor
    dependencies: tuple[ScopeDependency, ...]
    resource_requirements: tuple[ResourceRequirement, ...]


def _positions(values: tuple[ScopeDependency, ...] | tuple[ResourceRequirement, ...]) -> bool:
    return tuple(value.position for value in values) == tuple(range(len(values)))


def decide_item_scope_edit(  # noqa: C901, PLR0912
    snapshot: LedgerSnapshot,
    operation: ItemScopeEditOperation,
) -> ItemScopeEditDecision | DecisionFailure:
    if snapshot.coordination_authority != operation.authority or operation.authority.expires_at <= operation.changed_at:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Item scope editing requires live coordination."
        )
    item = snapshot.item(operation.item)
    current = next((value for value in snapshot.scopes if value.item == operation.item), None)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Only a live item can change scope.")
    if current is None or current != operation.current_scope:
        return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_STALE, "The item scope changed before editing.")
    match operation:
        case ReplaceDependenciesOperation(dependencies=dependencies, next_scope=next_scope):
            resource_requirements = current.scope.resource_requirements
            if (
                not _positions(dependencies)
                or len({value.dependency_id for value in dependencies}) != len(dependencies)
                or any(
                    value.dependency_id == operation.item or snapshot.item(value.dependency_id) is None
                    for value in dependencies
                )
            ):
                return DecisionFailure(
                    DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
                    "Dependencies must be ordered unique live items other than their owner.",
                )
            if next_scope.dependencies != dependencies or next_scope.resource_requirements != resource_requirements:
                return DecisionFailure(
                    DecisionFailureCode.ITEM_SCOPE_INVALID, "The next scope changed unrelated facts."
                )
        case ReplaceResourceRequirementsOperation(
            resource_requirements=resource_requirements,
            next_scope=next_scope,
        ):
            dependencies = current.scope.dependencies
            declared = {value.resource_id for value in snapshot.resource_definitions}
            if (
                not _positions(resource_requirements)
                or len({value.resource_id for value in resource_requirements}) != len(resource_requirements)
                or any(value.resource_id not in declared for value in resource_requirements)
            ):
                return DecisionFailure(
                    DecisionFailureCode.RESOURCE_REQUIREMENT_INVALID,
                    "Resource requirements must be ordered unique declared resources.",
                )
            if next_scope.dependencies != dependencies or next_scope.resource_requirements != resource_requirements:
                return DecisionFailure(
                    DecisionFailureCode.ITEM_SCOPE_INVALID, "The next scope changed unrelated facts."
                )
        case _ as unreachable:
            assert_never(unreachable)
    if next_scope.item_id != operation.item:
        return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, "The next scope has the wrong owner.")
    digest = item_scope_digest(next_scope)
    match digest:
        case DecisionFailure():
            return digest
        case value:
            pass
    if value == current.digest:
        return DecisionFailure(DecisionFailureCode.ITEM_SCOPE_INVALID, "A scope edit must change semantic scope.")
    after = ScopeAnchor(operation.item, current.revision + 1, value, next_scope)
    return ItemScopeEditDecision(operation.item, current, after, dependencies, resource_requirements)
