from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class OpenMode(Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


@dataclass(frozen=True, slots=True)
class InitReceipt:
    work_root: Path
    database_path: Path
    project_revision: int
    resumed: bool
