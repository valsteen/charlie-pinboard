from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.domain.model import SCHEMA_V1, SCHEMA_V2
from charlie_pinboard.legacy.atomic import atomic_write, atomic_write_text, transition_lock
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority, write_authority_selector
from charlie_pinboard.legacy.coordinator import CoordinatorRegistration, read_coordinator
from charlie_pinboard.legacy.markdown import Queue, render_current, render_queue, render_v2_header
from charlie_pinboard.legacy.transaction_store import (
    ChangeSet,
    commit_change_set,
    recover_pending_commit,
    validate_change_set,
    write_bytes_change,
)
from charlie_pinboard.legacy.validate import validate_work_state


class RegistrationError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _registration(project_root: Path, task_id: str, host_id: str, generation: int) -> CoordinatorRegistration:
    if not task_id or not host_id:
        raise RegistrationError("COORDINATOR_IDENTITY_INVALID", "task_id and host_id must be non-empty.")
    return CoordinatorRegistration(
        schema=SCHEMA_V1,
        project_root=str(project_root.resolve()),
        task_id=task_id,
        host_id=host_id,
        generation=generation,
        registered_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


def _initialize_work_state(
    project_root: Path,
    task_id: str,
    host_id: str,
    work_root: Path | None = None,
) -> Path:
    project_root = project_root.resolve()
    work_root = work_root.resolve() if work_root is not None else project_root / ".codex" / "work"
    if work_root.exists():
        raise RegistrationError("WORK_STATE_ALREADY_EXISTS", f"'{work_root}' already exists.")
    for directory in (
        work_root / "items",
        work_root / "attempts",
        work_root / "inbox",
        work_root / "history" / "items",
        work_root / "history" / "proposals",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    queue = Queue(path=work_root / "queue.md", header={}, items=(), revision="")
    atomic_write_text(work_root / "queue.md", render_queue(queue, ()))
    atomic_write_text(work_root / "current.md", render_current(None, None, "select"))
    atomic_write(work_root / "coordinator.json", _registration(project_root, task_id, host_id, 1).render())
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise RegistrationError("INITIALIZATION_POSTCONDITION_FAILED", report.render())
    return work_root


def initialize_work_state(
    project_root: Path,
    task_id: str,
    host_id: str,
    work_root: Path | None = None,
) -> Path:
    selected = work_root.resolve() if work_root is not None else project_root.resolve() / ".codex" / "work"
    with transition_lock(selected):
        return _initialize_work_state(project_root, task_id, host_id, selected)


def _initialize_work_state_v2(project_root: Path, work_root: Path | None = None) -> Path:
    project_root = project_root.resolve()
    base = work_root.resolve() if work_root is not None else project_root / ".codex" / "work"
    if base.exists():
        raise RegistrationError("WORK_STATE_ALREADY_EXISTS", f"'{base}' already exists.")
    current = base / "v2"
    for directory in (
        current / "items",
        current / "attempts",
        current / "resources",
        current / "leases" / "resources",
        current / "inbox",
        current / "history" / "items",
        current / "history" / "proposals",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    queue = Queue(path=current / "queue.md", header={"schema": SCHEMA_V2}, items=(), revision="")
    atomic_write_text(current / "queue.md", render_queue(queue, (), SCHEMA_V2))
    atomic_write_text(current / "current.md", render_current(None, None, "select", SCHEMA_V2))
    atomic_write_text(
        current / "migration-complete.md",
        render_v2_header({"kind": "migration-complete", "schema": SCHEMA_V2}) + "\n# Native v2 Ledger\n",
    )
    write_authority_selector(base, AuthorityVersion.V2, "v2")
    report = validate_work_state(base, project_root)
    if not report.valid:
        raise RegistrationError("INITIALIZATION_POSTCONDITION_FAILED", report.render())
    return base


def initialize_work_state_v2(project_root: Path, work_root: Path | None = None) -> Path:
    selected = work_root.resolve() if work_root is not None else project_root.resolve() / ".codex" / "work"
    with transition_lock(selected):
        return _initialize_work_state_v2(project_root, selected)


def transfer_coordinator(
    work_root: Path,
    project_root: Path,
    expected_generation: int,
    task_id: str,
    host_id: str,
) -> None:
    with transition_lock(work_root):
        authority = resolve_authority(work_root)
        if authority.version != AuthorityVersion.V1:
            raise RegistrationError(
                "MIGRATION_REQUIRED", "Legacy coordinator transfer is unavailable after v2 cutover."
            )
        root = authority.work_root
        recover_pending_commit(root)
        report = validate_work_state(root, project_root)
        if not report.valid:
            raise RegistrationError("WORK_STATE_INVALID", report.render())
        current = read_coordinator(root / "coordinator.json")
        if current.generation != expected_generation:
            raise RegistrationError(
                "COORDINATOR_OWNERSHIP_CONFLICT",
                f"Expected generation {expected_generation}, found {current.generation}.",
            )
        changes = ChangeSet.of(
            write_bytes_change(
                "coordinator.json",
                _registration(project_root, task_id, host_id, expected_generation + 1).render(),
            )
        )
        validate_change_set(root, project_root, changes, AuthorityVersion.V1)
        commit_change_set(root, project_root, changes, AuthorityVersion.V1)
