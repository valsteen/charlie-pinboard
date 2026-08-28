from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from charlie_pinboard.adapters.files.file_io import ensure_directory_chain, resolve_durable_roots
from charlie_pinboard.adapters.files.root import ensure_default_git_exclude
from charlie_pinboard.adapters.files.views import rebuild
from charlie_pinboard.adapters.sqlite.database import (
    initialize_database,
    open_database,
    reconcile_database_publication,
)
from charlie_pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore


def initialize_work_state(
    shared_repository_root: Path,
    work_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> InitReceipt:
    if work_root is None:
        ensure_default_git_exclude(shared_repository_root)
    roots = resolve_durable_roots(shared_repository_root, work_root)
    resumed = roots.database_path.exists()
    current = now or datetime.now(UTC)
    if resumed:
        connection = open_database(roots.database_path, OpenMode.READ_WRITE)
        connection.close()
        reconcile_database_publication(roots.database_path)
        ensure_directory_chain(roots)
    else:
        initialize_database(roots, current)
    store = SQLiteWorkStore(roots.database_path)
    result = rebuild(store, roots.work_root)
    if result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, result.warning.message)
    return InitReceipt(
        roots.work_root,
        roots.database_path,
        store.snapshot().lifecycle.project.revision,
        resumed,
    )
