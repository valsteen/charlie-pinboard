from enum import Enum
from pathlib import Path

from repo_work.records import Record


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


class Diagnostic(Record):
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
