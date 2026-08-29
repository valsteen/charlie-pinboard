from dataclasses import dataclass

from pinboard.application.stored_state import ArtifactKind


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


@dataclass(frozen=True, slots=True)
class WorkBriefIdentity:
    attempt_id: str
    item_id: str
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
