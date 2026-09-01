from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import StoredStateMutation
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionResult


class WorkTransaction(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[decision_models.TransitionReceipt]: ...


class WorkStore(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[stored_state.ArtifactReference]: ...
