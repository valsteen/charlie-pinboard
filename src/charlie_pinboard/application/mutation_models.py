from dataclasses import dataclass

from charlie_pinboard.application.stored_state import (
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_models import AttemptAuthorityDecision, CoordinationAuthorityDecision
from charlie_pinboard.domain.decision_models import Decision, TransitionReceipt
from charlie_pinboard.domain.identifiers import ArtifactRefId, HistoryId, HistorySubjectId, HostId, TaskId
from charlie_pinboard.domain.proposal_models import ProposalCreationDecision
from charlie_pinboard.domain.work_models import CanonicalJson


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Stored-history identity for an ordinary transition receipt."""

    transition: TransitionReceipt
    history_id: HistoryId
    project_revision: int
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: CanonicalJson


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

    decision: Decision
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
