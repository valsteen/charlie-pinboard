from enum import Enum
from typing import Annotated, Literal

import msgspec

from charlie_pinboard.application.stored_state import StoredWorkItemState
from charlie_pinboard.domain import work_models

type ItemStatusSchema = Literal["pinboard-item-status/v1"]
type ItemStatusAuthority = Literal["sqlite-v1"]
type DecimalRevision = Annotated[str, msgspec.Meta(pattern=r"^[0-9]+$")]


class ItemStatusAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    state: work_models.AttemptState
    candidate_revision: str | None


class ItemStatus(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ItemStatusSchema
    authority: ItemStatusAuthority
    revision: DecimalRevision
    item_id: str
    label: str
    state: StoredWorkItemState
    timing: work_models.Timing | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str
    attempts: tuple[ItemStatusAttempt, ...]


class ParallelSelection(Enum):
    ALL_SAFE = "all-safe"
    SELECTED = "selected"


class ParallelReasonCode(Enum):
    ATTEMPT_OWNED = "attempt-owned"
    DEPENDENCY_LIVE = "dependency-live"
    STATE_NOT_LAUNCHABLE = "state-not-launchable"


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
