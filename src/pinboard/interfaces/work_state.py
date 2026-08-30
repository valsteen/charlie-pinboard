from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import verify_reference
from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import ensure_directory_chain, resolve_durable_roots
from pinboard.adapters.files.root import ensure_default_git_exclude
from pinboard.adapters.files.views import expected_view_bytes, rebuild
from pinboard.adapters.sqlite.database import initialize_database, open_database, reconcile_database_publication
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import InitReceipt, OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces.work_state_models import Diagnostic, Severity, ValidationReport


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


def _error(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def validate_work_state(
    work_root: Path,
    attempt_briefs: Mapping[AttemptId, bytes] | None = None,
) -> ValidationReport:
    """Validate current SQLite authority and immutable artifacts without consulting generated views."""

    database = work_root / "state.sqlite3"
    try:
        state = SQLiteWorkStore(database).snapshot()
    except StorageError as error:
        return ValidationReport((_error(error.code.value, database, str(error)),))
    diagnostics: list[Diagnostic] = []
    for reference in state.artifact_references:
        try:
            verify_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error(error.code.value, work_root / reference.selector, str(error)))
    view_root = work_root / "views"
    for selector, expected in expected_view_bytes(state, attempt_briefs).items():
        path = view_root / selector
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if current != expected:
            diagnostics.append(
                Diagnostic(
                    "VIEW_REFRESH_REQUIRED",
                    Severity.WARNING,
                    path,
                    "Generated view is absent or stale; SQLite remains authoritative.",
                    "Run 'pinboard views rebuild'.",
                )
            )
    return ValidationReport(tuple(diagnostics))
