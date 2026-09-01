from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal

import msgspec

from pinboard.application import stored_state
from pinboard.domain import work_models

type ItemStatusSchema = Literal["pinboard-item-status/v1"]
type ItemStatusAuthority = Literal["sqlite-v3"]
type DecimalRevision = Annotated[str, msgspec.Meta(pattern=r"^[0-9]+$")]
type PreparationStatus = Literal["active", "expired", "released", "revoked"]


class ItemStatusAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    state: work_models.AttemptState
    candidate_revision: str | None


class PreparationStatusView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    definition_revision: int
    definition_digest: str
    task_id: str
    host_id: str
    lease_id: str
    generation: int
    expires_at: str
    status: PreparationStatus


class ItemStatus(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ItemStatusSchema
    authority: ItemStatusAuthority
    revision: DecimalRevision
    item_id: str
    label: str
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str
    attempts: tuple[ItemStatusAttempt, ...]
    preparation: PreparationStatusView | None = None


class ParallelSelection(Enum):
    ALL_SAFE = "all-safe"
    SELECTED = "selected"


class ParallelReasonCode(Enum):
    ATTEMPT_OWNED = "attempt-owned"
    DEPENDENCY_LIVE = "dependency-live"
    STATE_NOT_LAUNCHABLE = "state-not-launchable"
    PREPARATION_OWNED = "preparation-owned"


class QueryRejectionCode(Enum):
    PARALLEL_SELECTION_INVALID = "PARALLEL_SELECTION_INVALID"
    PARALLEL_TIME_INVALID = "PARALLEL_TIME_INVALID"


@dataclass(frozen=True, slots=True)
class QueryFailure:
    code: QueryRejectionCode
    message: str


type QueryResult[Value] = Value | QueryFailure


class DependencyReason(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    reason: str


class ReviewFlag(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: work_models.ProposalRelationKind
    related_item: str | None
    reason: str


class OverviewItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    label: str
    state: work_models.WorkState
    position: int
    eligible: bool
    timing: str | None
    depends_on: tuple[str, ...]
    dependency_reasons: tuple[DependencyReason, ...]
    review_flags: tuple[ReviewFlag, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str
    preparation: PreparationStatusView | None = None


class WorkOverview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    immediate_options: tuple[str, ...]


class ParallelReason(msgspec.Struct, frozen=True):
    code: ParallelReasonCode
    message: str


class LaunchableParallelItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: work_models.WorkState
    attempt_id: str | None


class ExcludedParallelItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: work_models.WorkState
    attempt_id: str | None
    reasons: Annotated[tuple[ParallelReason, ...], msgspec.Meta(min_length=1)]


type ParallelItem = LaunchableParallelItem | ExcludedParallelItem


class ParallelPreview(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: ParallelSelection
    safe: bool
    items: tuple[ParallelItem, ...]


class WorkItemDefinitionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-item-definition/v1"]
    title: str
    objective: str
    hypothesis: str
    evidence: tuple[str, ...]
    scope: tuple[str, ...]
    non_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    effect: str
    unlock: str


class ItemDefinition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-definition/v1"]
    authority: Literal["sqlite-v3"]
    project_revision: int
    item_id: str
    definition_revision: int
    definition_digest: str
    definition: WorkItemDefinitionView


class ItemDefinitionHistoryRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    digest: str
    definition: WorkItemDefinitionView
    reason: str
    source_task: str
    timestamp: str
    before_digest: str | None
    after_digest: str
    committed_project_revision: int


class ItemDefinitionHistory(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-definition-history/v1"]
    authority: Literal["sqlite-v3"]
    project_revision: int
    item_id: str
    revisions: tuple[ItemDefinitionHistoryRow, ...]
    next_before_revision: int | None
