from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SCHEMA_V1 = "repo-work/v1"


class WorkState(StrEnum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


TERMINAL_STATES = frozenset({"done", "superseded", "dropped"})


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
    header: dict[str, object]
    items: tuple[QueueItem, ...]
    revision: str

    def by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    path: Path
    item: str
    user_label: str


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
    state: str
    branch: str
    base_revision: str
    owner: str
