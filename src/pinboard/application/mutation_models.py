from dataclasses import dataclass

from pinboard.application.stored_state import (
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
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
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: work_models.CanonicalJson


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class TransitionMutation:
    """Persists one accepted closed lifecycle decision as an exact relational delta."""

    decision: decision_models.Decision
    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Persists an authorized coordination change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: CoordinationAuthorityDecision


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: AttemptAuthorityDecision


type StoredStateMutation = (
    TransitionMutation | ProposalCreationMutation | CoordinationAuthorityMutation | AttemptAuthorityMutation
)
