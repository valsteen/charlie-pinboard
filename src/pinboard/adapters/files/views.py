from collections.abc import Mapping
from pathlib import Path

from pinboard.adapters.files.errors import FileIOError
from pinboard.adapters.files.file_io import atomic_replace, ensure_child_directory
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.application import stored_state
from pinboard.application.ports import WorkStore
from pinboard.application.queries import overview_from_state
from pinboard.domain.identifiers import AttemptId

NOTICE = "Generated projection; SQLite is authoritative."


def _dependency_key(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _header(kind: str, revision: int) -> str:
    return f"---\nkind: {kind}\ndatabase_revision: {revision}\nauthority: sqlite-v1\n---\n\n> {NOTICE}\n\n"


def _queue(state: stored_state.StoredWorkState) -> bytes:
    overview = overview_from_state(state)
    lines = [
        _header("work-queue-view", state.lifecycle.project.revision),
        "# Work Queue\n\n",
        "| Position | Item | State | Eligible | Review | Attempt | Next action |\n",
        "| --- | --- | --- | --- | --- | --- | --- |\n",
    ]
    lines.extend(
        (
            f"| {item.position} | {item.item_id} | {item.state.value} | "
            f"{'yes' if item.eligible else 'no'} | "
            f"{', '.join(flag.kind.value for flag in item.review_flags) or '—'} | "
            f"{item.attempt_id or '—'} | {item.next_action or '—'} |\n"
        )
        for item in overview.items
    )
    return "".join(lines).encode()


def _current(state: stored_state.StoredWorkState) -> bytes:
    focus = state.focus
    return (
        _header("work-current-view", state.lifecycle.project.revision)
        + "# Current Work\n\n"
        + f"- Item: {focus.item_id or 'none'}\n"
        + f"- Attempt: {focus.attempt_id or 'none'}\n"
        + f"- Next action: {focus.next_action}\n"
    ).encode()


def _item(state: stored_state.StoredWorkState, item: stored_state.StoredWorkItem) -> bytes:
    dependencies = tuple(
        value.dependency_id
        for value in sorted(state.lifecycle.dependencies, key=_dependency_key)
        if value.item_id == item.item_id
    )
    overview_item = next(
        (value for value in overview_from_state(state).items if value.item_id == str(item.item_id)),
        None,
    )
    dependency_reasons = (
        tuple(f"{value.item_id}: {value.reason}" for value in overview_item.dependency_reasons)
        if overview_item is not None
        else ()
    )
    review_flags = (
        tuple(
            f"{value.kind.value}{f' ({value.related_item})' if value.related_item is not None else ''}: {value.reason}"
            for value in overview_item.review_flags
        )
        if overview_item is not None
        else ()
    )
    definition = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item.item_id),
        None,
    )
    if definition is None:
        raise ValueError(f"Work item '{item.item_id}' has no current definition.")
    accepted = definition.definition
    return (
        _header("work-item-view", state.lifecycle.project.revision)
        + f"# {accepted.title}\n\n"
        + f"- Item: {item.item_id}\n"
        + f"- State: {item.state.value}\n"
        + f"- Queue position: {item.queue_position or 'none'}\n"
        + f"- Eligible: {'yes' if overview_item is not None and overview_item.eligible else 'no'}\n"
        + f"- Subject revision: {item.subject_revision}\n"
        + f"- Dependencies: {', '.join(dependencies) if dependencies else 'none'}\n"
        + f"- Dependency reasons: {'; '.join(dependency_reasons) if dependency_reasons else 'none'}\n"
        + f"- Review flags: {'; '.join(review_flags) if review_flags else 'none'}\n"
        + f"- Outcome evidence: {item.outcome_evidence or 'none'}\n"
        + "\n## Accepted definition\n\n"
        + f"- Revision: {definition.revision}\n"
        + f"- Digest: {definition.digest}\n"
        + f"- Objective: {accepted.objective}\n"
        + f"- Hypothesis: {accepted.hypothesis}\n"
        + f"- Evidence: {'; '.join(accepted.evidence) if accepted.evidence else 'none'}\n"
        + f"- Scope: {'; '.join(accepted.scope)}\n"
        + f"- Non-scope: {'; '.join(accepted.non_scope) if accepted.non_scope else 'none'}\n"
        + f"- Acceptance criteria: {'; '.join(accepted.acceptance_criteria)}\n"
        + f"- Effect: {accepted.effect}\n"
        + f"- Unlock: {accepted.unlock}\n"
    ).encode()


def _attempt(
    state: stored_state.StoredWorkState,
    attempt: stored_state.StoredAttempt,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> bytes:
    if (brief := attempt_briefs.get(attempt.attempt_id)) is not None:
        return brief
    return (
        _header("work-attempt-view", state.lifecycle.project.revision)
        + f"# Attempt {attempt.attempt_id}\n\n"
        + f"- Item: {attempt.item_id}\n"
        + f"- State: {attempt.state.value}\n"
        + f"- Branch: {attempt.branch}\n"
        + f"- Base revision: {attempt.base_revision}\n"
        + f"- Candidate revision: {attempt.candidate_revision or 'none'}\n"
    ).encode()


def _history_row(receipt: stored_state.StoredTransitionReceipt) -> str:
    return (
        f"| {receipt.history_id} | {receipt.project_revision} | {receipt.action_kind.value} | "
        f"{receipt.subject_id} | {receipt.committed_at.isoformat()} |\n"
    )


def _history(state: stored_state.StoredWorkState) -> bytes:
    return (
        _header("work-history-view", state.lifecycle.project.revision)
        + "# Transition History\n\n"
        + "| History | Revision | Action | Subject | Committed |\n"
        + "| --- | --- | --- | --- | --- |\n"
        + "".join(_history_row(receipt) for receipt in state.transition_receipts)
        + "\n## Definition History\n\n"
        + "| Item | Revision | Digest | Reason | Source task | Accepted revision | Accepted at |\n"
        + "| --- | --- | --- | --- | --- | --- | --- |\n"
        + "".join(
            f"| {value.item_id} | {value.revision} | {value.digest} | {value.reason} | "
            f"{value.source_task_id} | {value.accepted_project_revision} | {value.accepted_at.isoformat()} |\n"
            for value in state.lifecycle.definition_revisions
        )
    ).encode()


def _write_views(
    work_root: Path,
    state: stored_state.StoredWorkState,
    affected: AffectedViews,
    attempt_briefs: Mapping[AttemptId, bytes],
) -> None:
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
            atomic_replace(attempt_root / f"{attempt_id}.md", _attempt(state, attempt, attempt_briefs))


def refresh(
    store: WorkStore,
    work_root: Path,
    affected: AffectedViews,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
) -> ViewRefreshResult:
    state = store.snapshot()
    try:
        _write_views(work_root, state, affected, attempt_briefs or {})
    except FileIOError as error:
        return ViewRefreshResult(
            state.lifecycle.project.revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error}",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return ViewRefreshResult(state.lifecycle.project.revision)


def expected_view_bytes(
    state: stored_state.StoredWorkState,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
) -> dict[str, bytes]:
    """Return every generated selector and its canonical bytes for one SQLite snapshot."""

    result = {
        "queue.md": _queue(state),
        "current.md": _current(state),
        "history.md": _history(state),
    }
    result.update((f"items/{item.item_id}.md", _item(state, item)) for item in state.lifecycle.work_items)
    result.update(
        (f"attempts/{attempt.attempt_id}.md", _attempt(state, attempt, attempt_briefs or {}))
        for attempt in state.lifecycle.attempts
    )
    return result


def rebuild(
    store: WorkStore,
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
) -> ViewRefreshResult:
    state = store.snapshot()
    try:
        _write_views(
            work_root,
            state,
            AffectedViews(
                queue=True,
                current_focus=True,
                history=True,
                items=tuple(item.item_id for item in state.lifecycle.work_items),
                attempts=tuple(attempt.attempt_id for attempt in state.lifecycle.attempts),
            ),
            attempt_briefs or {},
        )
    except FileIOError as error:
        return ViewRefreshResult(
            state.lifecycle.project.revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {error}",
                "Resolve the filesystem problem and run 'pinboard views rebuild' again.",
            ),
        )
    return ViewRefreshResult(state.lifecycle.project.revision)
