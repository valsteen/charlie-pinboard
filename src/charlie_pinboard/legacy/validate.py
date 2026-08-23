from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from charlie_pinboard.adapters.files.artifacts import ArtifactError, verify_reference
from charlie_pinboard.adapters.sqlite.database import StorageError
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.domain.model import SCHEMA_V1, SCHEMA_V2, WorkState
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority
from charlie_pinboard.legacy.coordinator import CoordinatorError, read_coordinator
from charlie_pinboard.legacy.diagnostics import Diagnostic, Severity
from charlie_pinboard.legacy.leases import LeaseError, read_attempt_lease, read_coordination_lease
from charlie_pinboard.legacy.markdown import (
    ParseError,
    Queue,
    QueueItem,
    parse_attempt,
    parse_current,
    parse_item,
    parse_queue,
    require_document_header,
)
from charlie_pinboard.legacy.resources import ResourceError, read_resource, read_resource_claim
from charlie_pinboard.legacy.storage_layout import journal_path_for


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


def _read_queue(work_root: Path, schema: str) -> tuple[Queue | None, list[Diagnostic]]:
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
    if queue.header.get("schema") != schema:
        diagnostics.append(_error("DOCUMENT_SCHEMA_INVALID", path, f"queue.md must use {schema}."))
    return queue, diagnostics


def _validate_item_records(queue: Queue, work_root: Path, schema: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    item_root = work_root / "items"
    queue_ids = {item.item for item in queue.items}
    for item in queue.items:
        path = item_root / f"{item.item}.md"
        if not path.is_file():
            diagnostics.append(_error("ITEM_RECORD_MISSING", path, f"No record exists for '{item.item}'."))
            continue
        try:
            require_document_header(path, "work-item", schema)
            record = parse_item(path)
        except ParseError as error:
            diagnostics.append(_parse_error(error))
            continue
        if record.item != item.item:
            diagnostics.append(
                _error("ITEM_RECORD_MISMATCH", path, f"Record names '{record.item}', expected '{item.item}'.")
            )
        if schema == SCHEMA_V2 and record.queue_item != item:
            diagnostics.append(
                _error(
                    "QUEUE_VIEW_STALE", path, f"Generated queue row for '{item.item}' disagrees with its item record."
                )
            )
    if item_root.is_dir():
        diagnostics.extend(
            _error("ITEM_RECORD_ORPHANED", path, "Nonterminal item record has no canonical queue row.")
            for path in item_root.glob("*.md")
            if path.stem not in queue_ids
        )
    return diagnostics


def _completed_items(work_root: Path, schema: str) -> tuple[set[str], list[Diagnostic]]:
    completed: set[str] = set()
    diagnostics: list[Diagnostic] = []
    history_root = work_root / "history" / "items"
    if not history_root.is_dir():
        return completed, diagnostics
    for path in history_root.glob("*.md"):
        try:
            header = require_document_header(path, "work-history", schema)
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


def _validate_dependencies(queue: Queue, work_root: Path, schema: str) -> list[Diagnostic]:
    completed, diagnostics = _completed_items(work_root, schema)
    graph, graph_diagnostics = _dependency_graph(queue, completed)
    diagnostics.extend(graph_diagnostics)
    diagnostics.extend(_dependency_cycles(graph, queue.path))
    return diagnostics


def _validate_live_dependencies(queue: Queue, work_root: Path, schema: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    live_ids = frozenset(queue.by_id())
    completed: set[str] = set()
    referenced_history = {
        dependency for item in queue.items for dependency in item.depends_on if dependency not in live_ids
    }
    for dependency in referenced_history:
        path = work_root / "history" / "items" / f"{dependency}.md"
        try:
            header = require_document_header(path, "work-history", schema)
        except OSError:
            continue
        except ParseError as error:
            diagnostics.append(_parse_error(error))
            continue
        if header.get("item") == dependency and header.get("state") == "done":
            completed.add(dependency)
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


def _validate_attempt(item: QueueItem, work_root: Path, schema: str) -> list[Diagnostic]:
    if item.state in {WorkState.ACTIVE, WorkState.PAUSED, WorkState.REVIEW} and item.attempt is None:
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
        require_document_header(path, "work-attempt", schema)
        attempt = parse_attempt(path)
    except ParseError as error:
        return [_parse_error(error)]
    matches = attempt.item == item.item and attempt.attempt == item.attempt and attempt.state.value == item.state.value
    return [] if matches else [_error("ATTEMPT_QUEUE_MISMATCH", path, "Attempt disagrees with queue.md.")]


def _validate_attempts(queue: Queue, work_root: Path, schema: str) -> list[Diagnostic]:
    return [diagnostic for item in queue.items for diagnostic in _validate_attempt(item, work_root, schema)]


def _validate_current(queue: Queue, work_root: Path, schema: str) -> list[Diagnostic]:
    path = work_root / "current.md"
    try:
        require_document_header(path, "work-current", schema)
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
    journal = journal_path_for(work_root)
    if not journal.exists():
        return []
    return [
        _error("COMMIT_RECOVERY_REQUIRED", journal, "A prior transition journal requires recovery before mutation.")
    ]


def _validate_v2_item_records(queue: Queue, work_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for item in queue.items:
        if item.attempt is not None:
            try:
                read_attempt_lease(work_root, item.attempt)
            except (LeaseError, ParseError, OSError, ValueError) as error:
                diagnostics.append(
                    _error("ATTEMPT_LEASE_INVALID", work_root / "attempts" / item.attempt / "attempt.md", str(error))
                )
        try:
            record = parse_item(work_root / "items" / f"{item.item}.md")
        except ParseError, OSError:
            continue
        for resource in record.resources:
            try:
                read_resource(work_root, resource)
            except ResourceError as error:
                diagnostics.append(_error(error.code, work_root / "resources" / f"{resource}.md", str(error)))
    return diagnostics


def _validate_v2_resource_records(work_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    resource_root = work_root / "resources"
    for path in resource_root.glob("*.md") if resource_root.is_dir() else ():
        try:
            read_resource(work_root, path.stem)
        except ResourceError as error:
            diagnostics.append(_error(error.code, path, str(error)))
    claim_root = work_root / "leases" / "resources"
    for path in claim_root.glob("*.md") if claim_root.is_dir() else ():
        separator = path.stem.find("--")
        if separator < 1:
            diagnostics.append(
                _error("RESOURCE_CLAIM_INVALID", path, "Claim filename must identify resource and host.")
            )
            continue
        try:
            read_resource_claim(work_root, path.stem[:separator], path.stem[separator + 2 :])
        except ResourceError as error:
            diagnostics.append(_error(error.code, path, str(error)))
    return diagnostics


def _validate_v2_records(queue: Queue, work_root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    marker = work_root / "migration-complete.md"
    if not marker.is_file():
        diagnostics.append(
            _error("MIGRATION_INCOMPLETE", marker, "Schema-v2 authority is missing its completion marker.")
        )
    else:
        try:
            require_document_header(marker, "migration-complete", SCHEMA_V2)
        except ParseError as error:
            diagnostics.append(_parse_error(error))
    coordinator = work_root / "coordinator.json"
    if coordinator.exists():
        diagnostics.append(
            _error(
                "V1_COORDINATOR_UNEXPECTED",
                coordinator,
                "Schema-v2 authority must not retain an active v1 coordinator registration.",
            )
        )
    coordination = work_root / "leases" / "coordination.md"
    if coordination.is_file():
        try:
            read_coordination_lease(work_root)
        except LeaseError as error:
            diagnostics.append(_error(error.code, coordination, str(error)))
    diagnostics.extend(_validate_v2_item_records(queue, work_root))
    diagnostics.extend(_validate_v2_resource_records(work_root))
    return diagnostics


def _validate_work_state(
    work_root: Path,
    project_root: Path,
    version: AuthorityVersion,
    *,
    check_pending: bool,
) -> ValidationReport:
    match version:
        case AuthorityVersion.V1:
            schema = SCHEMA_V1
        case AuthorityVersion.V2:
            schema = SCHEMA_V2
        case _ as unreachable:
            assert_never(unreachable)
    queue, diagnostics = _read_queue(work_root, schema)
    if check_pending:
        diagnostics.extend(_validate_no_pending_transaction(work_root))
    if queue is None:
        return ValidationReport(tuple(diagnostics))
    diagnostics.extend(_validate_item_records(queue, work_root, schema))
    diagnostics.extend(_validate_dependencies(queue, work_root, schema))
    if schema == SCHEMA_V1:
        diagnostics.extend(_validate_coordinator(work_root, project_root))
    diagnostics.extend(_validate_attempts(queue, work_root, schema))
    diagnostics.extend(_validate_current(queue, work_root, schema))
    if schema == SCHEMA_V2:
        diagnostics.extend(_validate_v2_records(queue, work_root))
    return ValidationReport(tuple(diagnostics))


def validate_work_state(work_root: Path, project_root: Path) -> ValidationReport:
    authority = resolve_authority(work_root)
    return _validate_work_state(authority.work_root, project_root, authority.version, check_pending=True)


def validate_sqlite_work_state(work_root: Path) -> ValidationReport:
    """Validate current SQLite authority and immutable artifacts without consulting generated views."""

    database = work_root / "state.sqlite3"
    try:
        state = SQLiteWorkStore(database).snapshot()
    except StorageError as error:
        return ValidationReport((_error(error.code.value, database, str(error)),))
    diagnostics: list[Diagnostic] = []
    for reference in state.artifacts.references:
        try:
            verify_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error(error.code, work_root / reference.selector, str(error)))
    view_root = work_root / "views"
    for selector in ("queue.md", "current.md", "history.md"):
        path = view_root / selector
        if not path.is_file():
            diagnostics.append(
                Diagnostic(
                    "VIEW_REFRESH_REQUIRED",
                    Severity.WARNING,
                    path,
                    "Generated view is absent; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    return ValidationReport(tuple(diagnostics))


def validate_live_work_state(work_root: Path, project_root: Path) -> ValidationReport:
    authority = resolve_authority(work_root)
    root = authority.work_root
    schema = SCHEMA_V2 if authority.version == AuthorityVersion.V2 else SCHEMA_V1
    queue, diagnostics = _read_queue(root, schema)
    diagnostics.extend(_validate_no_pending_transaction(root))
    if queue is None:
        return ValidationReport(tuple(diagnostics))
    diagnostics.extend(_validate_item_records(queue, root, schema))
    diagnostics.extend(_validate_live_dependencies(queue, root, schema))
    if authority.version == AuthorityVersion.V1:
        diagnostics.extend(_validate_coordinator(root, project_root))
    diagnostics.extend(_validate_attempts(queue, root, schema))
    diagnostics.extend(_validate_current(queue, root, schema))
    if authority.version == AuthorityVersion.V2:
        marker = root / "migration-complete.md"
        if not marker.is_file():
            diagnostics.append(_error("MIGRATION_INCOMPLETE", marker, "Schema-v2 authority is incomplete."))
        else:
            try:
                require_document_header(marker, "migration-complete", SCHEMA_V2)
            except ParseError as error:
                diagnostics.append(_parse_error(error))
        coordinator = root / "coordinator.json"
        if coordinator.exists():
            diagnostics.append(
                _error(
                    "V1_COORDINATOR_UNEXPECTED",
                    coordinator,
                    "Schema-v2 authority must not retain an active v1 coordinator registration.",
                )
            )
        diagnostics.extend(_validate_v2_item_records(queue, root))
    return ValidationReport(tuple(diagnostics))


def validate_v2_shadow(work_root: Path, project_root: Path) -> ValidationReport:
    return _validate_work_state(work_root, project_root, AuthorityVersion.V2, check_pending=True)


def validate_work_state_during_commit(
    work_root: Path,
    project_root: Path,
    version: AuthorityVersion,
) -> ValidationReport:
    return _validate_work_state(work_root, project_root, version, check_pending=False)
