from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, NewArtifact, WorkBriefIdentity
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode


class ArtifactPublisher(Protocol):
    @property
    def work_root(self) -> Path: ...

    def publish(self, artifact: NewArtifact) -> ArtifactRef: ...


class ArtifactReader(Protocol):
    def verify(self, reference: stored_state.ArtifactReference) -> None: ...

    def path(self, reference: stored_state.ArtifactReference) -> Path: ...


def publish_accepted_artifact(
    store: WorkStore,
    publisher: ArtifactPublisher,
    artifact: NewArtifact,
    accepted_at: datetime,
) -> stored_state.ArtifactReference:
    """Publish immutable bytes, then accept their verified reference in SQLite."""

    published = publisher.publish(artifact)
    return store.accept_artifact_reference(publisher.work_root, published, accepted_at)


def validate_transition_work_brief(
    state: stored_state.StoredWorkState,
    command: decision_models.TransitionCommand,
    artifacts: ArtifactReader,
    decode_identity: Callable[[bytes], WorkBriefIdentity],
) -> DecisionFailure | None:
    """Validate activation or resume brief identity against the locked SQLite snapshot."""

    match command:
        case decision_models.ActivateCommand(action=action, value=value):
            artifact_ref_id = value.brief_artifact_ref_id
            attempt_id = str(value.attempt)
            item_id = str(action.capability.subject)
            branch = value.branch
            base_revision = value.base_revision
        case decision_models.ResumeCommand(action=action, value=value) if value.brief_artifact_ref_id is not None:
            artifact_ref_id = value.brief_artifact_ref_id
            item_id = str(action.capability.subject)
            attempt = next(
                (candidate for candidate in state.lifecycle.attempts if str(candidate.item_id) == item_id), None
            )
            if attempt is None:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Resuming with a revised brief requires an existing attempt.",
                )
            attempt_id = str(attempt.attempt_id)
            branch = attempt.branch
            base_revision = attempt.base_revision
        case _:
            return None
    reference = next(
        (candidate for candidate in state.artifact_references if candidate.artifact_ref_id == artifact_ref_id),
        None,
    )
    if reference is None or reference.kind != stored_state.ArtifactKind.BRIEF:
        return None
    artifacts.verify(reference)
    try:
        identity = decode_identity(artifacts.path(reference).read_bytes())
    except (OSError, ValueError) as error:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"The selected brief artifact is not a valid canonical typed work brief: {error}",
        )
    item = next((candidate for candidate in state.lifecycle.work_items if str(candidate.item_id) == item_id), None)
    if item is None:
        return None
    expected = WorkBriefIdentity(
        attempt_id,
        item_id,
        branch,
        base_revision,
        item.scope_revision,
        item.scope_digest,
    )
    if identity != expected:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The selected brief artifact does not match the attempt, item, branch, base revision, and accepted scope.",
        )
    return None
