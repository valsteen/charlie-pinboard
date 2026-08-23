from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from charlie_pinboard.application.stored_state import ArtifactKind, ArtifactReference
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.model import ArtifactRole


@dataclass(frozen=True, slots=True)
class NewArtifact:
    kind: ArtifactKind
    key: str
    revision: int
    suffix: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    kind: ArtifactKind
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int

    def with_selector(self, selector: str) -> ArtifactRef:
        return replace(self, selector=selector)

    def with_digest(self, digest: str) -> ArtifactRef:
        return replace(self, content_sha256=digest)

    def with_size(self, size: int) -> ArtifactRef:
        return replace(self, size_bytes=size)


class ArtifactReferenceStore(Protocol):
    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
        *,
        item_id: ItemId | None = None,
        role: ArtifactRole | None = None,
    ) -> ArtifactReference: ...


def accept_reference(
    store: ArtifactReferenceStore,
    work_root: Path,
    published: ArtifactRef,
    accepted_at: datetime,
    *,
    item_id: ItemId | None = None,
    role: ArtifactRole | None = None,
) -> ArtifactReference:
    """Ask storage to verify and accept exact durable bytes in one transaction."""

    return store.accept_artifact_reference(
        work_root,
        published,
        accepted_at,
        item_id=item_id,
        role=role,
    )
