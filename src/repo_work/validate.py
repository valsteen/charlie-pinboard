import json
from dataclasses import dataclass
from pathlib import Path

from repo_work.diagnostics import Diagnostic, Severity
from repo_work.markdown import (
    ParseError,
    parse_attempt,
    parse_current,
    parse_item,
    parse_queue,
    require_document_header,
)
from repo_work.model import SCHEMA_V1, Queue, WorkState


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


def _validate_dependencies(queue: Queue, work_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    by_id = queue.by_id()
    completed: set[str] = set()
    history_root = work_root / "history" / "items"
    if history_root.is_dir():
        for path in history_root.glob("*.md"):
            try:
                header = require_document_header(path, "work-history")
            except ParseError as error:
                diagnostics.append(_parse_error(error))
                continue
            item = header.get("item")
            if isinstance(item, str) and header.get("state") == "done":
                completed.add(item)

    graph: dict[str, tuple[str, ...]] = {}
    for item in queue.items:
        graph[item.item] = tuple(dependency for dependency in item.depends_on if dependency in by_id)
        for dependency in item.depends_on:
            if dependency not in by_id and dependency not in completed:
                diagnostics.append(
                    _error(
                        "DEPENDENCY_UNKNOWN",
                        queue.path,
                        f"Item '{item.item}' depends on unknown item '{dependency}'.",
                    )
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str, path: tuple[str, ...]) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            cycle = (*path[path.index(item_id) :], item_id)
            diagnostics.append(_error("DEPENDENCY_CYCLE", queue.path, f"Dependency cycle: {' -> '.join(cycle)}."))
            return
        visiting.add(item_id)
        for dependency in graph.get(item_id, ()):
            visit(dependency, (*path, dependency))
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in graph:
        visit(item_id, (item_id,))
    return diagnostics


def _validate_coordinator(work_root: Path, project_root: Path) -> list[Diagnostic]:
    path = work_root / "coordinator.json"
    if not path.is_file():
        return [_error("COORDINATOR_NOT_REGISTERED", path, "No coordinator registration exists.")]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [_error("COORDINATOR_INVALID", path, f"Cannot parse coordinator registration: {error}")]
    diagnostics: list[Diagnostic] = []
    if value.get("schema") != SCHEMA_V1:
        diagnostics.append(_error("COORDINATOR_SCHEMA_INVALID", path, "Unsupported coordinator schema."))
    registered_root = value.get("project_root")
    if not isinstance(registered_root, str) or Path(registered_root).resolve() != project_root.resolve():
        diagnostics.append(
            _error(
                "COORDINATOR_PROJECT_MISMATCH",
                path,
                f"Registered project '{registered_root}' does not match '{project_root.resolve()}'.",
            )
        )
    if not isinstance(value.get("task_id"), str) or not value["task_id"]:
        diagnostics.append(_error("COORDINATOR_TASK_INVALID", path, "task_id must be a non-empty string."))
    if not isinstance(value.get("generation"), int) or value["generation"] < 1:
        diagnostics.append(_error("COORDINATOR_GENERATION_INVALID", path, "generation must be a positive integer."))
    return diagnostics


def validate_work_state(work_root: Path, project_root: Path) -> ValidationReport:
    diagnostics: list[Diagnostic] = []
    queue_path = work_root / "queue.md"
    current_path = work_root / "current.md"
    try:
        queue = parse_queue(queue_path)
        if queue.header.get("kind") != "work-queue":
            diagnostics.append(_error("DOCUMENT_KIND_INVALID", queue_path, "queue.md must have kind work-queue."))
        if queue.header.get("schema") != SCHEMA_V1:
            diagnostics.append(_error("DOCUMENT_SCHEMA_INVALID", queue_path, "queue.md must use repo-work/v1."))
    except (OSError, ParseError) as error:
        if isinstance(error, ParseError):
            diagnostics.append(_parse_error(error))
        else:
            diagnostics.append(_error("QUEUE_UNREADABLE", queue_path, str(error)))
        return ValidationReport(tuple(diagnostics))

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
        for path in item_root.glob("*.md"):
            if path.stem not in queue_ids:
                diagnostics.append(
                    _error("ITEM_RECORD_ORPHANED", path, "Nonterminal item record has no canonical queue row.")
                )

    diagnostics.extend(_validate_dependencies(queue, work_root))
    diagnostics.extend(_validate_coordinator(work_root, project_root))

    for item in queue.items:
        if item.state in {WorkState.ACTIVE, WorkState.PAUSED} and item.attempt is None:
            diagnostics.append(
                _error(
                    "QUEUE_ATTEMPT_MISSING", queue.path, f"Item '{item.item}' in state '{item.state}' needs an attempt."
                )
            )
            continue
        if item.attempt is None:
            continue
        if item.state in {WorkState.READY, WorkState.INTAKE, WorkState.DEFERRED}:
            diagnostics.append(
                _error(
                    "QUEUE_ATTEMPT_UNEXPECTED",
                    queue.path,
                    f"Item '{item.item}' in state '{item.state}' cannot name an attempt.",
                )
            )
            continue
        attempt_path = work_root / "attempts" / item.attempt / "attempt.md"
        if not attempt_path.is_file():
            diagnostics.append(_error("ATTEMPT_RECORD_MISSING", attempt_path, "Queue attempt record is missing."))
            continue
        try:
            attempt = parse_attempt(attempt_path)
        except ParseError as error:
            diagnostics.append(_parse_error(error))
            continue
        if attempt.item != item.item or attempt.attempt != item.attempt or attempt.state != item.state.value:
            diagnostics.append(
                _error(
                    "ATTEMPT_QUEUE_MISMATCH",
                    attempt_path,
                    "Attempt identity, item, or state disagrees with queue.md.",
                )
            )

    try:
        current = parse_current(current_path)
    except (OSError, ParseError) as error:
        if isinstance(error, ParseError):
            diagnostics.append(_parse_error(error))
        else:
            diagnostics.append(_error("CURRENT_UNREADABLE", current_path, str(error)))
        return ValidationReport(tuple(diagnostics))

    active = {item.item: item for item in queue.items if item.state == WorkState.ACTIVE}
    if current.focus_item is None:
        if current.focus_attempt is not None:
            diagnostics.append(
                _error("CURRENT_ATTEMPT_WITHOUT_ITEM", current.path, "A focus attempt requires a focus item.")
            )
    else:
        focused = active.get(current.focus_item)
        if focused is None:
            diagnostics.append(
                _error(
                    "CURRENT_FOCUS_MISMATCH",
                    current.path,
                    f"Focused item '{current.focus_item}' is not active in queue.md.",
                )
            )
        elif current.focus_attempt != focused.attempt:
            diagnostics.append(
                _error(
                    "CURRENT_FOCUS_MISMATCH",
                    current.path,
                    f"Focused attempt '{current.focus_attempt}' disagrees with queue attempt '{focused.attempt}'.",
                )
            )
    return ValidationReport(tuple(diagnostics))
