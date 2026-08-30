from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol

import msgspec

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, NewArtifact

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

    def verify(self, reference: stored_state.ArtifactReference) -> None: ...

    def path(self, reference: stored_state.ArtifactReference) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...


class DispatchBriefPreparer(Protocol):
    def __call__(
        self,
        attempt_path: Path,
        attempt_id: str,
        attempt_branch: str,
        source_checkout_root: Path,
        checkpoint: str,
        environment: DispatchEnvironment,
        *,
        accepted_item_id: str | None = None,
        accepted_scope_revision: int | None = None,
        accepted_scope_digest: str | None = None,
        supplied_prompt: bytes | None = None,
        brief_review: bytes | None = None,
        review_id: str | None = None,
        review_publisher: BriefReviewPublisher | None = None,
    ) -> str: ...
