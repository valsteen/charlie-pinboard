from contextlib import AbstractContextManager
from typing import Protocol

from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.decisions import Decision, TransitionReceipt


class WorkTransaction(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def commit(self, decision: Decision) -> TransitionReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...
