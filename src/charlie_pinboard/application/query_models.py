from enum import Enum
from typing import Annotated, Literal

import msgspec

from charlie_pinboard.domain.work_models import AttemptState, ProposalRelationKind, Timing, WorkState

type ItemStatusSchema = Literal["pinboard-item-status/v1"]
type ItemStatusAuthority = Literal["sqlite-v1"]
type DecimalRevision = Annotated[str, msgspec.Meta(pattern=r"^[0-9]+$")]


class ItemStatusState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"
    DONE = "done"
    SUPERSEDED = "superseded"
    DROPPED = "dropped"


class ItemStatusAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    state: AttemptState
    candidate_revision: str | None


class ItemStatus(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ItemStatusSchema
    authority: ItemStatusAuthority
    revision: DecimalRevision
    item_id: str
    label: str
    state: ItemStatusState
    timing: Timing | None
    outcome_evidence: str | None
    next_action: str | None
    notes: str
    attempts: tuple[ItemStatusAttempt, ...]


class ParallelOutcome(Enum):
    LAUNCHABLE = "launchable"
    EXCLUDED = "excluded"


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
    kind: ProposalRelationKind
    related_item: str | None
    reason: str


class OverviewItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    label: str
    state: WorkState
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


class ParallelItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: WorkState
    attempt_id: str | None
    outcome: ParallelOutcome
    reasons: tuple[ParallelReason, ...] = ()


class ParallelPreview(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: ParallelSelection
    safe: bool
    launchable: tuple[ParallelItem, ...]
    excluded: tuple[ParallelItem, ...]
