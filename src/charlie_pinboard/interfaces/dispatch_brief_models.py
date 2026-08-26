from dataclasses import dataclass
from enum import Enum
from typing import NewType

from charlie_pinboard.interfaces.brief_source_models import AuthoritySelector

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
class AcceptedScopeAuthorizationBasis:
    item_id: str
    scope_revision: int


@dataclass(frozen=True, slots=True)
class AuthorityAuthorizationBasis:
    authority_id: AuthorityId
    family: AuthorityFamily


@dataclass(frozen=True, slots=True)
class RepositoryPolicyAuthorizationBasis:
    authority_id: AuthorityId
    family: AuthorityFamily


@dataclass(frozen=True, slots=True)
class ExistingConsumerAuthorizationBasis:
    authority_id: AuthorityId
    family: AuthorityFamily


type AuthorizationBasis = (
    AcceptedScopeAuthorizationBasis
    | AuthorityAuthorizationBasis
    | RepositoryPolicyAuthorizationBasis
    | ExistingConsumerAuthorizationBasis
)


@dataclass(frozen=True, slots=True)
class ContractRecord:
    invariant: str
    authority: str
    consumer: str
    failure: str
    verification: str
    revalidation: str
    authorization_basis: AuthorizationBasis


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    authorization_basis: AuthorizationBasis
    obligation: str


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
