from dataclasses import dataclass

from pinboard.application import stored_state


@dataclass(frozen=True, slots=True)
class NewArtifact:
    kind: stored_state.ArtifactKind
    key: str
    revision: int
    suffix: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    kind: stored_state.ArtifactKind
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CheckpointArtifacts:
    result: ArtifactRef
    review: ArtifactRef


@dataclass(frozen=True, slots=True)
class WorkBriefIdentity:
    attempt_id: str
    item_id: str
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
