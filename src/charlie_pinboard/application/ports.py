from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from charlie_pinboard.application.artifacts import ArtifactRef
from charlie_pinboard.application.mutation_models import StoredStateMutation
from charlie_pinboard.application.stored_state import ArtifactReference, StoredWorkState
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.identifiers import ItemId


class WorkTransaction(Protocol):
    def snapshot(self) -> StoredWorkState: ...

    def commit(self, mutation: StoredStateMutation) -> decision_models.TransitionReceipt: ...


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
        role: work_models.ArtifactRole | None = None,
    ) -> ArtifactReference: ...
