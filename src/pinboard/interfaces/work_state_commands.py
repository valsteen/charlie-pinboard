"""Installed work-root resolution, initialization, validation, and repair commands."""

from datetime import UTC, datetime
from pathlib import Path

from pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode, RootError, RootErrorCode
from pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.interfaces import cli_commands, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import WorkBriefError
from pinboard.interfaces.work_state import (
    initialize_work_state,
    read_state_for_validation,
    validate_loaded_work_state,
)
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


def _project_validation(report: ValidationReport) -> ValidationView:
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


def show_roots(roots: cli_commands.ResolvedRoots, _command: cli_commands.RootCommand) -> int:
    write_json(RootView(str(roots.source_checkout), str(roots.shared_repository), str(roots.work)))
    return 0


def validate_state(roots: cli_commands.ResolvedRoots, command: cli_commands.ValidateCommand) -> int:
    operation_time = datetime.now(UTC)
    loaded_state = read_state_for_validation(roots.work)
    if isinstance(loaded_state, ValidationReport):
        validation_report = loaded_state
    else:
        current_state = loaded_state
        brief_error: WorkBriefError | None = None
        try:
            attempt_briefs = work_views.read_attempt_brief_views(roots, current_state)
        except WorkBriefError as error:
            brief_error = error
            attempt_briefs = None
        except ArtifactError:
            attempt_briefs = None
        validation_report = validate_loaded_work_state(roots.work, current_state, attempt_briefs, now=operation_time)
        if brief_error is not None:
            validation_report = ValidationReport(
                (
                    *validation_report.diagnostics,
                    Diagnostic(brief_error.code.value, Severity.ERROR, roots.work, brief_error.message),
                )
            )
    if command.json:
        write_json(_project_validation(validation_report))
    else:
        print(validation_report.render())
    return 0 if validation_report.valid else 10


def initialize_state(roots: cli_commands.ResolvedRoots, _command: cli_commands.InitializeCommand) -> int:
    selected_work = roots.work if roots.explicit_work_root else None
    operation_time = datetime.now(UTC)
    receipt = initialize_work_state(roots.shared_repository, selected_work, now=operation_time)
    print(f"OK WORK_STATE_INITIALIZED {receipt.work_root}")
    return 0


def rebuild_views(roots: cli_commands.ResolvedRoots, _command: cli_commands.RebuildViewsCommand) -> int:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    operation_time = datetime.now(UTC)
    rebuild_result = work_views.rebuild(roots, store, operation_time)
    if rebuild_result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuild_result.warning.message)
    print(f"OK VIEWS_REBUILT revision={rebuild_result.database_revision}")
    return 0
