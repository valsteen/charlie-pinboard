from contextlib import AbstractContextManager
from typing import Protocol

from repo_work.decisions import Decision, TransitionReceipt
from repo_work.model import LedgerSnapshot


class WorkTransaction(Protocol):
    def snapshot(self) -> LedgerSnapshot: ...

    def commit(self, decision: Decision) -> TransitionReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> LedgerSnapshot: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...
