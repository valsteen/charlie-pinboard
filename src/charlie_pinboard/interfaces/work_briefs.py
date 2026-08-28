import hashlib
import re
from pathlib import Path
from typing import assert_never

import msgspec

from charlie_pinboard.application.artifact_publication import ArtifactReader
from charlie_pinboard.application.artifacts import WorkBriefIdentity
from charlie_pinboard.application.stored_state import ArtifactKind, StoredWorkState
from charlie_pinboard.domain import work_models
from charlie_pinboard.domain.identifiers import AttemptId
from charlie_pinboard.interfaces.brief_sources import parse_authority_selector, select_brief_source
from charlie_pinboard.interfaces.errors import BriefSourceError, WorkBriefError, WorkBriefErrorCode
from charlie_pinboard.interfaces.work_brief_models import (
    AcceptanceCoverageOwner,
    AcceptedScopeAuthorization,
    ArchitectureImpact,
    AuthorityAuthorization,
    ContractCoverageOwner,
    CoverageOwner,
    CrossBoundaryCheckpoint,
    DeferredCoverageOwner,
    ExistingConsumerAuthorization,
    LocalCheckpoint,
    NoArchitectureImpact,
    NoLifecyclePartition,
    NotApplicableCoverageOwner,
    ReadOnlyArchitecture,
    RepositoryPolicyAuthorization,
    RequiredLifecyclePartition,
    ReviewedAuthority,
    UpdateRequiredArchitecture,
    WorkBrief,
    WorkBriefCheckpoint,
    WorkBriefReview,
)

PROHIBITION: re.Pattern[str] = re.compile(
    r"\b(?:must not|do not|cannot|never|prohibition|prohibited)\b",
    re.IGNORECASE,
)


def _invalid(message: str) -> WorkBriefError:
    return WorkBriefError(WorkBriefErrorCode.BRIEF_INVALID, message)


def _canonical_bytes[T](value: T) -> bytes:
    return msgspec.json.encode(value, order="sorted")


def _authorization_key(
    basis: AcceptedScopeAuthorization
    | AuthorityAuthorization
    | RepositoryPolicyAuthorization
    | ExistingConsumerAuthorization,
    brief: WorkBrief,
    authority_keys: frozenset[tuple[str, str]],
) -> None:
    match basis:
        case AcceptedScopeAuthorization(item_id=item_id, scope_revision=scope_revision):
            if item_id != brief.item_id or scope_revision != brief.accepted_scope.revision:
                raise _invalid("Accepted-scope authorization does not match the work brief identity.")
        case (
            AuthorityAuthorization(authority_id=authority_id, family=family)
            | RepositoryPolicyAuthorization(authority_id=authority_id, family=family)
            | ExistingConsumerAuthorization(authority_id=authority_id, family=family)
        ):
            if (authority_id, family) not in authority_keys:
                raise _invalid(f"Authorization references unknown authority family '{authority_id}#{family}'.")
        case _ as unreachable:
            assert_never(unreachable)


def _owner_key(owner: CoverageOwner) -> tuple[str, str | int]:
    match owner:
        case ContractCoverageOwner(contract_invariant=invariant):
            return "contract", invariant
        case AcceptanceCoverageOwner(criterion=criterion):
            return "acceptance", criterion
        case DeferredCoverageOwner(deferral_id=deferral_id):
            return "deferred", deferral_id
        case NotApplicableCoverageOwner(reason=reason):
            return "not-applicable", reason
        case _ as unreachable:
            assert_never(unreachable)


def _validate_architecture_impact(impact: ArchitectureImpact) -> None:
    match impact:
        case NoArchitectureImpact():
            return
        case ReadOnlyArchitecture(selector=selector) | UpdateRequiredArchitecture(selector=selector):
            try:
                parse_authority_selector(selector)
            except BriefSourceError as error:
                raise _invalid(f"Architecture impact selector is invalid: {error.message}") from error
        case _ as unreachable:
            assert_never(unreachable)


def _validate_common_checkpoint(checkpoint: WorkBriefCheckpoint) -> tuple[frozenset[int], frozenset[str]]:
    _validate_architecture_impact(checkpoint.architecture_impact)
    criterion_numbers = tuple(value.number for value in checkpoint.acceptance_criteria)
    if len(set(criterion_numbers)) != len(criterion_numbers):
        raise _invalid("Acceptance criterion numbers must be unique.")
    deferral_ids = tuple(value.deferral_id for value in checkpoint.deferrals)
    if len(set(deferral_ids)) != len(deferral_ids):
        raise _invalid("Deferral identities must be unique.")
    return frozenset(criterion_numbers), frozenset(deferral_ids)


def _reviewed_authority_keys(checkpoint: CrossBoundaryCheckpoint) -> frozenset[tuple[str, str]]:
    authority_ids = tuple(value.authority_id for value in checkpoint.reviewed_authorities)
    if len(set(authority_ids)) != len(authority_ids):
        raise _invalid("Reviewed authority identities must be unique.")
    authority_keys_list = [
        (authority.authority_id, family)
        for authority in checkpoint.reviewed_authorities
        for family in authority.families
    ]
    authority_keys = frozenset(authority_keys_list)
    if len(authority_keys) != len(authority_keys_list):
        raise _invalid("Reviewed authority families must be unique.")
    for authority in checkpoint.reviewed_authorities:
        if len(set(authority.families)) != len(authority.families):
            raise _invalid(f"Reviewed authority '{authority.authority_id}' repeats a family.")
        try:
            parse_authority_selector(authority.selector)
        except BriefSourceError as error:
            raise _invalid(
                f"Reviewed authority '{authority.authority_id}' has an invalid selector: {error.message}"
            ) from error
    return authority_keys


def _validate_coverage(
    checkpoint: CrossBoundaryCheckpoint,
    authority_keys: frozenset[tuple[str, str]],
    criteria: frozenset[int],
    deferrals: frozenset[str],
    contracts: frozenset[str],
) -> None:
    coverage_keys = tuple((value.authority_id, value.family) for value in checkpoint.coverage)
    if len(set(coverage_keys)) != len(coverage_keys) or frozenset(coverage_keys) != authority_keys:
        raise _invalid("Coverage must contain exactly one record for every reviewed authority family.")
    for record in checkpoint.coverage:
        match record.owner:
            case ContractCoverageOwner(contract_invariant=invariant):
                if invariant not in contracts:
                    raise _invalid(f"Coverage names unknown contract invariant '{invariant}'.")
            case AcceptanceCoverageOwner(criterion=criterion):
                if criterion not in criteria:
                    raise _invalid(f"Coverage names unknown acceptance criterion '{criterion}'.")
            case DeferredCoverageOwner(deferral_id=deferral_id):
                if deferral_id not in deferrals:
                    raise _invalid(f"Coverage names unknown deferral '{deferral_id}'.")
                if PROHIBITION.search(record.distinction):
                    raise _invalid("An in-scope prohibition cannot be deferred.")
            case NotApplicableCoverageOwner():
                if PROHIBITION.search(record.distinction):
                    raise _invalid("An in-scope prohibition cannot be marked not applicable.")
            case _ as unreachable:
                assert_never(unreachable)


def _validate_cross_boundary_checkpoint(
    brief: WorkBrief,
    checkpoint: CrossBoundaryCheckpoint,
    criteria: frozenset[int],
    deferrals: frozenset[str],
) -> None:
    authority_keys = _reviewed_authority_keys(checkpoint)
    contract_invariants = tuple(value.invariant for value in checkpoint.contracts)
    if len(set(contract_invariants)) != len(contract_invariants):
        raise _invalid("Contract invariants must be unique.")
    for contract in checkpoint.contracts:
        _authorization_key(contract.authorization_basis, brief, authority_keys)
    for record in checkpoint.verification:
        _authorization_key(record.authorization_basis, brief, authority_keys)
    _validate_coverage(checkpoint, authority_keys, criteria, deferrals, frozenset(contract_invariants))
    match checkpoint.lifecycle_partition:
        case NoLifecyclePartition():
            pass
        case RequiredLifecyclePartition(operations=operations):
            operation_ids = tuple(value.operation for value in operations)
            if len(set(operation_ids)) != len(operation_ids):
                raise _invalid("Lifecycle operation identities must be unique.")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_checkpoint(brief: WorkBrief, checkpoint: WorkBriefCheckpoint) -> None:
    criteria, deferrals = _validate_common_checkpoint(checkpoint)
    match checkpoint:
        case LocalCheckpoint():
            for record in checkpoint.verification:
                _authorization_key(record.authorization_basis, brief, frozenset())
        case CrossBoundaryCheckpoint():
            _validate_cross_boundary_checkpoint(brief, checkpoint, criteria, deferrals)
        case _ as unreachable:
            assert_never(unreachable)


def validate_work_brief(brief: WorkBrief) -> None:
    _validate_checkpoint(brief, brief.checkpoint)


def decode_work_brief(data: bytes) -> WorkBrief:
    try:
        brief = msgspec.json.decode(data, type=WorkBrief)
    except msgspec.DecodeError as error:
        raise _invalid(f"Cannot decode canonical work brief: {error}") from error
    validate_work_brief(brief)
    return brief


def canonical_work_brief_bytes(brief: WorkBrief) -> bytes:
    validate_work_brief(brief)
    return _canonical_bytes(brief) + b"\n"


def decode_canonical_work_brief(data: bytes) -> WorkBrief:
    brief = decode_work_brief(data)
    if data != canonical_work_brief_bytes(brief):
        raise WorkBriefError(
            WorkBriefErrorCode.BRIEF_NOT_CANONICAL,
            "Accepted work brief bytes are not the canonical msgspec encoding.",
        )
    return brief


def canonical_checkpoint_bytes(checkpoint: WorkBriefCheckpoint) -> bytes:
    return _canonical_bytes(checkpoint)


def canonical_reviewed_authority_set_bytes(authorities: tuple[ReviewedAuthority, ...]) -> bytes:
    return _canonical_bytes(authorities)


def validate_reviewed_authority_digests(source_checkout_root: Path, authorities: tuple[ReviewedAuthority, ...]) -> None:
    for authority in authorities:
        try:
            selected = select_brief_source(
                source_checkout_root,
                parse_authority_selector(authority.selector),
                require_utf8=True,
            )
        except BriefSourceError as error:
            raise WorkBriefError(
                WorkBriefErrorCode.BRIEF_INVALID,
                f"Cannot read reviewed authority '{authority.authority_id}': {error.message}",
            ) from error
        if hashlib.sha256(selected.content).hexdigest() != authority.reviewed_sha256:
            raise WorkBriefError(
                WorkBriefErrorCode.BRIEF_INVALID,
                f"Reviewed authority '{authority.authority_id}' changed after review.",
            )


def decode_work_brief_review(data: bytes) -> WorkBriefReview:
    try:
        return msgspec.json.decode(data, type=WorkBriefReview)
    except msgspec.DecodeError as error:
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode canonical work brief review: {error}",
        ) from error


def canonical_work_brief_review_bytes(review: WorkBriefReview) -> bytes:
    return _canonical_bytes(review) + b"\n"


def decode_canonical_work_brief_review(data: bytes) -> WorkBriefReview:
    review = decode_work_brief_review(data)
    if data != canonical_work_brief_review_bytes(review):
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Accepted work brief review bytes are not the canonical msgspec encoding.",
        )
    return review


def validate_work_brief_review(
    review: WorkBriefReview,
    brief: WorkBrief,
    reviewer_task_id: str | None = None,
) -> None:
    checkpoint = brief.checkpoint
    if not isinstance(checkpoint, CrossBoundaryCheckpoint):
        raise WorkBriefError(WorkBriefErrorCode.REVIEW_INVALID, "Local checkpoints do not use brief reviews.")
    if review.attempt_id != brief.attempt_id or review.checkpoint_id != checkpoint.checkpoint_id:
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_INVALID,
            "Brief review names a different attempt or checkpoint.",
        )
    owner_task_id = brief.owner_task_id if reviewer_task_id is None else reviewer_task_id
    if review.reviewer_task_id == owner_task_id:
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT,
            "The brief reviewer must be a different task from the attempt owner.",
        )
    if review.checkpoint_sha256 != hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest() or (
        review.reviewed_authority_set_sha256
        != hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest()
    ):
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_STALE,
            "Brief review is not bound to the current checkpoint and reviewed authorities.",
        )
    expected = {(record.authority_id, record.family, _owner_key(record.owner)) for record in checkpoint.coverage}
    observed = {(record.authority_id, record.family, _owner_key(record.owner)) for record in review.coverage}
    if len(observed) != len(review.coverage) or observed != expected:
        raise WorkBriefError(
            WorkBriefErrorCode.REVIEW_NOT_READY,
            "Brief review must contain exactly one covered result for every coverage owner.",
        )


def _authorization_text(
    basis: AcceptedScopeAuthorization
    | AuthorityAuthorization
    | RepositoryPolicyAuthorization
    | ExistingConsumerAuthorization,
) -> str:
    match basis:
        case AcceptedScopeAuthorization(item_id=item_id, scope_revision=revision):
            return f"accepted-scope:{item_id}@{revision}"
        case AuthorityAuthorization(authority_id=authority_id, family=family):
            return f"authority:{authority_id}#{family}"
        case RepositoryPolicyAuthorization(authority_id=authority_id, family=family):
            return f"repository-policy:{authority_id}#{family}"
        case ExistingConsumerAuthorization(authority_id=authority_id, family=family):
            return f"existing-consumer:{authority_id}#{family}"
        case _ as unreachable:
            assert_never(unreachable)


def _architecture_text(impact: ArchitectureImpact) -> str:
    match impact:
        case NoArchitectureImpact(reason=reason):
            return f"none — {reason}"
        case ReadOnlyArchitecture(selector=selector, reason=reason):
            return f"read-only — `{selector}` — {reason}"
        case UpdateRequiredArchitecture(selector=selector, reason=reason):
            return f"update-required — `{selector}` — {reason}"
        case _ as unreachable:
            assert_never(unreachable)


def _section(lines: list[str], heading: str, values: tuple[str, ...]) -> None:
    lines.extend((f"## {heading}", ""))
    lines.extend(f"- {value}" for value in values)
    lines.append("")


def render_work_brief_markdown(brief: WorkBrief, database_revision: int | None = None) -> bytes:
    checkpoint = brief.checkpoint
    lines = [
        "---",
        "kind: work-attempt-view",
        "authority: pinboard-work-brief/v2",
        f"attempt: {brief.attempt_id}",
        f"item_id: {brief.item_id}",
        f"branch: {brief.branch}",
        f"base_revision: {brief.base_revision}",
        f"owner_task_id: {brief.owner_task_id}",
        f"accepted_scope_revision: {brief.accepted_scope.revision}",
        f"accepted_scope_digest: {brief.accepted_scope.digest}",
        f"artifact_revision: {brief.artifact_revision}",
        *((f"database_revision: {database_revision}",) if database_revision is not None else ()),
        "---",
        "",
        "> Generated projection; canonical JSON is authoritative.",
        "",
        f"# {brief.title}",
        "",
        brief.outcome,
        "",
        f"## Checkpoint: {checkpoint.title}",
        "",
        f"- Checkpoint ID: `{checkpoint.checkpoint_id}`",
        f"- Boundary: `{('cross-boundary' if isinstance(checkpoint, CrossBoundaryCheckpoint) else 'local')}`",
        f"- Architecture impact: {_architecture_text(checkpoint.architecture_impact)}",
        "",
        checkpoint.outcome_description,
        "",
    ]
    _section(lines, "Supported production roots", brief.supported_production_roots)
    _section(lines, "Scope", brief.scope)
    _section(lines, "Bootstrap", brief.bootstrap)
    _section(lines, "Compatibility", brief.compatibility)
    _section(lines, "Non-goals", brief.non_goals)
    lines.extend(("## Product decision and provenance", "", brief.product_decision_and_provenance, ""))
    lines.extend(("## Testing strategy", "", brief.testing_strategy, ""))
    if isinstance(checkpoint, CrossBoundaryCheckpoint):
        lines.extend(("## Contract", ""))
        for record in checkpoint.contracts:
            lines.extend(
                (
                    f"### {record.invariant}",
                    "",
                    f"- Authority: {record.authority}",
                    f"- Consumer: {record.consumer}",
                    f"- Failure: {record.failure}",
                    f"- Verification: {record.verification}",
                    f"- Revalidation: {record.revalidation}",
                    f"- Authorization: `{_authorization_text(record.authorization_basis)}`",
                    "",
                )
            )
        lines.extend(("## Reviewed authorities", ""))
        lines.extend(
            (
                f"- `{authority.authority_id}` — `{authority.selector}` — `{authority.reviewed_sha256}` — "
                + ", ".join(f"`{family}`" for family in authority.families)
            )
            for authority in checkpoint.reviewed_authorities
        )
        lines.extend(("", "## Authoritative coverage", ""))
        for record in checkpoint.coverage:
            lines.extend(
                (
                    f"### {record.authority_id}#{record.family}",
                    "",
                    f"- Distinction: {record.distinction}",
                    f"- Consumer: {record.consumer}",
                    f"- Owner: `{_owner_key(record.owner)[0]}:{_owner_key(record.owner)[1]}`",
                    f"- Counterexample: {record.counterexample}",
                    "",
                )
            )
        match checkpoint.lifecycle_partition:
            case NoLifecyclePartition(reason=reason):
                lines.extend(("## Lifecycle partition", "", f"Not applicable — {reason}", ""))
            case RequiredLifecyclePartition(operations=operations):
                lines.extend(("## Lifecycle partition", ""))
                for operation in operations:
                    lines.extend(
                        (
                            f"### {operation.operation}",
                            "",
                            f"- Source state: {operation.source_state}",
                            f"- Authority: {operation.authority}",
                            f"- Evidence: {operation.evidence}",
                            f"- Effects: {operation.effects}",
                            f"- Illegal sibling: {operation.illegal_sibling}",
                            "",
                        )
                    )
            case _ as unreachable:
                assert_never(unreachable)
    lines.extend(("## Acceptance criteria", ""))
    lines.extend(f"{value.number}. {value.requirement}" for value in checkpoint.acceptance_criteria)
    lines.extend(("", "## Verification", ""))
    lines.extend(
        f"- `{_authorization_text(value.authorization_basis)}` — `{value.obligation}`"
        for value in checkpoint.verification
    )
    lines.extend(("", "## Deferrals", ""))
    lines.extend(
        f"- `{value.deferral_id}` — {value.reason} Reopen when: {value.reopen_when}" for value in checkpoint.deferrals
    )
    lines.extend(("", "## Remaining work", "", brief.remaining_work, ""))
    return "\n".join(lines).encode()


def read_work_brief(path: Path, *, canonical: bool = True) -> WorkBrief:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise WorkBriefError(WorkBriefErrorCode.BRIEF_INVALID, f"Cannot read work brief '{path}': {error}") from error
    return decode_canonical_work_brief(data) if canonical else decode_work_brief(data)


def decode_work_brief_identity(data: bytes) -> WorkBriefIdentity:
    brief = decode_canonical_work_brief(data)
    return WorkBriefIdentity(
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    )


def build_attempt_brief_views(state: StoredWorkState, artifacts: ArtifactReader) -> dict[AttemptId, bytes]:
    result: dict[AttemptId, bytes] = {}
    references = {value.artifact_ref_id: value for value in state.artifact_references}
    for attempt in state.lifecycle.attempts:
        if attempt.state == work_models.AttemptState.DONE:
            continue
        reference = references.get(attempt.brief_artifact_ref_id)
        if reference is None or reference.kind != ArtifactKind.BRIEF:
            raise _invalid(f"Live attempt '{attempt.attempt_id}' has no accepted brief reference.")
        if not reference.selector.endswith(".json"):
            raise _invalid(f"Live attempt '{attempt.attempt_id}' accepted brief is not canonical v2 JSON.")
        artifacts.verify(reference)
        brief = read_work_brief(artifacts.path(reference))
        expected = (
            str(attempt.attempt_id),
            str(attempt.item_id),
            attempt.branch,
            attempt.base_revision,
            attempt.accepted_scope_revision,
            attempt.accepted_scope_digest,
        )
        observed = (
            brief.attempt_id,
            brief.item_id,
            brief.branch,
            brief.base_revision,
            brief.accepted_scope.revision,
            brief.accepted_scope.digest,
        )
        if observed != expected:
            raise _invalid(f"Live attempt '{attempt.attempt_id}' brief identity does not match SQLite.")
        result[attempt.attempt_id] = render_work_brief_markdown(brief, state.lifecycle.project.revision)
    return result
