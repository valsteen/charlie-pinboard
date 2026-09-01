import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.dispatch import (
    confirm_dispatch_authority,
    find_dispatch_review,
    publish_dispatch_review,
    select_dispatch,
)
from pinboard.application.dispatch_models import (
    DispatchArtifactPort,
    DispatchEnvironment,
    DispatchRejectionCode,
)
from pinboard.application.dispatch_models import DispatchFailure as ApplicationDispatchFailure
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.domain.identifiers import ReviewId
from pinboard.interfaces import action_selection, cli_commands
from pinboard.interfaces.errors import (
    CliResult,
    CommandFailure,
    DispatchErrorCode,
    DispatchFailure,
    DispatchResult,
    WorkBriefError,
    WorkBriefErrorCode,
)
from pinboard.interfaces.work_brief_models import (
    CrossBoundaryCheckpoint,
    LocalCheckpoint,
    ReviewedAuthorityDigestMismatch,
    ReviewedAuthoritySelectionFailure,
    WorkBrief,
)
from pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_work_brief_review_bytes,
    decode_canonical_work_brief_review,
    decode_work_brief_review,
    read_work_brief,
    validate_reviewed_authority_digests,
    validate_work_brief_review,
)


@dataclass(frozen=True, slots=True)
class SuppliedDispatchReview:
    content: bytes
    review_id: ReviewId


@dataclass(frozen=True, slots=True)
class ExistingDispatchReview:
    checkpoint_sha256: str


@dataclass(frozen=True, slots=True)
class NewDispatchReview:
    checkpoint_sha256: str
    candidate: bytes
    review_id: ReviewId


type DispatchReview = ExistingDispatchReview | NewDispatchReview


def read_dispatch_environment(path: Path) -> DispatchResult[DispatchEnvironment]:
    try:
        data = path.read_bytes()
    except OSError as error:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE,
            f"Cannot read '{path}': {error}",
        )
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment)
    except msgspec.DecodeError as error:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID,
            f"Cannot decode dispatch environment: {error}",
        )


def _review_failure(error: WorkBriefError) -> DispatchFailure:
    match error.code:
        case (
            WorkBriefErrorCode.BRIEF_INVALID
            | WorkBriefErrorCode.BRIEF_NOT_CANONICAL
            | WorkBriefErrorCode.REVIEW_INVALID
            | WorkBriefErrorCode.REVIEW_NOT_CANONICAL
        ):
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID
        case WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT
        case WorkBriefErrorCode.REVIEW_NOT_READY:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_READY
        case WorkBriefErrorCode.REVIEW_STALE:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE
        case _ as unreachable:
            assert_never(unreachable)
    return DispatchFailure(code, error.message)


def _dispatch_failure(failure: ApplicationDispatchFailure) -> DispatchFailure:
    match failure.code:
        case DecisionFailureCode() as code:
            return DispatchFailure(code, failure.message)
        case DispatchRejectionCode.ACTION_INVALID:
            code = DispatchErrorCode.DISPATCH_ACTION_INVALID
        case DispatchRejectionCode.ACTION_UNAVAILABLE:
            code = DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE
        case DispatchRejectionCode.ATTEMPT_NOT_ACTIVE:
            code = DispatchErrorCode.DISPATCH_ATTEMPT_NOT_ACTIVE
        case DispatchRejectionCode.BRIEF_MISSING:
            code = DispatchErrorCode.DISPATCH_BRIEF_MISSING
        case DispatchRejectionCode.REVIEW_COLLISION:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION
        case DispatchRejectionCode.REVIEW_MISSING:
            code = DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING
        case DispatchRejectionCode.STALE_ACTION:
            code = DispatchErrorCode.STALE_ACTION
        case _ as unreachable:
            assert_never(unreachable)
    return DispatchFailure(code, failure.message)


def _canonical_prompt(
    attempt_path: Path,
    attempt_id: str,
    checkpoint_id: str,
    environment: DispatchEnvironment,
) -> str:
    permissions = ", ".join(sorted(permission.value for permission in environment.permissions)) or "none"
    return (
        "Use $pinboard-deliver for this repository attempt.\n\n"
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
) -> DispatchFailure | None:
    if brief.attempt_id != attempt_id:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different attempt."
        )
    if accepted_item_id is not None and brief.item_id != accepted_item_id:
        return DispatchFailure(DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different item.")
    if accepted_scope_revision is not None and brief.accepted_scope.revision != accepted_scope_revision:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope revision.",
        )
    if accepted_scope_digest is not None and brief.accepted_scope.digest != accepted_scope_digest:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief names a different accepted scope digest.",
        )
    if brief.branch != attempt_branch or environment.branch != attempt_branch:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            "Canonical brief, attempt, and dispatch environment branches must match.",
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKOUT_MISSING, f"Checkout '{checkout}' is not a directory."
        )
    if checkout.resolve() != source_checkout_root.resolve():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKOUT_MISMATCH,
            "The dispatch environment checkout must match the selected source checkout.",
        )
    if brief.checkpoint.checkpoint_id != checkpoint_id:
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING,
            f"Checkpoint '{checkpoint_id}' is not the current canonical checkpoint.",
        )
    return None


def _read_dispatch_brief(
    attempt_path: Path,
    attempt_id: str,
    attempt_branch: str,
    source_checkout_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_item_id: str | None,
    accepted_scope_revision: int | None,
    accepted_scope_digest: str | None,
) -> DispatchResult[WorkBrief]:
    try:
        brief = read_work_brief(attempt_path)
    except WorkBriefError as error:
        return DispatchFailure(DispatchErrorCode.DISPATCH_BRIEF_INVALID, error.message)
    if (
        failure := _validate_dispatch_identity(
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
    ) is not None:
        return failure
    if isinstance(brief.checkpoint, CrossBoundaryCheckpoint):
        failure = validate_reviewed_authority_digests(source_checkout_root, brief.checkpoint.reviewed_authorities)
        match failure:
            case None:
                pass
            case ReviewedAuthoritySelectionFailure(authority_id=authority_id, reason=reason):
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE,
                    f"Cannot read reviewed authority '{authority_id}': {reason}",
                )
            case ReviewedAuthorityDigestMismatch(authority_id=authority_id):
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_AUTHORITY_STALE,
                    f"Reviewed authority '{authority_id}' changed after review.",
                )
            case _ as unreachable:
                assert_never(unreachable)
    return brief


def _review_candidate(
    brief: WorkBrief,
    supplied_review: SuppliedDispatchReview | None,
) -> DispatchResult[DispatchReview | None]:
    match brief.checkpoint:
        case LocalCheckpoint():
            if supplied_review is not None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                )
            return None
        case CrossBoundaryCheckpoint() as checkpoint:
            if supplied_review is None:
                return ExistingDispatchReview(hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest())
            try:
                review = decode_work_brief_review(supplied_review.content)
                validate_work_brief_review(review, brief)
                candidate = canonical_work_brief_review_bytes(review)
            except WorkBriefError as error:
                return _review_failure(error)
            return NewDispatchReview(review.checkpoint_sha256, candidate, supplied_review.review_id)
        case _ as unreachable:
            assert_never(unreachable)


def _validate_accepted_review(brief: WorkBrief, accepted_review: bytes | None) -> DispatchFailure | None:
    match brief.checkpoint:
        case LocalCheckpoint():
            if accepted_review is not None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not use cross-boundary brief reviews.",
                )
        case CrossBoundaryCheckpoint():
            if accepted_review is None:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING,
                    "The exact ready brief review is absent.",
                )
            try:
                review = decode_canonical_work_brief_review(accepted_review)
                validate_work_brief_review(review, brief)
            except WorkBriefError as error:
                return _review_failure(error)
        case _ as unreachable:
            assert_never(unreachable)
    return None


def _render_dispatch_prompt(
    brief: WorkBrief,
    attempt_path: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    accepted_review: bytes | None,
    supplied_prompt: bytes | None,
) -> DispatchResult[str]:
    if (failure := _validate_accepted_review(brief, accepted_review)) is not None:
        return failure
    prompt = _canonical_prompt(attempt_path, brief.attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        return DispatchFailure(
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt


def prepare_dispatch(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    source_checkout_root: Path,
    action: decision_models.Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
    supplied_review: SuppliedDispatchReview | None = None,
) -> DispatchResult[str]:
    selected = select_dispatch(store, action, datetime.now(UTC))
    if isinstance(selected, ApplicationDispatchFailure):
        return _dispatch_failure(selected)
    assert isinstance(action, decision_models.DispatchAction)
    attempt, reference = selected
    artifacts.verify(reference)
    attempt_path = artifacts.path(reference)
    brief = _read_dispatch_brief(
        attempt_path,
        str(attempt.attempt_id),
        attempt.branch,
        source_checkout_root,
        checkpoint,
        environment,
        str(attempt.item_id),
        attempt.accepted_scope_revision,
        attempt.accepted_scope_digest,
    )
    if isinstance(brief, DispatchFailure):
        return brief
    request = _review_candidate(brief, supplied_review)
    if isinstance(request, DispatchFailure):
        return request
    accepted_review: bytes | None = None
    publication_revision: int | None = None
    match request:
        case None:
            pass
        case ExistingDispatchReview(checkpoint_sha256=checkpoint_sha256):
            accepted = find_dispatch_review(store, attempt.attempt_id, checkpoint_sha256)
            if isinstance(accepted, ApplicationDispatchFailure):
                return _dispatch_failure(accepted)
            accepted_reference = accepted
            artifacts.verify(accepted_reference)
            accepted_review = artifacts.path(accepted_reference).read_bytes()
        case NewDispatchReview(
            checkpoint_sha256=checkpoint_sha256,
            candidate=candidate,
            review_id=review_id,
        ):
            publication = publish_dispatch_review(
                store,
                artifacts,
                attempt.attempt_id,
                checkpoint_sha256,
                candidate,
                review_id,
                datetime.now(UTC),
            )
            if isinstance(publication, ApplicationDispatchFailure):
                return _dispatch_failure(publication)
            accepted_reference, publication_revision = publication
            artifacts.verify(accepted_reference)
            accepted_review = artifacts.path(accepted_reference).read_bytes()
        case _ as unreachable:
            assert_never(unreachable)
    prompt = _render_dispatch_prompt(
        brief,
        attempt_path,
        checkpoint,
        environment,
        accepted_review,
        supplied_prompt,
    )
    if isinstance(prompt, DispatchFailure):
        return prompt
    if (failure := confirm_dispatch_authority(store, action, publication_revision, datetime.now(UTC))) is not None:
        return _dispatch_failure(failure)
    return prompt


def prepare_dispatch_command(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.DispatchCommand,
) -> CliResult[int]:
    """Prepare one installed dispatch request and return every advertised rejection."""

    environment = read_dispatch_environment(command.environment)
    if isinstance(environment, DispatchFailure):
        return environment
    supplied_prompt: bytes | None = None
    if command.prompt is not None:
        try:
            supplied_prompt = command.prompt.read_bytes()
        except OSError as error:
            return DispatchFailure(
                DispatchErrorCode.DISPATCH_PROMPT_UNREADABLE,
                f"Cannot read '{command.prompt}': {error}",
            )
    match command:
        case cli_commands.CoordinatorReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id) | (
            cli_commands.CoordinationReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id)
        ):
            try:
                supplied_review = SuppliedDispatchReview(brief_review_path.read_bytes(), review_id)
            except OSError as error:
                return DispatchFailure(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
                    f"Cannot read '{brief_review_path}': {error}",
                )
        case cli_commands.CoordinatorDispatchCommand() | cli_commands.CoordinationDispatchCommand():
            supplied_review = None
        case _ as unreachable:
            assert_never(unreachable)
    action = action_selection.action_from_command(command)
    if isinstance(action, CommandFailure):
        return action
    action = action_selection.reselect_action(roots, action, decision_models.Role.COORDINATOR)
    if isinstance(action, CommandFailure):
        return action
    prompt = prepare_dispatch(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
        roots.source_checkout,
        action,
        command.checkpoint,
        environment,
        supplied_prompt,
        supplied_review,
    )
    if isinstance(prompt, DispatchFailure):
        return prompt
    if supplied_prompt is None:
        print(prompt, end="")
    else:
        print("OK DISPATCH_READY")
    return 0
