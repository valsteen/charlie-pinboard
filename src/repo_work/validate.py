from dataclasses import dataclass
from pathlib import Path

from repo_work.coordinator import CoordinatorError, read_coordinator
from repo_work.diagnostics import Diagnostic, Severity
from repo_work.markdown import (
    ParseError,
    parse_attempt,
    parse_current,
    parse_item,
    parse_queue,
    require_document_header,
)
from repo_work.model import SCHEMA_V1, Queue, QueueItem, WorkState


@dataclass(frozen=True, slots=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(diagnostic.severity == Severity.ERROR for diagnostic in self.diagnostics)

    def render(self) -> str:
        if not self.diagnostics:
            return "OK WORK_STATE_VALID"
        return "\n".join(diagnostic.render() for diagnostic in self.diagnostics)


def _error(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def _parse_error(error: ParseError) -> Diagnostic:
    return _error(error.code, error.path, str(error))


def _read_queue(work_root: Path) -> tuple[Queue | None, list[Diagnostic]]:
    path = work_root / "queue.md"
    try:
        queue = parse_queue(path)
    except OSError as error:
        return None, [_error("QUEUE_UNREADABLE", path, str(error))]
    except ParseError as error:
        return None, [_parse_error(error)]
    diagnostics: list[Diagnostic] = []
    if queue.header.get("kind") != "work-queue":
        diagnostics.append(_error("DOCUMENT_KIND_INVALID", path, "queue.md must have kind work-queue."))
    if queue.header.get("schema") != SCHEMA_V1:
        diagnostics.append(_error("DOCUMENT_SCHEMA_INVALID", path, "queue.md must use repo-work/v1."))
    return queue, diagnostics


def _validate_item_records(queue: Queue, work_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    item_root = work_root / "items"
    queue_ids = {item.item for item in queue.items}
    for item in queue.items:
        path = item_root / f"{item.item}.md"
        if not path.is_file():
            diagnostics.append(_error("ITEM_RECORD_MISSING", path, f"No record exists for '{item.item}'."))
            continue
        try:
            record = parse_item(path)
        except ParseError as error:
            diagnostics.append(_parse_error(error))
            continue
        if record.item != item.item:
            diagnostics.append(
                _error("ITEM_RECORD_MISMATCH", path, f"Record names '{record.item}', expected '{item.item}'.")
            )
    if item_root.is_dir():
        diagnostics.extend(
            _error("ITEM_RECORD_ORPHANED", path, "Nonterminal item record has no canonical queue row.")
            for path in item_root.glob("*.md")
            if path.stem not in queue_ids
        )
    return diagnostics


def _completed_items(work_root: Path) -> tuple[set[str], list[Diagnostic]]:
    completed: set[str] = set()
    diagnostics: list[Diagnostic] = []
    history_root = work_root / "history" / "items"
    if not history_root.is_dir():
        return completed, diagnostics
    for path in history_root.glob("*.md"):
        try:
            header = require_document_header(path, "work-history")
        except ParseError as error:
            diagnostics.append(_parse_error(error))
            continue
        item = header.get("item")
        if isinstance(item, str) and header.get("state") == "done":
            completed.add(item)
    return completed, diagnostics


def _dependency_graph(queue: Queue, completed: set[str]) -> tuple[dict[str, tuple[str, ...]], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    by_id = queue.by_id()
    graph: dict[str, tuple[str, ...]] = {}
    for item in queue.items:
        graph[item.item] = tuple(dependency for dependency in item.depends_on if dependency in by_id)
        diagnostics.extend(
            _error("DEPENDENCY_UNKNOWN", queue.path, f"Item '{item.item}' depends on unknown item '{dependency}'.")
            for dependency in item.depends_on
            if dependency not in by_id and dependency not in completed
        )
    return graph, diagnostics


def _dependency_cycles(graph: dict[str, tuple[str, ...]], path: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str, route: tuple[str, ...]) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            cycle = (*route[route.index(item_id) :], item_id)
            diagnostics.append(_error("DEPENDENCY_CYCLE", path, f"Dependency cycle: {' -> '.join(cycle)}."))
            return
        visiting.add(item_id)
        for dependency in graph.get(item_id, ()):
            visit(dependency, (*route, dependency))
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in graph:
        visit(item_id, (item_id,))
    return diagnostics


def _validate_dependencies(queue: Queue, work_root: Path) -> list[Diagnostic]:
    completed, diagnostics = _completed_items(work_root)
    graph, graph_diagnostics = _dependency_graph(queue, completed)
    diagnostics.extend(graph_diagnostics)
    diagnostics.extend(_dependency_cycles(graph, queue.path))
    return diagnostics


def _validate_coordinator(work_root: Path, project_root: Path) -> list[Diagnostic]:
    path = work_root / "coordinator.json"
    if not path.is_file():
        return [_error("COORDINATOR_NOT_REGISTERED", path, "No coordinator registration exists.")]
    try:
        registration = read_coordinator(path)
    except CoordinatorError as error:
        return [_error(error.code, path, str(error))]
    if Path(registration.project_root).resolve() == project_root.resolve():
        return []
    return [
        _error(
            "COORDINATOR_PROJECT_MISMATCH",
            path,
            f"Registered project '{registration.project_root}' does not match '{project_root.resolve()}'.",
        )
    ]


def _validate_attempt(item: QueueItem, work_root: Path) -> list[Diagnostic]:
    if item.state in {WorkState.ACTIVE, WorkState.PAUSED} and item.attempt is None:
        return [_error("QUEUE_ATTEMPT_MISSING", work_root / "queue.md", f"Item '{item.item}' needs an attempt.")]
    if item.attempt is None:
        return []
    if item.state in {WorkState.READY, WorkState.INTAKE, WorkState.DEFERRED}:
        return [
            _error("QUEUE_ATTEMPT_UNEXPECTED", work_root / "queue.md", f"Item '{item.item}' cannot name an attempt.")
        ]
    path = work_root / "attempts" / item.attempt / "attempt.md"
    if not path.is_file():
        return [_error("ATTEMPT_RECORD_MISSING", path, "Queue attempt record is missing.")]
    try:
        attempt = parse_attempt(path)
    except ParseError as error:
        return [_parse_error(error)]
    matches = attempt.item == item.item and attempt.attempt == item.attempt and attempt.state == item.state.value
    return [] if matches else [_error("ATTEMPT_QUEUE_MISMATCH", path, "Attempt disagrees with queue.md.")]


def _validate_attempts(queue: Queue, work_root: Path) -> list[Diagnostic]:
    return [diagnostic for item in queue.items for diagnostic in _validate_attempt(item, work_root)]


def _validate_current(queue: Queue, work_root: Path) -> list[Diagnostic]:
    path = work_root / "current.md"
    try:
        current = parse_current(path)
    except OSError as error:
        return [_error("CURRENT_UNREADABLE", path, str(error))]
    except ParseError as error:
        return [_parse_error(error)]
    if current.focus_item is None:
        return (
            [_error("CURRENT_ATTEMPT_WITHOUT_ITEM", current.path, "A focus attempt requires a focus item.")]
            if current.focus_attempt is not None
            else []
        )
    active = {item.item: item for item in queue.items if item.state == WorkState.ACTIVE}
    focused = active.get(current.focus_item)
    if focused is None:
        return [_error("CURRENT_FOCUS_MISMATCH", current.path, f"Focused item '{current.focus_item}' is not active.")]
    if current.focus_attempt != focused.attempt:
        return [_error("CURRENT_FOCUS_MISMATCH", current.path, "Focused attempt disagrees with the queue attempt.")]
    return []


def _validate_no_pending_transaction(work_root: Path) -> list[Diagnostic]:
    from repo_work.transaction_store import journal_path_for

    journal = journal_path_for(work_root)
    if not journal.exists():
        return []
    return [
        _error("COMMIT_RECOVERY_REQUIRED", journal, "A prior transition journal requires recovery before mutation.")
    ]


def _validate_work_state(work_root: Path, project_root: Path, *, check_pending: bool) -> ValidationReport:
    queue, diagnostics = _read_queue(work_root)
    if check_pending:
        diagnostics.extend(_validate_no_pending_transaction(work_root))
    if queue is None:
        return ValidationReport(tuple(diagnostics))
    diagnostics.extend(_validate_item_records(queue, work_root))
    diagnostics.extend(_validate_dependencies(queue, work_root))
    diagnostics.extend(_validate_coordinator(work_root, project_root))
    diagnostics.extend(_validate_attempts(queue, work_root))
    diagnostics.extend(_validate_current(queue, work_root))
    return ValidationReport(tuple(diagnostics))


def validate_work_state(work_root: Path, project_root: Path) -> ValidationReport:
    return _validate_work_state(work_root, project_root, check_pending=True)


def validate_work_state_during_commit(work_root: Path, project_root: Path) -> ValidationReport:
    return _validate_work_state(work_root, project_root, check_pending=False)
