from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol

import msgspec

from charlie_pinboard.application.actions import ActionQueryError, discover_actions
from charlie_pinboard.application.artifacts import ArtifactRef, ArtifactReferenceStore, NewArtifact, accept_reference
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.stored_state import ArtifactKind, ArtifactReference
from charlie_pinboard.domain.decisions import Action, ActionKind, Role
from charlie_pinboard.domain.identifiers import AttemptId, ItemId, LeaseId
from charlie_pinboard.domain.model import ArtifactRole, AttemptState

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]
type DispatchSchema = Literal["repo-work-dispatch/v1"]
type BriefReviewPublisher = Callable[[str, bytes | None, str | None], tuple[bytes, str]]


class DispatchError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class DispatchPermission(Enum):
    REPOSITORY_READ = "repository-read"
    REPOSITORY_WRITE = "repository-write"
    NETWORK = "network"
    EXTERNAL_WRITE = "external-write"
    LIVE_APPLICATION = "live-application"


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
    checkout: NonEmptyLine
    branch: NonEmptyLine
    starting_revision: NonEmptyLine
    permissions: tuple[DispatchPermission, ...]


class DispatchStore(WorkStore, ArtifactReferenceStore, Protocol):
    pass


class DispatchArtifactPort(Protocol):
    @property
    def work_root(self) -> Path: ...

    def verify(self, reference: ArtifactReference) -> None: ...

    def path(self, reference: ArtifactReference) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...


class DispatchBriefPreparer(Protocol):
    def __call__(
        self,
        attempt_path: Path,
        attempt_id: str,
        attempt_branch: str,
        project_root: Path,
        checkpoint: str,
        environment: DispatchEnvironment,
        supplied_prompt: bytes | None = None,
        brief_review: bytes | None = None,
        review_id: str | None = None,
        review_publisher: BriefReviewPublisher | None = None,
    ) -> str: ...


def _current_action(store: WorkStore, supplied: Action) -> Action:
    try:
        actions = discover_actions(
            store,
            Role.COORDINATOR,
            lease_id=supplied.lease_id,
            generation=supplied.coordinator_generation,
        )
    except ActionQueryError as error:
        raise DispatchError(error.code, str(error).partition(": ")[2]) from error
    current = next((value for value in actions if value.action_id == supplied.action_id), None)
    if current is None or supplied.kind != ActionKind.DISPATCH:
        raise DispatchError("DISPATCH_ACTION_UNAVAILABLE", f"Action '{supplied.action_id}' is not available.")
    if current != supplied:
        if current.expected_revision != supplied.expected_revision:
            raise DispatchError("STALE_ACTION", "The work ledger changed after this dispatch action was selected.")
        raise DispatchError("DISPATCH_ACTION_INVALID", "The dispatch action does not carry exact current authority.")
    return current


def _review_publisher(
    store: DispatchStore,
    artifacts: DispatchArtifactPort,
    attempt_id: AttemptId,
    item_id: ItemId,
) -> BriefReviewPublisher:
    def publish(checkpoint_sha256: str, candidate: bytes | None, review_id: str | None) -> tuple[bytes, str]:
        key = f"{attempt_id}-brief-review-{checkpoint_sha256}"
        state = store.snapshot()
        existing = next(
            (
                value
                for value in state.artifacts.references
                if value.kind == ArtifactKind.EVIDENCE and value.key == key and value.revision == 1
            ),
            None,
        )
        if candidate is None:
            if review_id is not None:
                raise DispatchError("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", "--review-id requires --brief-review.")
            if existing is None:
                raise DispatchError("DISPATCH_BRIEF_REVIEW_MISSING", "The exact ready brief review is absent.")
            artifacts.verify(existing)
            path = artifacts.path(existing)
            return path.read_bytes(), str(path)
        if (
            review_id is None
            or not review_id
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in review_id)
        ):
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
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
            accept_reference(
                store,
                artifacts.work_root,
                rejected,
                datetime.now(UTC),
                item_id=item_id,
                role=ArtifactRole.EVIDENCE,
            )
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_COLLISION",
                f"Ready review already differs; later evidence is preserved at '{rejected.selector}'.",
            )
        published = artifacts.publish(NewArtifact(ArtifactKind.EVIDENCE, key, 1, ".md", candidate))
        accepted = accept_reference(
            store,
            artifacts.work_root,
            published,
            datetime.now(UTC),
            item_id=item_id,
            role=ArtifactRole.EVIDENCE,
        )
        return candidate, str(artifacts.work_root / accepted.selector)

    return publish


def prepare_sqlite_dispatch(
    store: DispatchStore,
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
        raise DispatchError("DISPATCH_ATTEMPT_NOT_ACTIVE", f"Attempt '{attempt_id}' is not active.")
    resource_backed_mutating = DispatchPermission.REPOSITORY_WRITE in environment.permissions and any(
        value.item_id == attempt.item_id for value in state.resources.requirements
    )
    reference = next(
        (
            value
            for value in state.artifacts.references
            if value.artifact_ref_id == attempt.brief_artifact_ref_id and value.kind == ArtifactKind.BRIEF
        ),
        None,
    )
    if reference is None:
        raise DispatchError("DISPATCH_BRIEF_MISSING", "The attempt has no accepted brief artifact.")
    artifacts.verify(reference)
    attempt_path = artifacts.path(reference)
    publisher = _review_publisher(store, artifacts, attempt.attempt_id, attempt.item_id)

    def validate_without_publication(
        checkpoint_sha256: str,
        candidate: bytes | None,
        candidate_review_id: str | None,
    ) -> tuple[bytes, str]:
        if candidate is None:
            return publisher(checkpoint_sha256, None, candidate_review_id)
        if (
            candidate_review_id is None
            or not candidate_review_id
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in candidate_review_id)
        ):
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
                "--brief-review requires one kebab-case --review-id.",
            )
        return candidate, "--brief-review"

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
        validate_without_publication if resource_backed_mutating else publisher,
    )
    if resource_backed_mutating:
        raise DispatchError(
            "RESOURCE_BACKED_MUTATING_DISPATCH_UNSUPPORTED",
            "Resource-backed mutating dispatch remains unavailable pending the recorded post-cutover revalidation.",
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
    if current is None or replace(current, expected_revision=action.expected_revision) != action:
        raise DispatchError("DISPATCH_ACTION_UNAVAILABLE", "Dispatch authority changed during prompt preparation.")
    return prompt
