from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pinboard.adapters.files.artifacts import verify_reference
from pinboard.adapters.files.errors import ArtifactError
from pinboard.adapters.files.views import expected_view_bytes
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    path: Path
    message: str
    hint: str | None = None

    def render(self) -> str:
        result = f"{self.severity.value.upper()} {self.code} {self.path}: {self.message}"
        if self.hint:
            result += f" Hint: {self.hint}"
        return result


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
