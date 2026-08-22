import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Final, assert_never

from repo_work.identifiers import AttemptId, ItemId, ResourceId
from repo_work.model import (
    SCHEMA_V1,
    SCHEMA_V2,
    TERMINAL_STATES,
    AttemptState,
    WorkState,
)

type HeaderValue = str | bool | None
type Header = dict[str, HeaderValue]


@dataclass(frozen=True, slots=True)
class QueueItem:
    item: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt: str | None
    source: str
    next_action: str | None
    notes: str
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class Queue:
    path: Path
    header: Header
    items: tuple[QueueItem, ...]
    revision: str

    def by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    path: Path
    item: str
    user_label: str
    queue_item: QueueItem | None = None
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    path: Path
    focus_item: str | None
    focus_attempt: str | None
    next_action: str


@dataclass(frozen=True, slots=True)
class Attempt:
    path: Path
    attempt: str
    item: str
    state: AttemptState
    branch: str
    base_revision: str
    provenance: str


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


class V2HeaderSentinel(Enum):
    EMPTY = "—"


type V2HeaderValue = str | int | bool | V2HeaderSentinel | None


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


def _scalar(raw: str) -> HeaderValue:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if value in {"null", "~"}:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def encode_string_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def serialize_v2_header_value(value: V2HeaderValue) -> str:
    match value:
        case V2HeaderSentinel():
            return value.value
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case str():
            return encode_string_scalar(value)
        case _ as unreachable:
            assert_never(unreachable)


def serialize_v2_header_fields(fields: Mapping[str, V2HeaderValue]) -> dict[str, str]:
    return {key: serialize_v2_header_value(value) for key, value in fields.items()}


def render_v2_header(fields: Mapping[str, V2HeaderValue]) -> str:
    serialized = serialize_v2_header_fields(fields)
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in serialized.items()) + "---\n"


def parse_header(path: Path) -> Header:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ParseError("HEADER_MISSING", path, "The file must start with '---'.", 1)

    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ParseError("HEADER_UNTERMINATED", path, "The header has no closing '---'.") from error

    header: Header = {}
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


def _parse_queue_row(
    cells: list[str],
    path: Path,
    line: int,
    identities: set[ItemId],
    *,
    schema: str | bool | None,
) -> QueueItem:
    if len(cells) != len(QUEUE_COLUMNS):
        raise ParseError("QUEUE_ROW_COLUMNS", path, f"Expected {len(QUEUE_COLUMNS)} cells, found {len(cells)}.", line)
    item_id, state_value, timing, dependencies, attempt, source, next_action, notes = cells
    if not ITEM_PATTERN.fullmatch(item_id):
        raise ParseError("QUEUE_ITEM_INVALID", path, f"Invalid item identity '{item_id}'.", line)
    if item_id in identities:
        raise ParseError("QUEUE_ITEM_DUPLICATE", path, f"Duplicate item '{item_id}'.", line)
    identities.add(ItemId(item_id))
    if state_value in TERMINAL_STATES:
        raise ParseError("QUEUE_TERMINAL_STATE", path, f"Terminal state '{state_value}' is not nonterminal.", line)
    try:
        state = WorkState(state_value)
    except ValueError as error:
        raise ParseError("QUEUE_STATE_INVALID", path, f"Unknown state '{state_value}'.", line) from error
    if schema == SCHEMA_V2:
        depends_on = (
            ()
            if dependencies == "—"
            else tuple(ItemId(dependency.strip()) for dependency in dependencies.split(",") if dependency.strip())
        )
        parsed_timing = _generated_optional(timing, "—")
        attempt_value = _generated_optional(attempt, "—")
        parsed_attempt = AttemptId(attempt_value) if attempt_value is not None else None
        parsed_next_action = _generated_optional(next_action, "—")
    else:
        depends_on = tuple(
            ItemId(dependency.strip())
            for dependency in dependencies.split(",")
            if _optional(dependency.strip()) is not None
        )
        parsed_timing = _optional(timing)
        attempt_value = _optional(attempt)
        parsed_attempt = AttemptId(attempt_value) if attempt_value is not None else None
        parsed_next_action = _optional(next_action)
    return QueueItem(
        item=ItemId(item_id),
        state=state,
        timing=parsed_timing,
        depends_on=depends_on,
        attempt=parsed_attempt,
        source=source,
        next_action=parsed_next_action,
        notes=notes,
    )


def parse_queue(path: Path) -> Queue:
    raw = path.read_bytes()
    return _parse_queue_text(raw.decode("utf-8"), path, parse_header(path), hashlib.sha256(raw).hexdigest())


def parse_queue_text(text: str, path: Path, revision: str = "") -> Queue:
    return _parse_queue_text(text, path, parse_header_text(text), revision)


def _parse_queue_text(text: str, path: Path, header: Header, revision: str) -> Queue:
    lines = text.splitlines()
    table_start = _queue_table_start(lines, path)
    items: list[QueueItem] = []
    identities: set[ItemId] = set()
    for index in range(table_start + 2, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line.lstrip().startswith("|"):
            break
        items.append(_parse_queue_row(_cells(line), path, index + 1, identities, schema=header.get("schema")))

    return Queue(
        path=path,
        header=header,
        items=tuple(items),
        revision=revision,
    )


def _required_string(header: Header, path: Path, field: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value:
        raise ParseError("HEADER_FIELD_REQUIRED", path, f"Header field '{field}' must be a non-empty string.")
    return value


def _present_string(header: Header, path: Path, field: str) -> str:
    value = header.get(field)
    if not isinstance(value, str):
        raise ParseError("HEADER_FIELD_REQUIRED", path, f"Header field '{field}' must be a present string.")
    return value


def _generated_optional(value: str, missing: str) -> str | None:
    return None if value == missing else value


def _generated_string_list(value: str) -> tuple[str, ...]:
    if value == "—":
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def require_document_header(path: Path, expected_kind: str, expected_schema: str = SCHEMA_V1) -> Header:
    header = parse_header(path)
    if header.get("kind") != expected_kind:
        raise ParseError(
            "DOCUMENT_KIND_INVALID",
            path,
            f"Expected kind '{expected_kind}', found '{header.get('kind')}'.",
        )
    if header.get("schema") != expected_schema:
        raise ParseError(
            "DOCUMENT_SCHEMA_INVALID",
            path,
            f"Expected schema '{expected_schema}', found '{header.get('schema')}'.",
        )
    return header


def parse_item(path: Path) -> WorkItemRecord:
    header = parse_header(path)
    schema = header.get("schema")
    if header.get("kind") != "work-item":
        raise ParseError("DOCUMENT_KIND_INVALID", path, "Expected kind 'work-item'.")
    if schema not in {SCHEMA_V1, SCHEMA_V2}:
        raise ParseError("DOCUMENT_SCHEMA_INVALID", path, "Expected a supported repo-work item schema.")
    item = _required_string(header, path, "item")
    if not ITEM_PATTERN.fullmatch(item):
        raise ParseError("ITEM_ID_INVALID", path, f"Invalid item identity '{item}'.")
    if schema == SCHEMA_V1:
        return WorkItemRecord(path=path, item=ItemId(item), user_label=_required_string(header, path, "user_label"))
    state_value = _required_string(header, path, "state")
    try:
        state = WorkState(state_value)
    except ValueError as error:
        raise ParseError("ITEM_STATE_INVALID", path, f"Unknown state '{state_value}'.") from error
    depends_on = tuple(ItemId(value) for value in _generated_string_list(_required_string(header, path, "depends_on")))
    resources_value = _required_string(header, path, "resources")
    resources = tuple(ResourceId(value) for value in _generated_string_list(resources_value))
    if len(resources) != len(set(resources)):
        raise ParseError("ITEM_RESOURCES_DUPLICATE", path, "resources must not contain duplicate identities.")
    queue_item = QueueItem(
        item=ItemId(item),
        state=state,
        timing=_generated_optional(_required_string(header, path, "timing"), "—"),
        depends_on=depends_on,
        attempt=(
            AttemptId(value)
            if (value := _generated_optional(_required_string(header, path, "attempt"), "—")) is not None
            else None
        ),
        source=_present_string(header, path, "source"),
        next_action=_generated_optional(_required_string(header, path, "next_action"), "—"),
        notes=_present_string(header, path, "notes"),
    )
    return WorkItemRecord(path, ItemId(item), _required_string(header, path, "user_label"), queue_item, resources)


def parse_current(path: Path) -> CurrentPointer:
    header = parse_header(path)
    if header.get("kind") != "work-current" or header.get("schema") not in {SCHEMA_V1, SCHEMA_V2}:
        raise ParseError("DOCUMENT_SCHEMA_INVALID", path, "Expected a repo-work current document.")
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
        focus_item=ItemId(focus_item) if focus_item is not None else None,
        focus_attempt=AttemptId(focus_attempt) if focus_attempt is not None else None,
        next_action=_required_string(header, path, "next_action"),
    )


def parse_attempt(path: Path) -> Attempt:
    header = parse_header(path)
    schema = header.get("schema")
    if header.get("kind") != "work-attempt" or schema not in {SCHEMA_V1, SCHEMA_V2}:
        raise ParseError("DOCUMENT_SCHEMA_INVALID", path, "Expected a repo-work attempt document.")
    attempt = _required_string(header, path, "attempt")
    item = _required_string(header, path, "item")
    state = _required_string(header, path, "state")
    if not ITEM_PATTERN.fullmatch(attempt):
        raise ParseError("ATTEMPT_ID_INVALID", path, f"Invalid attempt identity '{attempt}'.")
    if not ITEM_PATTERN.fullmatch(item):
        raise ParseError("ATTEMPT_ITEM_INVALID", path, f"Invalid item identity '{item}'.")
    try:
        attempt_state = AttemptState(state)
    except ValueError as error:
        raise ParseError("ATTEMPT_STATE_INVALID", path, f"Invalid attempt state '{state}'.") from error
    if schema == SCHEMA_V2 and "owner" in header:
        raise ParseError(
            "ATTEMPT_STATIC_OWNER_INVALID",
            path,
            "Schema-v2 ownership comes only from the renewable lease fields; use provenance for non-authoritative origin.",
        )
    provenance_field = "owner" if schema == SCHEMA_V1 else "provenance"
    return Attempt(
        path=path,
        attempt=AttemptId(attempt),
        item=ItemId(item),
        state=attempt_state,
        branch=_required_string(header, path, "branch"),
        base_revision=_required_string(header, path, "base_revision"),
        provenance=_required_string(header, path, provenance_field),
    )


def render_queue(_queue: Queue, items: tuple[QueueItem, ...], schema: str | None = None) -> str:
    updated = date.today().isoformat()
    selected_schema = schema or (
        _queue.header.get("schema") if isinstance(_queue.header.get("schema"), str) else SCHEMA_V1
    )
    header = (
        render_v2_header({"kind": "work-queue", "schema": selected_schema, "updated": updated}).splitlines()
        if selected_schema == SCHEMA_V2
        else ["---", "kind: work-queue", f"schema: {selected_schema}", f'updated: "{updated}"', "---"]
    )
    lines = [
        *header,
        "",
        "# Work Queue",
        "",
        *(
            ["Generated from `items/*.md`; edit the item files, not this overview.", ""]
            if selected_schema == SCHEMA_V2
            else []
        ),
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
            item.next_action or ("—" if selected_schema == SCHEMA_V2 else "none"),
            item.notes,
        )
        if any("|" in value or "\n" in value for value in values):
            raise ValueError(f"QUEUE_CELL_UNSAFE: item '{item.item}' contains a pipe or newline.")
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def render_current(focus_item: str | None, focus_attempt: str | None, next_action: str, schema: str = SCHEMA_V1) -> str:
    updated = date.today().isoformat()
    if schema == SCHEMA_V2:
        return (
            render_v2_header(
                {
                    "kind": "work-current",
                    "schema": schema,
                    "updated": updated,
                    "focus_item": focus_item,
                    "focus_attempt": focus_attempt,
                    "next_action": next_action,
                }
            )
            + "\n# Current Work\n"
        )
    return (
        "---\n"
        "kind: work-current\n"
        f"schema: {schema}\n"
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


def replace_v2_header_fields(
    text: str,
    replacements: Mapping[str, V2HeaderValue],
    additions: Mapping[str, V2HeaderValue] | None = None,
) -> str:
    existing = parse_header_text(text)
    missing = set(replacements) - set(existing)
    if missing:
        raise ValueError(f"HEADER_FIELD_REQUIRED: cannot replace {', '.join(sorted(missing))}")
    values: dict[str, V2HeaderValue] = dict(existing)
    values.update(replacements)
    replacement_values = serialize_v2_header_fields({key: values[key] for key in existing})
    addition_values = serialize_v2_header_fields(
        {key: value for key, value in (additions or {}).items() if key not in existing}
    )
    return replace_header_fields(text, replacement_values, addition_values)


def remove_header_fields(text: str, fields: frozenset[str]) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("HEADER_MISSING: cannot remove fields")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("HEADER_UNTERMINATED: cannot remove fields") from error
    found: set[str] = set()
    kept = [lines[0]]
    for line in lines[1:end]:
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in fields:
            found.add(key)
        else:
            kept.append(line)
    missing = fields - found
    if missing:
        raise ValueError(f"HEADER_FIELD_REQUIRED: cannot remove {', '.join(sorted(missing))}")
    return "\n".join((*kept, *lines[end:])) + "\n"


def render_v2_item(text: str, item: QueueItem, resources: tuple[str, ...] = ()) -> str:
    values = {
        "kind": "work-item",
        "schema": SCHEMA_V2,
        "item": item.item,
        "state": item.state.value,
        "timing": item.timing if item.timing is not None else V2HeaderSentinel.EMPTY,
        "depends_on": ", ".join(item.depends_on) if item.depends_on else V2HeaderSentinel.EMPTY,
        "attempt": item.attempt if item.attempt is not None else V2HeaderSentinel.EMPTY,
        "source": item.source,
        "next_action": item.next_action if item.next_action is not None else V2HeaderSentinel.EMPTY,
        "notes": item.notes,
        "resources": ", ".join(resources) if resources else V2HeaderSentinel.EMPTY,
    }
    existing = parse_header_text(text)
    replacements = {key: value for key, value in values.items() if key in existing}
    additions = {key: value for key, value in values.items() if key not in existing}
    return replace_v2_header_fields(text, replacements, additions)


def parse_header_text(text: str) -> Header:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("HEADER_MISSING: cannot parse fields")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("HEADER_UNTERMINATED: cannot parse fields") from error
    result: Header = {}
    for line in lines[1:end]:
        if ":" in line:
            key, raw = line.split(":", 1)
            result[key.strip()] = _scalar(raw)
    return result
