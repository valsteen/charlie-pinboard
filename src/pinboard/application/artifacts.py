from dataclasses import dataclass, field
from typing import Literal

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
class ResultArtifactRef:
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    kind: Literal[stored_state.ArtifactKind.RESULT] = field(init=False, default=stored_state.ArtifactKind.RESULT)


@dataclass(frozen=True, slots=True)
class EvidenceArtifactRef:
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    kind: Literal[stored_state.ArtifactKind.EVIDENCE] = field(init=False, default=stored_state.ArtifactKind.EVIDENCE)


@dataclass(frozen=True, slots=True)
class CheckpointArtifacts:
    result: ResultArtifactRef
    review: EvidenceArtifactRef


@dataclass(frozen=True, slots=True)
class WorkBriefIdentity:
    attempt_id: str
    item_id: str
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
