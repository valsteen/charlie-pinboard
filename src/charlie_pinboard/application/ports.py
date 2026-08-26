from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from charlie_pinboard.application.artifacts import ArtifactRef
from charlie_pinboard.application.mutations import (
    TransitionMutation,
    TransitionReceiptMutation,
)
from charlie_pinboard.application.stored_state import ArtifactReference, StoredWorkState
from charlie_pinboard.domain.decisions import TransitionReceipt
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import ArtifactRole


class WorkTransaction(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def commit(self, mutation: TransitionMutation | TransitionReceiptMutation) -> TransitionReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
        *,
        item_id: ItemId | None = None,
        role: ArtifactRole | None = None,
    ) -> ArtifactReference: ...
