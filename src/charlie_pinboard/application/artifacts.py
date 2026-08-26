from dataclasses import dataclass

from charlie_pinboard.application.stored_state import ArtifactKind


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
