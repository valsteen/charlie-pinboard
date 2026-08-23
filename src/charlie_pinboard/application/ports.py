from contextlib import AbstractContextManager
from typing import Protocol, overload

from charlie_pinboard.application.mutations import (
    PlanningMutation,
    PlanningMutationReceipt,
    TransitionReceiptMutation,
)
from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.decisions import Decision, TransitionReceipt


class WorkTransaction(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    @overload
    def commit(self, mutation: Decision | TransitionReceiptMutation) -> TransitionReceipt: ...

    @overload
    def commit(self, mutation: PlanningMutation) -> PlanningMutationReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...
