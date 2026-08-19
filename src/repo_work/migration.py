import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import assert_never
from uuid import uuid4

from repo_work.atomic import atomic_write, atomic_write_text
from repo_work.authority import Authority, AuthorityVersion, authority_transaction, write_authority_selector
from repo_work.markdown import (
    parse_attempt,
    parse_current,
    parse_header_text,
    parse_queue,
    remove_header_fields,
    render_queue,
    render_v2_header,
    render_v2_item,
    replace_v2_header_fields,
)
from repo_work.model import SCHEMA_V2, Queue
from repo_work.validate import validate_v2_shadow, validate_work_state


class MigrationError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    live_items: int
    attempts: int
    proposals: int
    history_items: int
    cutover: bool


class MigrationWriteKind(Enum):
    SHADOW_WRITE = "shadow-write"
    SHADOW_RENAME = "shadow-rename"
    SELECTOR_WRITE = "selector-write"


@dataclass(frozen=True, slots=True)
class MigrationBoundary:
    kind: MigrationWriteKind
    path: str


type MigrationFailpoint = Callable[[MigrationBoundary], None]


@dataclass(slots=True)
class MigrationWriter:
    shadow: Path
    failpoint: MigrationFailpoint | None

    def _after(self, kind: MigrationWriteKind, path: Path) -> None:
        if self.failpoint is not None:
            self.failpoint(MigrationBoundary(kind, str(path)))

    def text(self, path: Path, value: str) -> None:
        atomic_write_text(path, value)
        self._after(MigrationWriteKind.SHADOW_WRITE, path.relative_to(self.shadow))

    def data(self, path: Path, value: bytes) -> None:
        atomic_write(path, value)
        self._after(MigrationWriteKind.SHADOW_WRITE, path.relative_to(self.shadow))


def _timestamp(value: datetime) -> str:
    current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _count(root: Path, pattern: str) -> int:
    return len(tuple(root.glob(pattern))) if root.is_dir() else 0


def _inventory(root: Path, cutover: bool) -> MigrationResult:
    return MigrationResult(
        live_items=_count(root / "items", "*.md"),
        attempts=_count(root / "attempts", "*/attempt.md"),
        proposals=_count(root / "inbox", "*.json"),
        history_items=_count(root / "history" / "items", "*.md"),
        cutover=cutover,
    )


def _copy_optional_tree(source: Path, destination: Path, writer: MigrationWriter) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise MigrationError("MIGRATION_INCOMPLETE", f"Migration does not follow symlink '{path}'.")
        relative = path.relative_to(source)
        if path.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            writer.data(destination / relative, path.read_bytes())


def _attempt_v2(text: str, now: datetime) -> str:
    timestamp = _timestamp(now)
    owner = parse_header_text(text).get("owner")
    if not isinstance(owner, str) or not owner:
        raise MigrationError("MIGRATION_INCOMPLETE", "A v1 attempt is missing its owner provenance.")
    without_owner = remove_header_fields(text, frozenset({"owner"}))
    return replace_v2_header_fields(
        without_owner,
        {"schema": SCHEMA_V2},
        {
            "provenance": owner,
            "owner_task_id": "unclaimed",
            "owner_host_id": "unclaimed",
            "lease_id": "unclaimed",
            "lease_generation": 0,
            "lease_acquired_at": timestamp,
            "lease_expires_at": timestamp,
            "lease_status": "released",
        },
    )


def _build_shadow(
    source: Path,
    shadow: Path,
    now: datetime,
    failpoint: MigrationFailpoint | None,
) -> MigrationResult:
    writer = MigrationWriter(shadow, failpoint)
    queue = parse_queue(source / "queue.md")
    for directory in (
        shadow / "items",
        shadow / "attempts",
        shadow / "resources",
        shadow / "leases" / "resources",
        shadow / "inbox",
        shadow / "history" / "items",
        shadow / "history" / "proposals",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    by_id = queue.by_id()
    for path in sorted((source / "items").glob("*.md")):
        item = by_id.get(path.stem)
        if item is None:
            raise MigrationError("MIGRATION_INCOMPLETE", f"Item record '{path}' has no live queue row.")
        writer.text(shadow / "items" / path.name, render_v2_item(path.read_text(encoding="utf-8"), item))

    for path in sorted((source / "attempts").rglob("*")):
        if path.is_symlink():
            raise MigrationError("MIGRATION_INCOMPLETE", f"Migration does not follow symlink '{path}'.")
        relative = path.relative_to(source / "attempts")
        destination = shadow / "attempts" / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and path.name == "attempt.md":
            writer.text(destination, _attempt_v2(path.read_text(encoding="utf-8"), now))
        elif path.is_file():
            writer.data(destination, path.read_bytes())

    _copy_optional_tree(source / "inbox", shadow / "inbox", writer)
    _copy_optional_tree(source / "history" / "proposals", shadow / "history" / "proposals", writer)
    for path in sorted((source / "history" / "items").glob("*.md")):
        text = replace_v2_header_fields(path.read_text(encoding="utf-8"), {"schema": SCHEMA_V2})
        writer.text(shadow / "history" / "items" / path.name, text)

    current = source / "current.md"
    current_text = replace_v2_header_fields(current.read_text(encoding="utf-8"), {"schema": SCHEMA_V2})
    writer.text(shadow / "current.md", current_text)
    v2_queue = Queue(shadow / "queue.md", {"schema": SCHEMA_V2}, queue.items, "")
    writer.text(shadow / "queue.md", render_queue(v2_queue, queue.items, SCHEMA_V2))
    writer.text(
        shadow / "migration-complete.md",
        render_v2_header({"kind": "migration-complete", "schema": SCHEMA_V2}) + "\n# Migration Complete\n",
    )
    return _inventory(shadow, False)


def _queue_and_items_equivalent(source: Path, shadow: Path) -> bool:
    before = parse_queue(source / "queue.md")
    after = parse_queue(shadow / "queue.md")
    before_rows = [
        (item.item, item.state, item.timing, item.depends_on, item.attempt, item.source, item.next_action, item.notes)
        for item in before.items
    ]
    after_rows = [
        (item.item, item.state, item.timing, item.depends_on, item.attempt, item.source, item.next_action, item.notes)
        for item in after.items
    ]
    if before_rows != after_rows:
        return False
    after_by_id = after.by_id()
    source_items = sorted(path.name for path in (source / "items").glob("*.md"))
    shadow_items = sorted(path.name for path in (shadow / "items").glob("*.md"))
    if source_items != shadow_items:
        return False
    for name in source_items:
        item = after_by_id.get(Path(name).stem)
        if item is None:
            return False
        expected = render_v2_item((source / "items" / name).read_text(encoding="utf-8"), item)
        if expected != (shadow / "items" / name).read_text(encoding="utf-8"):
            return False
    return True


def _current_equivalent(source: Path, shadow: Path) -> bool:
    source_current = parse_current(source / "current.md")
    shadow_current = parse_current(shadow / "current.md")
    return (
        source_current.focus_item,
        source_current.focus_attempt,
        source_current.next_action,
    ) == (
        shadow_current.focus_item,
        shadow_current.focus_attempt,
        shadow_current.next_action,
    )


def _attempts_equivalent(source: Path, shadow: Path) -> bool:
    source_attempts = sorted(
        path.relative_to(source / "attempts") for path in (source / "attempts").rglob("*") if path.is_file()
    )
    shadow_attempts = sorted(
        path.relative_to(shadow / "attempts") for path in (shadow / "attempts").rglob("*") if path.is_file()
    )
    if source_attempts != shadow_attempts:
        return False
    for relative in source_attempts:
        source_path = source / "attempts" / relative
        shadow_path = shadow / "attempts" / relative
        if relative.name != "attempt.md":
            if source_path.read_bytes() != shadow_path.read_bytes():
                return False
            continue
        before_attempt = parse_attempt(source_path)
        after_attempt = parse_attempt(shadow_path)
        if (
            before_attempt.attempt,
            before_attempt.item,
            before_attempt.state,
            before_attempt.branch,
            before_attempt.base_revision,
            before_attempt.provenance,
        ) != (
            after_attempt.attempt,
            after_attempt.item,
            after_attempt.state,
            after_attempt.branch,
            after_attempt.base_revision,
            after_attempt.provenance,
        ):
            return False
    return True


def _copied_tree_equivalent(source: Path, shadow: Path, relative_root: Path) -> bool:
    before_files = sorted(
        path.relative_to(source / relative_root) for path in (source / relative_root).rglob("*") if path.is_file()
    )
    after_files = sorted(
        path.relative_to(shadow / relative_root) for path in (shadow / relative_root).rglob("*") if path.is_file()
    )
    if before_files != after_files:
        return False
    return all(
        (source / relative_root / path).read_bytes() == (shadow / relative_root / path).read_bytes()
        for path in before_files
    )


def _history_items_equivalent(source: Path, shadow: Path) -> bool:
    source_root = source / "history" / "items"
    shadow_root = shadow / "history" / "items"
    source_history = sorted(path.name for path in source_root.glob("*.md"))
    shadow_history = sorted(path.name for path in shadow_root.glob("*.md"))
    if source_history != shadow_history:
        return False
    for name in source_history:
        expected = replace_v2_header_fields(
            (source_root / name).read_text(encoding="utf-8"),
            {"schema": SCHEMA_V2},
        )
        if expected != (shadow_root / name).read_text(encoding="utf-8"):
            return False
    return True


def _equivalent(source: Path, shadow: Path) -> bool:
    if not _queue_and_items_equivalent(source, shadow):
        return False
    if not _current_equivalent(source, shadow) or not _attempts_equivalent(source, shadow):
        return False
    for relative_root in (Path("inbox"), Path("history/proposals")):
        if not _copied_tree_equivalent(source, shadow, relative_root):
            return False
    return _history_items_equivalent(source, shadow)


def _current_v2_result(authority: Authority) -> MigrationResult | None:
    match authority.version:
        case AuthorityVersion.V1:
            return None
        case AuthorityVersion.V2:
            return _inventory(authority.work_root, False)
        case _ as unreachable:
            assert_never(unreachable)


def _validated_shadow(
    source: Path,
    base: Path,
    project_root: Path,
    now: datetime,
    failpoint: MigrationFailpoint | None,
) -> Path:
    shadow = base / f".v2-migration-{uuid4().hex}"
    try:
        _build_shadow(source, shadow, now, failpoint)
        report = validate_v2_shadow(shadow, project_root)
        if not report.valid or not _equivalent(source, shadow):
            detail = report.render() if not report.valid else "Source and shadow semantic inventories differ."
            raise MigrationError("MIGRATION_INCOMPLETE", detail)
        return shadow
    except Exception:
        if shadow.exists():
            shutil.rmtree(shadow)
        raise


def _install_shadow(
    shadow: Path,
    destination: Path,
    failpoint: MigrationFailpoint | None,
) -> None:
    try:
        if destination.exists():
            shutil.rmtree(destination)
        shadow.replace(destination)
        if failpoint is not None:
            failpoint(MigrationBoundary(MigrationWriteKind.SHADOW_RENAME, "v2"))
    except Exception:
        if shadow.exists():
            shutil.rmtree(shadow)
        raise


def migrate_to_v2(
    base_work_root: Path,
    project_root: Path,
    *,
    now: datetime | None = None,
    failpoint: MigrationFailpoint | None = None,
) -> MigrationResult:
    base = base_work_root.resolve()
    current_time = now or datetime.now(UTC)
    with authority_transaction(base) as authority:
        current_result = _current_v2_result(authority)
        if current_result is not None:
            return current_result

        source_report = validate_work_state(base, project_root)
        if not source_report.valid:
            raise MigrationError("MIGRATION_SOURCE_INVALID", source_report.render())

        destination = base / "v2"
        if destination.is_symlink():
            raise MigrationError(
                "MIGRATION_INCOMPLETE", f"Migration destination '{destination}' must not be a symlink."
            )
        marker = destination / "migration-complete.md"
        if destination.exists() and not marker.is_file():
            raise MigrationError("MIGRATION_INCOMPLETE", f"Incomplete shadow exists at '{destination}'.")

        if not destination.exists():
            shadow = _validated_shadow(base, base, project_root, current_time, failpoint)
            _install_shadow(shadow, destination, failpoint)
        else:
            report = validate_v2_shadow(destination, project_root)
            if not report.valid or not _equivalent(base, destination):
                shadow = _validated_shadow(base, base, project_root, current_time, failpoint)
                _install_shadow(shadow, destination, failpoint)

        report = validate_v2_shadow(destination, project_root)
        if not report.valid or not _equivalent(base, destination):
            detail = report.render() if not report.valid else "Current v1 and v2 semantic inventories differ."
            raise MigrationError("MIGRATION_INCOMPLETE", detail)
        inventory = _inventory(destination, False)

        write_authority_selector(base, AuthorityVersion.V2, "v2")
        if failpoint is not None:
            failpoint(MigrationBoundary(MigrationWriteKind.SELECTOR_WRITE, "authority.json"))
        return MigrationResult(
            inventory.live_items,
            inventory.attempts,
            inventory.proposals,
            inventory.history_items,
            True,
        )
