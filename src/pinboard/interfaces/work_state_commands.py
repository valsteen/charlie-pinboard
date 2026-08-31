"""Installed work-root resolution, initialization, validation, and repair commands."""

from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode, RootError, RootErrorCode
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_state import initialize_work_state, validate_work_state
from pinboard.interfaces.work_state_models import (
    Diagnostic,
    DiagnosticView,
    RootView,
    Severity,
    ValidationReport,
    ValidationView,
)


def resolve_roots(selection: cli_commands.RootSelection) -> cli_commands.ResolvedRoots:
    project_argument = selection.project_root
    selected_checkout = Path.cwd() if project_argument is None else project_argument
    try:
        source_checkout = resolve_source_checkout_root(selected_checkout)
        shared_repository = resolve_shared_repository_root(source_checkout)
    except RootError as error:
        if project_argument is None or error.code != RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE:
            raise
        source_checkout = project_argument.resolve()
        shared_repository = source_checkout
    work_argument = selection.work_root
    work = work_argument.resolve() if work_argument is not None else shared_repository / ".codex" / "pinboard"
    return cli_commands.ResolvedRoots(source_checkout, shared_repository, work, work_argument is not None)


def _diagnostic_view(report: ValidationReport) -> ValidationView:
    return ValidationView(
        valid=report.valid,
        diagnostics=tuple(
            DiagnosticView(
                code=value.code,
                severity=value.severity.value,
                path=str(value.path),
                message=value.message,
                hint=value.hint,
            )
            for value in report.diagnostics
        ),
    )


def root(roots: cli_commands.ResolvedRoots, _command: cli_commands.RootCommand) -> int:
    write_json(RootView(str(roots.source_checkout), str(roots.shared_repository), str(roots.work)))
    return 0


def validate(roots: cli_commands.ResolvedRoots, command: cli_commands.ValidateCommand) -> int:
    now = datetime.now(UTC)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    try:
        attempt_briefs = work_views.attempt_brief_views(roots, store)
    except WorkBriefError as error:
        report = validate_work_state(roots.work, now=now)
        report = ValidationReport(
            (*report.diagnostics, Diagnostic(error.code.value, Severity.ERROR, roots.work, error.message))
        )
    except ArtifactError, StorageError:
        report = validate_work_state(roots.work, now=now)
    else:
        report = validate_work_state(roots.work, attempt_briefs, now=now)
    if command.json:
        write_json(_diagnostic_view(report))
    else:
        print(report.render())
    return 0 if report.valid else 10


def initialize(roots: cli_commands.ResolvedRoots, _command: cli_commands.InitializeCommand) -> int:
    selected_work = roots.work if roots.explicit_work_root else None
    receipt = initialize_work_state(roots.shared_repository, selected_work, now=datetime.now(UTC))
    print(f"OK WORK_STATE_INITIALIZED {receipt.work_root}")
    return 0


def rebuild_views(roots: cli_commands.ResolvedRoots, _command: cli_commands.RebuildViewsCommand) -> int:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    result = work_views.rebuild(roots, store, datetime.now(UTC))
    if result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, result.warning.message)
    print(f"OK VIEWS_REBUILT revision={result.database_revision}")
    return 0
