from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import FileIOError, ensure_directory_chain, resolve_durable_roots
from charlie_pinboard.adapters.files.views import rebuild
from charlie_pinboard.adapters.sqlite.database import OpenMode, StorageError, initialize_database, open_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore


class InitializationError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class InitReceipt:
    work_root: Path
    database_path: Path
    project_revision: int
    resumed: bool


def initialize_work_state(
    project_root: Path,
    work_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> InitReceipt:
    try:
        roots = resolve_durable_roots(project_root, work_root)
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
