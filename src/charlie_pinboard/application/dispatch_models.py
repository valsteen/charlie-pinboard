from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol

import msgspec

from charlie_pinboard.application.artifacts import ArtifactRef, NewArtifact
from charlie_pinboard.application.stored_state import ArtifactReference

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]

type DispatchSchema = Literal["pinboard-dispatch/v1"]

type BriefReviewPublisher = Callable[[str, bytes | None, str | None], tuple[bytes, str]]


class DispatchPermission(Enum):
    REPOSITORY_READ = "repository-read"
    REPOSITORY_WRITE = "repository-write"
    NETWORK = "network"
    EXTERNAL_WRITE = "external-write"
    LIVE_APPLICATION = "live-application"


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
    checkout: NonEmptyLine
    branch: NonEmptyLine
    starting_revision: NonEmptyLine
    permissions: tuple[DispatchPermission, ...]


class DispatchArtifactPort(Protocol):
    @property
    def work_root(self) -> Path: ...

    def verify(self, reference: ArtifactReference) -> None: ...

    def path(self, reference: ArtifactReference) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...


class DispatchBriefPreparer(Protocol):
    def __call__(
        self,
        attempt_path: Path,
        attempt_id: str,
        attempt_branch: str,
        project_root: Path,
        checkpoint: str,
        environment: DispatchEnvironment,
        *,
        accepted_item_id: str | None = None,
        accepted_scope_revision: int | None = None,
        supplied_prompt: bytes | None = None,
        brief_review: bytes | None = None,
        review_id: str | None = None,
        review_publisher: BriefReviewPublisher | None = None,
    ) -> str: ...
