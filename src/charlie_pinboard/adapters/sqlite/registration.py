from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import FileIOError, ensure_directory_chain, resolve_durable_roots
from charlie_pinboard.adapters.files.views import rebuild
from charlie_pinboard.adapters.sqlite.database import OpenMode, StorageError, initialize_database, open_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.registration import InitializationError, InitReceipt

_RETIRED_STATE_SELECTORS = (
    "authority.json",
    "queue.md",
    "current.md",
    "coordinator.json",
    "v2",
    "legacy-v1",
    "legacy-v2",
)


def _reject_retired_state(work_root: Path) -> None:
    existing = tuple(selector for selector in _RETIRED_STATE_SELECTORS if (work_root / selector).exists())
    if existing:
        raise InitializationError(
            "WORK_STATE_CONFLICT",
            f"Fresh SQLite initialization refuses existing predecessor state: {', '.join(existing)}.",
        )


def initialize_work_state(
    project_root: Path,
    work_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> InitReceipt:
    try:
        roots = resolve_durable_roots(project_root, work_root)
        _reject_retired_state(roots.work_root)
        resumed = roots.database_path.exists()
        current = now or datetime.now(UTC)
        if resumed:
            connection = open_database(roots.database_path, OpenMode.READ_WRITE)
            connection.close()
            ensure_directory_chain(roots)
        else:
            initialize_database(roots, current)
        store = SQLiteWorkStore(roots.database_path)
        result = rebuild(store, roots.work_root)
    except InitializationError:
        raise
    except (FileIOError, StorageError) as error:
        code = error.code.value if isinstance(error, StorageError) else "STORAGE_IO_ERROR"
        raise InitializationError(code, str(error)) from error
    if result.warning is not None:
        raise InitializationError(result.warning.code, result.warning.message)
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        store.snapshot().lifecycle.project.revision,
        resumed,
    )
