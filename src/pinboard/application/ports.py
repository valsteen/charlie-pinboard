from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import StoredStateMutation
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ItemId


class WorkTransaction(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def commit(self, mutation: StoredStateMutation) -> decision_models.TransitionReceipt: ...


class WorkStore(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
        *,
        item_id: ItemId | None = None,
        role: work_models.ArtifactRole | None = None,
    ) -> stored_state.ArtifactReference: ...
