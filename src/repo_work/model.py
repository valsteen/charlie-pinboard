from enum import Enum
from pathlib import Path
from typing import Final

from repo_work.records import Record

SCHEMA_V1: Final = "repo-work/v1"


class WorkState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


TERMINAL_STATES: Final = frozenset({"done", "superseded", "dropped"})


type HeaderValue = str | bool | None
type Header = dict[str, HeaderValue]


class QueueItem(Record):
    item: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt: str | None
    source: str
    next_action: str | None
    notes: str


class Queue(Record):
    path: Path
    header: Header
    items: tuple[QueueItem, ...]
    revision: str

    def by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}


class WorkItemRecord(Record):
    path: Path
    item: str
    user_label: str


class CurrentPointer(Record):
    path: Path
    focus_item: str | None
    focus_attempt: str | None
    next_action: str


class Attempt(Record):
    path: Path
    attempt: str
    item: str
    state: str
    branch: str
    base_revision: str
    owner: str
