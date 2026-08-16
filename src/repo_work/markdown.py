import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Final

from repo_work.model import (
    SCHEMA_V1,
    TERMINAL_STATES,
    Attempt,
    CurrentPointer,
    Queue,
    QueueItem,
    WorkItemRecord,
    WorkState,
)

ITEM_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
QUEUE_COLUMNS = (
    "Item",
    "State",
    "Timing",
    "Depends on",
    "Attempt",
    "Source",
    "Next action",
    "Reopen when / notes",
)
EMPTY_CELLS: Final = frozenset({"", "—", "-", "none", "null"})


class ParseError(ValueError):
    code: str
    path: Path
    line: int | None

    def __init__(self, code: str, path: Path, message: str, line: int | None = None) -> None:
        self.code = code
        self.path = path
        self.line = line
        location = f"{path}:{line}" if line is not None else str(path)
        super().__init__(f"{code} {location}: {message}")


def _scalar(raw: str) -> object:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value in {"null", "~"}:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def parse_header(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ParseError("HEADER_MISSING", path, "The file must start with '---'.", 1)

    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ParseError("HEADER_UNTERMINATED", path, "The header has no closing '---'.") from error

    header: dict[str, object] = {}
    for index, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ParseError("HEADER_FIELD_INVALID", path, "Expected 'name: value'.", index)
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ParseError("HEADER_FIELD_INVALID", path, "Header key is empty.", index)
        if key in header:
            raise ParseError("HEADER_FIELD_DUPLICATE", path, f"Duplicate header field '{key}'.", index)
        header[key] = _scalar(raw_value)
    return header


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _optional(cell: str) -> str | None:
    return None if cell.casefold() in EMPTY_CELLS else cell


def _queue_table_start(lines: list[str], path: Path) -> int:
    start = next((index for index, line in enumerate(lines) if tuple(_cells(line)) == QUEUE_COLUMNS), None)
    if start is None:
        raise ParseError("QUEUE_TABLE_MISSING", path, "The canonical queue table was not found.")
    if start + 1 >= len(lines) or len(_cells(lines[start + 1])) != len(QUEUE_COLUMNS):
        raise ParseError("QUEUE_SEPARATOR_MISSING", path, "The queue table separator is missing.", start + 2)
    return start


def _parse_queue_row(cells: list[str], path: Path, line: int, identities: set[str]) -> QueueItem:
    if len(cells) != len(QUEUE_COLUMNS):
        raise ParseError("QUEUE_ROW_COLUMNS", path, f"Expected {len(QUEUE_COLUMNS)} cells, found {len(cells)}.", line)
    item_id, state_value, timing, dependencies, attempt, source, next_action, notes = cells
    if not ITEM_PATTERN.fullmatch(item_id):
        raise ParseError("QUEUE_ITEM_INVALID", path, f"Invalid item identity '{item_id}'.", line)
    if item_id in identities:
        raise ParseError("QUEUE_ITEM_DUPLICATE", path, f"Duplicate item '{item_id}'.", line)
    identities.add(item_id)
    if state_value in TERMINAL_STATES:
        raise ParseError("QUEUE_TERMINAL_STATE", path, f"Terminal state '{state_value}' is not nonterminal.", line)
    try:
        state = WorkState(state_value)
    except ValueError as error:
        raise ParseError("QUEUE_STATE_INVALID", path, f"Unknown state '{state_value}'.", line) from error
    depends_on = tuple(
        dependency.strip() for dependency in dependencies.split(",") if _optional(dependency.strip()) is not None
    )
    return QueueItem(
        item=item_id,
        state=state,
        timing=_optional(timing),
        depends_on=depends_on,
        attempt=_optional(attempt),
        source=source,
        next_action=_optional(next_action),
        notes=notes,
    )


def parse_queue(path: Path) -> Queue:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    header = parse_header(path)
    lines = text.splitlines()
    table_start = _queue_table_start(lines, path)
    items: list[QueueItem] = []
    identities: set[str] = set()
    for index in range(table_start + 2, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line.lstrip().startswith("|"):
            break
        items.append(_parse_queue_row(_cells(line), path, index + 1, identities))

    return Queue(
        path=path,
        header=header,
        items=tuple(items),
        revision=hashlib.sha256(raw).hexdigest(),
    )


def _required_string(header: dict[str, object], path: Path, field: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value:
        raise ParseError("HEADER_FIELD_REQUIRED", path, f"Header field '{field}' must be a non-empty string.")
    return value


def require_document_header(path: Path, expected_kind: str) -> dict[str, object]:
    header = parse_header(path)
    if header.get("kind") != expected_kind:
        raise ParseError(
            "DOCUMENT_KIND_INVALID",
            path,
            f"Expected kind '{expected_kind}', found '{header.get('kind')}'.",
        )
    if header.get("schema") != SCHEMA_V1:
        raise ParseError(
            "DOCUMENT_SCHEMA_INVALID",
            path,
            f"Expected schema '{SCHEMA_V1}', found '{header.get('schema')}'.",
        )
    return header


def parse_item(path: Path) -> WorkItemRecord:
    header = require_document_header(path, "work-item")
    item = _required_string(header, path, "item")
    if not ITEM_PATTERN.fullmatch(item):
        raise ParseError("ITEM_ID_INVALID", path, f"Invalid item identity '{item}'.")
    return WorkItemRecord(
        path=path,
        item=item,
        user_label=_required_string(header, path, "user_label"),
    )


def parse_current(path: Path) -> CurrentPointer:
    header = require_document_header(path, "work-current")
    for field in ("focus_item", "focus_attempt"):
        if field not in header:
            raise ParseError("HEADER_FIELD_REQUIRED", path, f"Header field '{field}' must be present.")
    focus_item = header.get("focus_item")
    focus_attempt = header.get("focus_attempt")
    if focus_item is not None and (not isinstance(focus_item, str) or not ITEM_PATTERN.fullmatch(focus_item)):
        raise ParseError("CURRENT_ITEM_INVALID", path, "focus_item must be null or a work item identity.")
    if focus_attempt is not None and (not isinstance(focus_attempt, str) or not ITEM_PATTERN.fullmatch(focus_attempt)):
        raise ParseError("CURRENT_ATTEMPT_INVALID", path, "focus_attempt must be null or an attempt identity.")
    return CurrentPointer(
        path=path,
        focus_item=focus_item,
        focus_attempt=focus_attempt,
        next_action=_required_string(header, path, "next_action"),
    )


def parse_attempt(path: Path) -> Attempt:
    header = require_document_header(path, "work-attempt")
    attempt = _required_string(header, path, "attempt")
    item = _required_string(header, path, "item")
    state = _required_string(header, path, "state")
    if not ITEM_PATTERN.fullmatch(attempt):
        raise ParseError("ATTEMPT_ID_INVALID", path, f"Invalid attempt identity '{attempt}'.")
    if not ITEM_PATTERN.fullmatch(item):
        raise ParseError("ATTEMPT_ITEM_INVALID", path, f"Invalid item identity '{item}'.")
    if state not in {"active", "paused", "blocked", "review"}:
        raise ParseError("ATTEMPT_STATE_INVALID", path, f"Invalid attempt state '{state}'.")
    return Attempt(
        path=path,
        attempt=attempt,
        item=item,
        state=state,
        branch=_required_string(header, path, "branch"),
        base_revision=_required_string(header, path, "base_revision"),
        owner=_required_string(header, path, "owner"),
    )


def render_queue(_queue: Queue, items: tuple[QueueItem, ...]) -> str:
    updated = date.today().isoformat()
    lines = [
        "---",
        "kind: work-queue",
        f"schema: {SCHEMA_V1}",
        f'updated: "{updated}"',
        "---",
        "",
        "# Work Queue",
        "",
        "| " + " | ".join(QUEUE_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in QUEUE_COLUMNS) + " |",
    ]
    for item in items:
        values = (
            item.item,
            item.state.value,
            item.timing or "—",
            ", ".join(item.depends_on) or "—",
            item.attempt or "—",
            item.source,
            item.next_action or "none",
            item.notes,
        )
        if any("|" in value or "\n" in value for value in values):
            raise ValueError(f"QUEUE_CELL_UNSAFE: item '{item.item}' contains a pipe or newline.")
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def render_current(focus_item: str | None, focus_attempt: str | None, next_action: str) -> str:
    updated = date.today().isoformat()
    return (
        "---\n"
        "kind: work-current\n"
        f"schema: {SCHEMA_V1}\n"
        f'updated: "{updated}"\n'
        f"focus_item: {focus_item or 'null'}\n"
        f"focus_attempt: {focus_attempt or 'null'}\n"
        f"next_action: {next_action}\n"
        "---\n\n"
        "# Current Work\n"
    )


def replace_header_fields(text: str, replacements: dict[str, str], additions: dict[str, str] | None = None) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("HEADER_MISSING: cannot replace fields")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("HEADER_UNTERMINATED: cannot replace fields") from error
    found: set[str] = set()
    for index in range(1, end):
        if ":" not in lines[index]:
            continue
        key = lines[index].split(":", 1)[0].strip()
        if key in replacements:
            lines[index] = f"{key}: {replacements[key]}"
            found.add(key)
    missing = set(replacements) - found
    if missing:
        raise ValueError(f"HEADER_FIELD_REQUIRED: cannot replace {', '.join(sorted(missing))}")
    for key, value in (additions or {}).items():
        if not any(line.split(":", 1)[0].strip() == key for line in lines[1:end] if ":" in line):
            lines.insert(end, f"{key}: {value}")
            end += 1
    return "\n".join(lines) + "\n"
