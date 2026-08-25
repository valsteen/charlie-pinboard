from datetime import UTC, datetime
from enum import Enum

import msgspec

from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    AttemptLeaseState,
    ItemDependency,
    StoredAttempt,
    StoredProposal,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import AttemptState, WorkState


class QueryError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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


def _dependency_key(value: ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: ItemDependency) -> int:
    return value.position


def _item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _parallel_item_key(value: ParallelItem) -> str:
    return value.item_id


_TERMINAL_ITEM_STATES = {
    StoredWorkItemState.DONE,
    StoredWorkItemState.SUPERSEDED,
    StoredWorkItemState.DROPPED,
}


def _work_state(value: StoredWorkItemState) -> WorkState:
    try:
        return WorkState(value.value)
    except ValueError as error:
        raise QueryError("WORK_STATE_INVALID", f"Item state {value.value!r} is not live.") from error


def overview_from_state(state: StoredWorkState) -> WorkOverview:
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
    }
    dependencies = {
        item.item_id: tuple(
            str(link.dependency_id)
            for link in sorted(
                (candidate for candidate in state.lifecycle.dependencies if candidate.item_id == item.item_id),
                key=_dependency_key,
            )
        )
        for item in state.lifecycle.work_items
    }
    items = tuple(
        OverviewItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            item.timing.value if item.timing is not None else None,
            dependencies[item.item_id],
            str(attempts[item.item_id]) if item.item_id in attempts else None,
            item.next_action,
            item.notes or "",
        )
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if item.state not in _TERMINAL_ITEM_STATES
    )
    live_ids = frozenset(item.item_id for item in items)
    immediate = tuple(
        item.item_id
        for item in items
        if item.state in {WorkState.INTAKE, WorkState.READY, WorkState.DEFERRED}
        or (
            item.state in {WorkState.PAUSED, WorkState.BLOCKED}
            and not any(dependency in live_ids for dependency in item.depends_on)
        )
    )
    inbox = tuple(
        str(proposal.proposal_id)
        for proposal in sorted(state.proposals.proposals, key=_proposal_key)
        if proposal.disposition is None
    )
    return WorkOverview(
        "pinboard-overview/v1",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(state.focus.item_id) if state.focus.item_id is not None else None,
        str(state.focus.attempt_id) if state.focus.attempt_id is not None else None,
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == AttemptState.ACTIVE
        ),
        items,
        inbox,
        immediate,
    )


def read_overview(store: WorkStore) -> WorkOverview:
    return overview_from_state(store.snapshot())


def _preview_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise QueryError("PARALLEL_TIME_INVALID", "Preview time must be timezone-aware.")
    return current.astimezone(UTC)


def _parallel_reasons(
    state: StoredWorkState,
    item_id: ItemId,
    live_items: frozenset[str],
    current: datetime,
) -> tuple[ParallelReason, ...]:
    item = next(value for value in state.lifecycle.work_items if value.item_id == item_id)
    if item.state not in {StoredWorkItemState.READY, StoredWorkItemState.ACTIVE}:
        return (
            ParallelReason(
                ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    live_dependencies = tuple(
        str(link.dependency_id)
        for link in sorted(state.lifecycle.dependencies, key=_dependency_position)
        if link.item_id == item_id and str(link.dependency_id) in live_items
    )
    if live_dependencies:
        return (
            ParallelReason(
                ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    attempt = next(
        (
            candidate
            for candidate in state.lifecycle.attempts
            if candidate.item_id == item_id and candidate.state == AttemptState.ACTIVE
        ),
        None,
    )
    if attempt is not None:
        lease = next(
            (candidate for candidate in state.authority.attempt_leases if candidate.attempt_id == attempt.attempt_id),
            None,
        )
        if lease is not None and lease.state == AttemptLeaseState.ACTIVE and current < lease.expires_at:
            return (
                ParallelReason(
                    ParallelReasonCode.ATTEMPT_OWNED,
                    f"Active attempt '{attempt.attempt_id}' is owned until {lease.expires_at.isoformat()}.",
                ),
            )
    return ()


def preview_parallel(
    store: WorkStore,
    *,
    selected: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ParallelPreview:
    state = store.snapshot()
    current = _preview_time(now)
    live = tuple(item for item in state.lifecycle.work_items if item.state not in _TERMINAL_ITEM_STATES)
    by_id = {str(item.item_id): item for item in live}
    if len(selected) != len(set(selected)) or any(item_id not in by_id for item_id in selected):
        raise QueryError("PARALLEL_SELECTION_INVALID", "Selected item identities must be unique current items.")
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else tuple(sorted(live, key=_item_key))
    live_ids = frozenset(by_id)
    launchable: list[ParallelItem] = []
    excluded: list[ParallelItem] = []
    for item in candidates:
        reasons = _parallel_reasons(state, item.item_id, live_ids, current)
        value = ParallelItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            str(
                next(
                    (
                        attempt.attempt_id
                        for attempt in state.lifecycle.attempts
                        if attempt.item_id == item.item_id
                        and attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
                    ),
                    "",
                )
            )
            or None,
            ParallelOutcome.EXCLUDED if reasons else ParallelOutcome.LAUNCHABLE,
            reasons,
        )
        (excluded if reasons else launchable).append(value)
    return ParallelPreview(
        "pinboard-parallel-preview/v1",
        str(state.lifecycle.project.revision),
        ParallelSelection.SELECTED if selected else ParallelSelection.ALL_SAFE,
        not selected or not excluded,
        tuple(sorted(launchable, key=_parallel_item_key)),
        tuple(sorted(excluded, key=_parallel_item_key)),
    )
