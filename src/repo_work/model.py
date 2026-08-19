from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal

SCHEMA_V1: Final = "repo-work/v1"
SCHEMA_V2: Final = "repo-work/v2"
type SchemaV1 = Literal["repo-work/v1"]


class WorkState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"


class AttemptState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


TERMINAL_STATES: Final = frozenset({"done", "superseded", "dropped"})


type HeaderValue = str | bool | None
type Header = dict[str, HeaderValue]


@dataclass(frozen=True, slots=True)
class QueueItem:
    item: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt: str | None
    source: str
    next_action: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class Queue:
    path: Path
    header: Header
    items: tuple[QueueItem, ...]
    revision: str

    def by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    path: Path
    item: str
    user_label: str
    queue_item: QueueItem | None = None
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    path: Path
    focus_item: str | None
    focus_attempt: str | None
    next_action: str


@dataclass(frozen=True, slots=True)
class Attempt:
    path: Path
    attempt: str
    item: str
    state: AttemptState
    branch: str
    base_revision: str
    provenance: str
