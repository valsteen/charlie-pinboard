from enum import Enum

import msgspec

from charlie_pinboard.domain.work_models import WorkState


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

class OverviewItem(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str

class WorkOverview(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    inbox: tuple[str, ...]
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

