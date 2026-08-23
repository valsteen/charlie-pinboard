from contextlib import AbstractContextManager
from typing import Protocol, overload

from charlie_pinboard.application.mutations import (
    PlanningMutation,
    PlanningMutationReceipt,
    TransitionMutation,
    TransitionReceiptMutation,
)
from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.decisions import TransitionReceipt


class WorkTransaction(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    @overload
    def commit(self, mutation: TransitionMutation | TransitionReceiptMutation) -> TransitionReceipt: ...

    @overload
    def commit(self, mutation: PlanningMutation) -> PlanningMutationReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...
