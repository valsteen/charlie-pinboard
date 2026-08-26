from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NewType

AuthorityId = NewType("AuthorityId", str)
AuthorityFamily = NewType("AuthorityFamily", str)


class CoverageDisposition(Enum):
    CONTRACT = "contract"
    ACCEPTANCE = "acceptance"
    DEFERRED = "deferred"
    NOT_APPLICABLE = "not-applicable"


class BriefOwnerKind(Enum):
    CONTRACT = "contract"
    CRITERION = "criterion"
    DEFERRAL = "deferral"
    REASON = "reason"


@dataclass(frozen=True, slots=True)
class MarkdownTable:
    rows: tuple[tuple[str, ...], ...]
    serialized: bytes


@dataclass(frozen=True, slots=True)
class AuthoritySelector:
    relative_path: Path
    heading: str | None


class ArchitectureImpactKind(Enum):
    NONE = "none"
    READ_ONLY = "read-only"
    UPDATE_REQUIRED = "update-required"


@dataclass(frozen=True, slots=True)
class ArchitectureImpact:
    kind: ArchitectureImpactKind
    selector: AuthoritySelector | None
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewedAuthority:
    authority_id: AuthorityId
    selector: AuthoritySelector
    reviewed_sha256: str
    families: tuple[AuthorityFamily, ...]


@dataclass(frozen=True, slots=True)
class ContractRecord:
    invariant: str
    authority: str
    consumer: str
    failure: str
    verification: str
    revalidation: str


@dataclass(frozen=True, slots=True)
class BriefOwner:
    kind: BriefOwnerKind
    value: str


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    authority_id: AuthorityId
    family: AuthorityFamily
    distinction: str
    consumer: str
    disposition: CoverageDisposition
    owner: BriefOwner
    counterexample: str


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    operation: str
    source_state: str
    authority: str
    evidence: str
    effects: str
    illegal_sibling: str


@dataclass(frozen=True, slots=True)
class BriefReviewMetadata:
    attempt: str
    checkpoint: str
    checkpoint_sha256: str
    reviewed_authority_set_sha256: str
    reviewer_task_id: str
    status: str
    verdict: str


class CheckpointBoundary(Enum):
    LOCAL = "local"
    CROSS_BOUNDARY = "cross-boundary"
