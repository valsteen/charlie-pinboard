from contextlib import AbstractContextManager
from typing import Protocol

from charlie_pinboard.domain.decisions import Decision, TransitionReceipt
from charlie_pinboard.domain.model import LedgerSnapshot


class WorkTransaction(Protocol):
    def snapshot(self) -> LedgerSnapshot: ...

    def commit(self, decision: Decision) -> TransitionReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> LedgerSnapshot: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...
