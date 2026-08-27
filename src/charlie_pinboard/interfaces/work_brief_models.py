from typing import Annotated, Literal

import msgspec

type NonEmptyText = Annotated[str, msgspec.Meta(min_length=1)]
type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^\S(?:[^\n]*\S)?$")]
type KebabId = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type NonEmptyTexts = Annotated[tuple[NonEmptyText, ...], msgspec.Meta(min_length=1)]


class AcceptedScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: PositiveInt
    digest: Sha256


class NoArchitectureImpact(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="none",
    tag_field="kind",
):
    reason: NonEmptyText


class ReadOnlyArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="read-only",
    tag_field="kind",
):
    selector: NonEmptyLine
    reason: NonEmptyText


class UpdateRequiredArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="update-required",
    tag_field="kind",
):
    selector: NonEmptyLine
    reason: NonEmptyText


type ArchitectureImpact = NoArchitectureImpact | ReadOnlyArchitecture | UpdateRequiredArchitecture


class AcceptedScopeAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="accepted-scope",
    tag_field="kind",
):
    item_id: KebabId
    scope_revision: PositiveInt


class AuthorityAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="authority",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


class RepositoryPolicyAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="repository-policy",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


class ExistingConsumerAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="existing-consumer",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


type AuthorizationBasis = (
    AcceptedScopeAuthorization | AuthorityAuthorization | RepositoryPolicyAuthorization | ExistingConsumerAuthorization
)


class ContractRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    invariant: NonEmptyText
    authority: NonEmptyText
    consumer: NonEmptyText
    failure: NonEmptyText
    verification: NonEmptyText
    revalidation: NonEmptyText
    authorization_basis: AuthorizationBasis


class VerificationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authorization_basis: AuthorizationBasis
    obligation: NonEmptyText


class AcceptanceCriterion(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    number: PositiveInt
    requirement: NonEmptyText


class ReviewedAuthority(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    selector: NonEmptyLine
    reviewed_sha256: Sha256
    families: Annotated[tuple[KebabId, ...], msgspec.Meta(min_length=1)]


class ContractCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="contract",
    tag_field="disposition",
):
    contract_invariant: NonEmptyText


class AcceptanceCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="acceptance",
    tag_field="disposition",
):
    criterion: PositiveInt


class DeferredCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="deferred",
    tag_field="disposition",
):
    deferral_id: KebabId


class NotApplicableCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="disposition",
):
    reason: NonEmptyText


type CoverageOwner = (
    ContractCoverageOwner | AcceptanceCoverageOwner | DeferredCoverageOwner | NotApplicableCoverageOwner
)


class CoverageRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    family: KebabId
    distinction: NonEmptyText
    consumer: NonEmptyText
    owner: CoverageOwner
    counterexample: NonEmptyText


class LifecycleRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: KebabId
    source_state: NonEmptyText
    authority: NonEmptyText
    evidence: NonEmptyText
    effects: NonEmptyText
    illegal_sibling: NonEmptyText


class NoLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="kind",
):
    reason: NonEmptyText


class RequiredLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="required",
    tag_field="kind",
):
    operations: Annotated[tuple[LifecycleRecord, ...], msgspec.Meta(min_length=1)]


type LifecyclePartition = NoLifecyclePartition | RequiredLifecyclePartition


class Deferral(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    deferral_id: KebabId
    reason: NonEmptyText
    reopen_when: NonEmptyText


class LocalCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="local",
    tag_field="boundary",
):
    checkpoint_id: KebabId
    title: NonEmptyLine
    architecture_impact: ArchitectureImpact
    outcome_description: NonEmptyText
    acceptance_criteria: Annotated[tuple[AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    verification: Annotated[tuple[VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[Deferral, ...]


class CrossBoundaryCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="cross-boundary",
    tag_field="boundary",
):
    checkpoint_id: KebabId
    title: NonEmptyLine
    architecture_impact: ArchitectureImpact
    outcome: Literal["independently-buildable"]
    outcome_description: NonEmptyText
    contracts: Annotated[tuple[ContractRecord, ...], msgspec.Meta(min_length=1)]
    acceptance_criteria: Annotated[tuple[AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    reviewed_authorities: Annotated[tuple[ReviewedAuthority, ...], msgspec.Meta(min_length=1)]
    coverage: Annotated[tuple[CoverageRecord, ...], msgspec.Meta(min_length=1)]
    lifecycle_partition: LifecyclePartition
    verification: Annotated[tuple[VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[Deferral, ...]


type WorkBriefCheckpoint = LocalCheckpoint | CrossBoundaryCheckpoint


class WorkBrief(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief/v2"]
    artifact_revision: PositiveInt
    attempt_id: KebabId
    item_id: KebabId
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner_task_id: NonEmptyLine
    accepted_scope: AcceptedScope
    title: NonEmptyLine
    outcome: NonEmptyText
    supported_production_roots: NonEmptyTexts
    product_decision_and_provenance: NonEmptyText
    testing_strategy: NonEmptyText
    scope: NonEmptyTexts
    bootstrap: tuple[NonEmptyText, ...]
    compatibility: tuple[NonEmptyText, ...]
    non_goals: tuple[NonEmptyText, ...]
    checkpoint: WorkBriefCheckpoint
    remaining_work: NonEmptyText


class ReviewCoverageResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    family: KebabId
    owner: CoverageOwner
    verdict: Literal["covered"]
    counterexample_result: NonEmptyText


class WorkBriefReview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-review/v2"]
    attempt_id: KebabId
    checkpoint_id: KebabId
    checkpoint_sha256: Sha256
    reviewed_authority_set_sha256: Sha256
    reviewer_task_id: NonEmptyLine
    status: Literal["complete"]
    verdict: Literal["ready"]
    coverage: Annotated[tuple[ReviewCoverageResult, ...], msgspec.Meta(min_length=1)]
