from datetime import UTC, datetime
from pathlib import Path

from repo_work.atomic import atomic_write, atomic_write_text, transition_lock
from repo_work.coordinator import CoordinatorRegistration, read_coordinator
from repo_work.markdown import render_current, render_queue
from repo_work.model import SCHEMA_V1, Queue
from repo_work.transaction_store import (
    ChangeSet,
    commit_change_set,
    recover_pending_commit,
    validate_change_set,
    write_bytes_change,
)
from repo_work.validate import validate_work_state


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


def initialize_work_state(
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


def transfer_coordinator(
    work_root: Path,
    project_root: Path,
    expected_generation: int,
    task_id: str,
    host_id: str,
) -> None:
    with transition_lock(work_root):
        recover_pending_commit(work_root)
        report = validate_work_state(work_root, project_root)
        if not report.valid:
            raise RegistrationError("WORK_STATE_INVALID", report.render())
        current = read_coordinator(work_root / "coordinator.json")
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
        validate_change_set(work_root, project_root, changes)
        commit_change_set(work_root, project_root, changes)
