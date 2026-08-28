import hashlib
from pathlib import Path
from typing import assert_never

import msgspec

from charlie_pinboard.application.dispatch_models import BriefReviewPublisher, DispatchEnvironment
from charlie_pinboard.application.errors import DispatchError, DispatchErrorCode
from charlie_pinboard.interfaces.errors import WorkBriefError, WorkBriefErrorCode
from charlie_pinboard.interfaces.work_brief_models import CrossBoundaryCheckpoint, LocalCheckpoint, WorkBrief
from charlie_pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_work_brief_review,
    decode_work_brief_review,
    read_work_brief,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
)


def read_dispatch_environment(path: Path) -> DispatchEnvironment:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE,
            f"Cannot read '{path}': {error}",
        ) from error
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment)
    except msgspec.DecodeError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
            f"Cannot decode dispatch environment: {error}",
        ) from error


def _review_error(error: WorkBriefError) -> DispatchError:
    match error.code:
        case WorkBriefErrorCode.BRIEF_INVALID | WorkBriefErrorCode.BRIEF_NOT_CANONICAL:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID
        case WorkBriefErrorCode.REVIEW_INVALID | WorkBriefErrorCode.REVIEW_NOT_CANONICAL:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID
        case WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT
        case WorkBriefErrorCode.REVIEW_NOT_READY:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_READY
        case WorkBriefErrorCode.REVIEW_STALE:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE
        case _ as unreachable:
            assert_never(unreachable)
    return DispatchError(code, error.message)


def _canonical_prompt(
    attempt_path: Path,
    attempt_id: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
) -> str:
    permissions = ", ".join(sorted(permission.value for permission in environment.permissions)) or "none"
    return (
        "Use $deliver for this repository attempt.\n\n"
        f"Attempt: {attempt_id}\n"
        f"Checkpoint: {checkpoint_id}\n"
        f"Canonical brief: {attempt_path}\n\n"
        "Read and follow that canonical attempt brief. It is the sole semantic execution contract. "
        "Do not restate, narrow, defer, or add acceptance semantics in this launch.\n\n"
        "Execution environment:\n"
        f"- Checkout: {environment.checkout}\n"
        f"- Branch: {environment.branch}\n"
        f"- Starting revision: {environment.starting_revision}\n"
        f"- Permissions: {permissions}\n"
    )


def _validate_cross_boundary_review(
    brief: WorkBrief,
    checkpoint: CrossBoundaryCheckpoint,
    brief_review: bytes | None,
    review_id: str | None,
    review_publisher: BriefReviewPublisher | None,
) -> None:
    candidate: bytes | None = None
    if brief_review is not None:
        try:
            review = decode_work_brief_review(brief_review)
            validate_work_brief_review(review, brief)
            candidate = canonical_work_brief_review_bytes(review)
        except WorkBriefError as error:
            raise _review_error(error) from error
        checkpoint_sha256 = review.checkpoint_sha256
    else:
        checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
    if review_publisher is None:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING,
            "Current SQLite dispatch requires application-owned review publication.",
        )
    review_bytes, _source = review_publisher(checkpoint_sha256, candidate, review_id)
    try:
        accepted = decode_canonical_work_brief_review(review_bytes)
        validate_work_brief_review(accepted, brief)
    except WorkBriefError as error:
        raise _review_error(error) from error


def _validate_dispatch_identity(
    brief: WorkBrief,
    attempt_id: str,
    attempt_branch: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
    accepted_item_id: str | None,
    accepted_scope_revision: int | None,
    accepted_scope_digest: str | None,
    source_checkout_root: Path,
) -> None:
    if brief.attempt_id != attempt_id:
        raise DispatchError(DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different attempt.")
    if accepted_item_id is not None and brief.item_id != accepted_item_id:
        raise DispatchError(DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different item.")
    if accepted_scope_revision is not None and brief.accepted_scope.revision != accepted_scope_revision:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope revision.",
        )
    if accepted_scope_digest is not None and brief.accepted_scope.digest != accepted_scope_digest:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope digest.",
        )
    if brief.branch != attempt_branch or environment.branch != attempt_branch:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            "Canonical brief, attempt, and dispatch environment branches must match.",
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        raise DispatchError(DispatchErrorCode.DISPATCH_CHECKOUT_MISSING, f"Checkout '{checkout}' is not a directory.")
    if checkout.resolve() != source_checkout_root.resolve():
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CHECKOUT_MISMATCH,
            "The dispatch environment checkout must match the selected source checkout.",
        )
    if brief.checkpoint.checkpoint_id != checkpoint_id:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING,
            f"Checkpoint '{checkpoint_id}' is not the current canonical checkpoint.",
        )


def _validate_checkpoint_dispatch(
    brief: WorkBrief,
    source_checkout_root: Path,
    brief_review: bytes | None,
    review_id: str | None,
    review_publisher: BriefReviewPublisher | None,
) -> None:
    match brief.checkpoint:
        case LocalCheckpoint():
            if brief_review is not None or review_id is not None:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                )
        case CrossBoundaryCheckpoint(reviewed_authorities=authorities) as checkpoint:
            try:
                validate_reviewed_authority_digests(source_checkout_root, authorities)
            except WorkBriefError as error:
                code = (
                    DispatchErrorCode.DISPATCH_AUTHORITY_STALE
                    if "changed after review" in error.message
                    else DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE
                )
                raise DispatchError(code, error.message) from error
            _validate_cross_boundary_review(brief, checkpoint, brief_review, review_id, review_publisher)
        case _ as unreachable:
            assert_never(unreachable)


def prepare_dispatch_from_artifact(
    attempt_path: Path,
    attempt_id: str,
    attempt_branch: str,
    source_checkout_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    *,
    accepted_item_id: str | None = None,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    supplied_prompt: bytes | None = None,
    brief_review: bytes | None = None,
    review_id: str | None = None,
    review_publisher: BriefReviewPublisher | None = None,
) -> str:
    """Preserve the accepted typed brief contract after SQLite selected its identity."""

    try:
        brief = read_work_brief(attempt_path)
    except WorkBriefError as error:
        raise DispatchError(DispatchErrorCode.DISPATCH_BRIEF_INVALID, error.message) from error
    _validate_dispatch_identity(
        brief,
        attempt_id,
        attempt_branch,
        checkpoint,
        environment,
        accepted_item_id,
        accepted_scope_revision,
        accepted_scope_digest,
        source_checkout_root,
    )
    _validate_checkpoint_dispatch(brief, source_checkout_root, brief_review, review_id, review_publisher)
    prompt = _canonical_prompt(attempt_path, attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        raise DispatchError(
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt
