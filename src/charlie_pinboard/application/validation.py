from dataclasses import dataclass
from pathlib import Path

from charlie_pinboard.adapters.files.artifacts import ArtifactError, verify_reference
from charlie_pinboard.adapters.files.views import expected_view_bytes
from charlie_pinboard.adapters.sqlite.database import StorageError
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.diagnostics import Diagnostic, Severity


@dataclass(frozen=True, slots=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def valid(self) -> bool:
        return not any(diagnostic.severity == Severity.ERROR for diagnostic in self.diagnostics)

    def render(self) -> str:
        if not self.diagnostics:
            return "OK WORK_STATE_VALID"
        return "\n".join(diagnostic.render() for diagnostic in self.diagnostics)


def _error(code: str, path: Path, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, path=path, message=message, hint=hint)


def validate_sqlite_work_state(work_root: Path) -> ValidationReport:
    """Validate current SQLite authority and immutable artifacts without consulting generated views."""

    database = work_root / "state.sqlite3"
    try:
        state = SQLiteWorkStore(database).snapshot()
    except StorageError as error:
        return ValidationReport((_error(error.code.value, database, str(error)),))
    diagnostics: list[Diagnostic] = []
    for reference in state.artifacts.references:
        try:
            verify_reference(work_root, reference)
        except ArtifactError as error:
            diagnostics.append(_error(error.code, work_root / reference.selector, str(error)))
    view_root = work_root / "views"
    for selector, expected in expected_view_bytes(state).items():
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
