from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

import msgspec

type BriefSourceManifestSchema = Literal["pinboard-brief-sources/v1"]
type BriefSourcePlanSchema = Literal["pinboard-brief-source-plan/v1"]
type BriefSourceIdentity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type BriefSourceSelector = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\n]+\z")]


class BriefSourceRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: BriefSourceIdentity
    selector: BriefSourceSelector
    families: tuple[BriefSourceIdentity, ...]

    def __post_init__(self) -> None:
        if not self.families or len(set(self.families)) != len(self.families):
            raise ValueError("families must contain one or more unique kebab-case values")


class BriefSourceManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: BriefSourceManifestSchema
    sources: tuple[BriefSourceRequest, ...]

    def __post_init__(self) -> None:
        authority_ids = tuple(source.authority_id for source in self.sources)
        if not authority_ids or len(set(authority_ids)) != len(authority_ids):
            raise ValueError("sources must contain one or more uniquely identified authorities")


@dataclass(frozen=True, slots=True)
class AuthoritySelector:
    relative_path: PurePosixPath
    heading: str | None


@dataclass(frozen=True, slots=True)
class BriefSourceLine:
    number: int
    content: bytes


@dataclass(frozen=True, slots=True)
class SelectedBriefSource:
    selector: AuthoritySelector
    content: bytes
    start_line: int
    end_line: int
    whole_file: bool
    lines: tuple[BriefSourceLine, ...]


@dataclass(frozen=True, slots=True)
class BriefSourceSegment:
    authority_id: str
    selector: str
    index: int
    start_line: int
    end_line: int
    content: bytes
    content_byte_count: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class PlannedBriefSource:
    authority_id: str
    selector: str
    families: tuple[str, ...]
    selected_sha256: str
    selected_byte_count: int
    start_line: int
    end_line: int
    whole_file: bool
    segments: tuple[BriefSourceSegment, ...]


@dataclass(frozen=True, slots=True)
class BriefSourceBatch:
    index: int
    content_byte_count: int
    estimated_rendered_byte_count: int
    segments: tuple[BriefSourceSegment, ...]


@dataclass(frozen=True, slots=True)
class BriefSourcePlan:
    schema: BriefSourcePlanSchema
    manifest_sha256: str
    max_batch_bytes: int
    sources: tuple[PlannedBriefSource, ...]
    batches: tuple[BriefSourceBatch, ...]


class BriefSourceSegmentView(msgspec.Struct, frozen=True):
    authority_id: str
    selector: str
    index: int
    start_line: int
    end_line: int
    content_byte_count: int
    content_sha256: str


class BriefSourceView(msgspec.Struct, frozen=True):
    authority_id: str
    selector: str
    families: tuple[str, ...]
    selected_sha256: str
    selected_byte_count: int
    start_line: int
    end_line: int
    whole_file: bool
    segments: tuple[BriefSourceSegmentView, ...]


class BriefSourceBatchView(msgspec.Struct, frozen=True):
    index: int
    content_byte_count: int
    estimated_rendered_byte_count: int
    segments: tuple[BriefSourceSegmentView, ...]


class BriefSourcePlanView(msgspec.Struct, frozen=True):
    schema: str
    manifest_sha256: str
    max_batch_bytes: int
    sources: tuple[BriefSourceView, ...]
    batches: tuple[BriefSourceBatchView, ...]
