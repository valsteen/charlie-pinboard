from dataclasses import dataclass
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import FileIOError, atomic_replace, ensure_child_directory
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import (
    ItemDependency,
    StoredAttempt,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkState,
)
from charlie_pinboard.domain.identifiers import AttemptId, ItemId

NOTICE = "Generated projection; SQLite is authoritative."


def _item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _dependency_key(value: ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


@dataclass(frozen=True, slots=True)
class ViewWarning:
    code: str
    message: str
    repair: str


@dataclass(frozen=True, slots=True)
class ViewRefreshResult:
    database_revision: int
    warning: ViewWarning | None = None


@dataclass(frozen=True, slots=True)
class AffectedViews:
    queue: bool = False
    current_focus: bool = False
    history: bool = False
    items: tuple[ItemId, ...] = ()
    attempts: tuple[AttemptId, ...] = ()

    @classmethod
    def current(cls) -> AffectedViews:
        return cls(current_focus=True)

    @classmethod
    def all(cls, state: StoredWorkState) -> AffectedViews:
        return cls(
            queue=True,
            current_focus=True,
            history=True,
            items=tuple(item.item_id for item in state.lifecycle.work_items),
            attempts=tuple(attempt.attempt_id for attempt in state.lifecycle.attempts),
        )


def _header(kind: str, revision: int) -> str:
    return f"---\nkind: {kind}\ndatabase_revision: {revision}\nauthority: sqlite-v1\n---\n\n> {NOTICE}\n\n"


def _queue(state: StoredWorkState) -> bytes:
    lines = [
        _header("work-queue-view", state.lifecycle.project.revision),
        "# Work Queue\n\n",
        "| Item | State | Timing | Attempt | Next action |\n",
        "| --- | --- | --- | --- | --- |\n",
    ]
    attempts = {attempt.item_id: attempt.attempt_id for attempt in state.lifecycle.attempts}
    lines.extend(
        (
            f"| {item.item_id} | {item.state.value} | "
            f"{item.timing.value if item.timing is not None else '—'} | "
            f"{attempts.get(item.item_id, '—')} | {item.next_action or '—'} |\n"
        )
        for item in sorted(state.lifecycle.work_items, key=_item_key)
    )
    return "".join(lines).encode()


def _current(state: StoredWorkState) -> bytes:
    focus = state.focus
    return (
        _header("work-current-view", state.lifecycle.project.revision)
        + "# Current Work\n\n"
        + f"- Item: {focus.item_id or 'none'}\n"
        + f"- Attempt: {focus.attempt_id or 'none'}\n"
        + f"- Next action: {focus.next_action}\n"
    ).encode()


def _item(state: StoredWorkState, item: StoredWorkItem) -> bytes:
    dependencies = tuple(
        value.dependency_id
        for value in sorted(state.lifecycle.dependencies, key=_dependency_key)
        if value.item_id == item.item_id
    )
    return (
        _header("work-item-view", state.lifecycle.project.revision)
        + f"# {item.user_label}\n\n"
        + f"- Item: {item.item_id}\n"
        + f"- State: {item.state.value}\n"
        + f"- Subject revision: {item.subject_revision}\n"
        + f"- Dependencies: {', '.join(dependencies) if dependencies else 'none'}\n"
        + f"- Outcome evidence: {item.outcome_evidence or 'none'}\n"
    ).encode()


def _attempt(state: StoredWorkState, attempt: StoredAttempt) -> bytes:
    return (
        _header("work-attempt-view", state.lifecycle.project.revision)
        + f"# Attempt {attempt.attempt_id}\n\n"
        + f"- Item: {attempt.item_id}\n"
        + f"- State: {attempt.state.value}\n"
        + f"- Branch: {attempt.branch}\n"
        + f"- Base revision: {attempt.base_revision}\n"
        + f"- Candidate revision: {attempt.candidate_revision or 'none'}\n"
    ).encode()


def _history_row(receipt: StoredTransitionReceipt) -> str:
    return (
        f"| {receipt.history_id} | {receipt.project_revision} | {receipt.action_kind.value} | "
        f"{receipt.subject_id} | {receipt.committed_at.isoformat()} |\n"
    )


def _history(state: StoredWorkState) -> bytes:
    return (
        _header("work-history-view", state.lifecycle.project.revision)
        + "# Transition History\n\n"
        + "| History | Revision | Action | Subject | Committed |\n"
        + "| --- | --- | --- | --- | --- |\n"
        + "".join(_history_row(receipt) for receipt in state.history.receipts)
    ).encode()


def _write_views(work_root: Path, state: StoredWorkState, affected: AffectedViews) -> None:
    view_root = ensure_child_directory(work_root, "views")
    item_root = ensure_child_directory(view_root, "items")
    attempt_root = ensure_child_directory(view_root, "attempts")
    if affected.queue:
        atomic_replace(view_root / "queue.md", _queue(state))
    if affected.current_focus:
        atomic_replace(view_root / "current.md", _current(state))
    if affected.history:
        atomic_replace(view_root / "history.md", _history(state))
    items = {item.item_id: item for item in state.lifecycle.work_items}
    for item_id in affected.items:
        item = items.get(item_id)
        if item is not None:
            atomic_replace(item_root / f"{item_id}.md", _item(state, item))
    attempts = {attempt.attempt_id: attempt for attempt in state.lifecycle.attempts}
    for attempt_id in affected.attempts:
        attempt = attempts.get(attempt_id)
        if attempt is not None:
            atomic_replace(attempt_root / f"{attempt_id}.md", _attempt(state, attempt))


def refresh(store: WorkStore, work_root: Path, affected: AffectedViews) -> ViewRefreshResult:
    state = store.snapshot()
    try:
        _write_views(work_root, state, affected)
    except FileIOError as error:
        return ViewRefreshResult(
            state.lifecycle.project.revision,
            ViewWarning(
                "VIEW_REFRESH_REQUIRED",
                f"The SQLite transition succeeded, but generated views need repair: {error}",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return ViewRefreshResult(state.lifecycle.project.revision)


def rebuild(store: WorkStore, work_root: Path) -> ViewRefreshResult:
    state = store.snapshot()
    try:
        _write_views(work_root, state, AffectedViews.all(state))
    except FileIOError as error:
        return ViewRefreshResult(
            state.lifecycle.project.revision,
            ViewWarning(
                "VIEW_REFRESH_REQUIRED",
                f"Generated views could not be rebuilt: {error}",
                "Resolve the filesystem problem and run 'pinboard views rebuild' again.",
            ),
        )
    return ViewRefreshResult(state.lifecycle.project.revision)
