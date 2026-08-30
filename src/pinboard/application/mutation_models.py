from dataclasses import dataclass

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_models import AttemptAuthorityDecision, CoordinationAuthorityDecision
from pinboard.domain.identifiers import ArtifactRefId, HistoryId, HistorySubjectId, HostId, TaskId
from pinboard.domain.proposal_models import ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Stored-history identity for an ordinary transition receipt."""

    transition: decision_models.TransitionReceipt
    history_id: HistoryId
    project_revision: int
    action_kind: stored_state.TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: stored_state.TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: work_models.CanonicalJson


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    receipt: MutationReceipt
    decision: ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class CheckpointArtifactChanges:
    """Exact identities assigned to one accepted checkpoint result and review."""

    result: ArtifactRef
    result_id: ArtifactRefId
    review: ArtifactRef
    review_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class TransitionMutation:
    """Persists one accepted closed lifecycle decision as an exact relational delta."""

    decision: decision_models.Decision
    receipt: MutationReceipt
    focus_after: stored_state.StoredFocus | None
    checkpoint_artifacts: CheckpointArtifactChanges | None = None


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Persists an authorized coordination change without deciding its legality."""

    receipt: MutationReceipt
    decision: CoordinationAuthorityDecision


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    receipt: MutationReceipt
    decision: AttemptAuthorityDecision


type StoredStateMutation = (
    TransitionMutation | ProposalCreationMutation | CoordinationAuthorityMutation | AttemptAuthorityMutation
)
