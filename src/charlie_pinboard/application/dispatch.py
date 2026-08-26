from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from charlie_pinboard.application.actions import discover_actions
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.dispatch_models import (
    BriefReviewPublisher,
    DispatchArtifactPort,
    DispatchBriefPreparer,
    DispatchEnvironment,
)
from charlie_pinboard.application.errors import ActionQueryError, DispatchError, DispatchErrorCode
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.domain.decision_models import (
    Action,
    ActionKind,
    Role,
)
from charlie_pinboard.domain.identifiers import AttemptId, ItemId, LeaseId
from charlie_pinboard.domain.work_models import (
    ArtifactRole,
    AttemptState,
)


def _current_action(store: WorkStore, supplied: Action) -> Action:
    try:
        actions = discover_actions(
            store,
            Role.COORDINATOR,
            lease_id=supplied.lease_id,
            generation=supplied.coordinator_generation,
        )
    except ActionQueryError as error:
        raise DispatchError(DispatchErrorCode(error.code.value), str(error).partition(": ")[2]) from error
    current = next((value for value in actions if value.action_id == supplied.action_id), None)
    if current is None or supplied.kind != ActionKind.DISPATCH:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE, f"Action '{supplied.action_id}' is not available."
        )
    if current != supplied:
        if current.expected_revision != supplied.expected_revision:
            raise DispatchError(
                DispatchErrorCode.STALE_ACTION,
                "The work ledger changed after this dispatch action was selected.",
            )
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ACTION_INVALID, "The dispatch action does not carry exact current authority."
        )
    return current


def _review_publisher(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    attempt_id: AttemptId,
    item_id: ItemId,
    publication_revisions: list[int],
) -> BriefReviewPublisher:
    def publish(checkpoint_sha256: str, candidate: bytes | None, review_id: str | None) -> tuple[bytes, str]:
        key = f"{attempt_id}-brief-review-{checkpoint_sha256}"
        state = store.snapshot()
        existing = next(
            (
                value
                for value in state.artifact_references
                if value.kind == ArtifactKind.EVIDENCE and value.key == key and value.revision == 1
            ),
            None,
        )
        if candidate is None:
            if review_id is not None:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID, "--review-id requires --brief-review."
                )
            if existing is None:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING, "The exact ready brief review is absent."
                )
            artifacts.verify(existing)
            path = artifacts.path(existing)
            return path.read_bytes(), str(path)
        if (
            review_id is None
            or not review_id
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in review_id)
        ):
            raise DispatchError(
                DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                "--brief-review requires one kebab-case --review-id.",
            )
        if existing is not None:
            artifacts.verify(existing)
            ready_path = artifacts.path(existing)
            if ready_path.read_bytes() == candidate:
                return candidate, str(ready_path)
            rejected = artifacts.publish(
                NewArtifact(
                    ArtifactKind.EVIDENCE,
                    f"{key}-rejected-{review_id}",
                    1,
                    ".md",
                    candidate,
                ),
            )
            store.accept_artifact_reference(
                artifacts.work_root,
                rejected,
                datetime.now(UTC),
                item_id=item_id,
                role=ArtifactRole.EVIDENCE,
            )
            raise DispatchError(
                DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION,
                f"Ready review already differs; later evidence is preserved at '{rejected.selector}'.",
            )
        published = artifacts.publish(NewArtifact(ArtifactKind.EVIDENCE, key, 1, ".md", candidate))
        accepted = store.accept_artifact_reference(
            artifacts.work_root,
            published,
            datetime.now(UTC),
            item_id=item_id,
            role=ArtifactRole.EVIDENCE,
        )
        publication_revisions.append(accepted.accepted_revision)
        return candidate, str(artifacts.work_root / accepted.selector)

    return publish


def prepare_dispatch(
    store: WorkStore,
    artifacts: DispatchArtifactPort,
    prepare_brief: DispatchBriefPreparer,
    project_root: Path,
    action: Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
    brief_review: bytes | None = None,
    review_id: str | None = None,
) -> str:
    _current_action(store, action)
    state = store.snapshot()
    attempt_id = AttemptId(action.subject)
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if attempt is None or attempt.state != AttemptState.ACTIVE:
        raise DispatchError(DispatchErrorCode.DISPATCH_ATTEMPT_NOT_ACTIVE, f"Attempt '{attempt_id}' is not active.")
    reference = next(
        (
            value
            for value in state.artifact_references
            if value.artifact_ref_id == attempt.brief_artifact_ref_id and value.kind == ArtifactKind.BRIEF
        ),
        None,
    )
    if reference is None:
        raise DispatchError(DispatchErrorCode.DISPATCH_BRIEF_MISSING, "The attempt has no accepted brief artifact.")
    artifacts.verify(reference)
    attempt_path = artifacts.path(reference)
    publication_revisions: list[int] = []
    publisher = _review_publisher(
        store,
        artifacts,
        attempt.attempt_id,
        attempt.item_id,
        publication_revisions,
    )

    prompt = prepare_brief(
        attempt_path,
        str(attempt.attempt_id),
        attempt.branch,
        project_root,
        checkpoint,
        environment,
        supplied_prompt,
        brief_review,
        review_id,
        publisher,
    )
    current = next(
        (
            value
            for value in discover_actions(
                store,
                Role.COORDINATOR,
                lease_id=LeaseId(action.lease_id) if action.lease_id is not None else None,
                generation=action.coordinator_generation,
            )
            if value.action_id == action.action_id
        ),
        None,
    )
    current_matches = current == action
    if current is not None and publication_revisions:
        current_matches = (
            current.expected_revision == str(publication_revisions[-1])
            and replace(current, expected_revision=action.expected_revision) == action
        )
    if not current_matches:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ACTION_UNAVAILABLE, "Dispatch authority changed during prompt preparation."
        )
    return prompt
