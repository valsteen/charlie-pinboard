from dataclasses import dataclass
from pathlib import Path


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
