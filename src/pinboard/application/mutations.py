from typing import assert_never

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef, CheckpointArtifacts
from pinboard.application.errors import MutationContractError, MutationContractErrorCode
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointArtifactChanges,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.history import HistoryOutcome, encode_transition_receipt_outcome
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    TaskId,
)


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case TransitionMutation(decision=decision):
            checkpoint = None
            candidate = None
            match decision.change:
                case decision_models.CheckpointAcceptanceChange(checkpoint=value, candidate=accepted_candidate):
                    checkpoint = str(value)
                    candidate = str(accepted_candidate)
                case (
                    decision_models.ReviewAcceptanceChange(candidate=accepted_candidate)
                    | decision_models.ReviewSubmissionChange(protected_candidate_after=accepted_candidate)
                ):
                    candidate = str(accepted_candidate)
                case (
                    decision_models.AcceptedProposalChange()
                    | decision_models.ActivationChange()
                    | decision_models.AttemptStateChange()
                    | decision_models.BlockAttemptChange()
                    | decision_models.BlockItemChange()
                    | decision_models.AttemptClosureChange()
                    | decision_models.CompletionChange()
                    | decision_models.CoordinatorTransferChange()
                    | decision_models.ItemClosureChange()
                    | decision_models.ItemStateChange()
                    | decision_models.MergedProposalChange()
                    | decision_models.ReturnedProposalChange()
                    | decision_models.RejectedProposalChange()
                    | decision_models.ResumeAttemptChange()
                    | decision_models.ReviewReturnChange()
                ):
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(
                    evidence=decision.receipt.evidence,
                    outcome=decision.receipt.outcome,
                    candidate=candidate,
                    checkpoint=checkpoint,
                ),
            )
        case ProposalCreationMutation() | CoordinationAuthorityMutation() | AttemptAuthorityMutation():
            transition = mutation.receipt.transition
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(evidence=transition.evidence, outcome=transition.outcome),
            )
        case _ as unreachable:
            assert_never(unreachable)


def stored_transition_receipt(mutation: StoredStateMutation) -> stored_state.StoredTransitionReceipt:
    """Convert one focused accepted mutation into its exact persisted receipt."""

    outcome = _history_outcome(mutation)
    receipt = mutation.receipt
    return stored_state.StoredTransitionReceipt(
        receipt.history_id,
        receipt.project_revision,
        receipt.transition.action_id,
        receipt.action_kind,
        receipt.subject_id,
        receipt.artifact_ref_id,
        receipt.authorization,
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.input_schema,
        receipt.input_payload,
        outcome.outcome_schema,
        work_models.CanonicalJson(outcome.payload),
        receipt.transition.decided_at,
    )


def _history_action_kind(value: decision_models.ActionKind) -> stored_state.TransitionHistoryActionKind:
    match value:
        case (
            decision_models.ActionKind.ACCEPT_CHECKPOINT
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_PROPOSAL
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.COMPLETE
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.REPORT_BLOCKER
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
            | decision_models.ActionKind.SUBMIT_REVIEW
            | decision_models.ActionKind.TRANSFER_COORDINATOR
        ):
            return stored_state.TransitionHistoryActionKind(value.value)
        case _ as unreachable:
            assert_never(unreachable)


def _history_authorization_kind(
    value: decision_models.AuthorizationKind,
) -> stored_state.TransitionHistoryAuthorizationKind:
    match value:
        case (
            decision_models.AuthorizationKind.COORDINATOR
            | decision_models.AuthorizationKind.COORDINATION
            | decision_models.AuthorizationKind.ATTEMPT
        ):
            return stored_state.TransitionHistoryAuthorizationKind(value.value)
        case decision_models.AuthorizationKind.OBSERVER:
            raise MutationContractError(
                MutationContractErrorCode.RECEIPT_MISMATCH,
                "An observer action cannot produce mutation history.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _checkpoint_artifact_ids(
    before: stored_state.StoredWorkState,
    artifacts: CheckpointArtifacts,
) -> CheckpointArtifactChanges:
    assigned: list[tuple[stored_state.ArtifactKind, str, int, ArtifactRefId, str, str, int]] = [
        (
            value.kind,
            value.key,
            value.revision,
            value.artifact_ref_id,
            value.selector,
            value.content_sha256,
            value.size_bytes,
        )
        for value in before.artifact_references
    ]
    next_id = 1 + max((int(value[3]) for value in assigned), default=0)

    def identify(published: ArtifactRef, expected_kind: stored_state.ArtifactKind) -> ArtifactRefId:
        nonlocal next_id
        if published.kind != expected_kind:
            raise MutationContractError(
                MutationContractErrorCode.CHECKPOINT_ARTIFACTS_INVALID,
                f"Checkpoint evidence requires one {expected_kind.value} artifact.",
            )
        existing = next(
            (value for value in assigned if value[:3] == (published.kind, published.key, published.revision)),
            None,
        )
        if existing is not None:
            if existing[4:] != (published.selector, published.content_sha256, published.size_bytes):
                raise MutationContractError(
                    MutationContractErrorCode.CHECKPOINT_ARTIFACTS_INVALID,
                    "An accepted checkpoint artifact identity already names different bytes.",
                )
            return existing[3]
        result = ArtifactRefId(next_id)
        next_id += 1
        assigned.append(
            (
                published.kind,
                published.key,
                published.revision,
                result,
                published.selector,
                published.content_sha256,
                published.size_bytes,
            )
        )
        return result

    result_id = identify(artifacts.result, stored_state.ArtifactKind.RESULT)
    review_id = identify(artifacts.review, stored_state.ArtifactKind.EVIDENCE)
    if result_id == review_id:
        raise MutationContractError(
            MutationContractErrorCode.CHECKPOINT_ARTIFACTS_INVALID,
            "Checkpoint result and review require distinct artifact identities.",
        )
    return CheckpointArtifactChanges(artifacts.result, result_id, artifacts.review, review_id)


def _change_subjects(change: decision_models.DecisionChange) -> tuple[ItemId | None, AttemptId | None, bool]:
    match change:
        case decision_models.ItemStateChange(item=item):
            return item, None, False
        case decision_models.ActivationChange(item=item, attempt=attempt):
            return item, attempt, False
        case (
            decision_models.AttemptStateChange(item=item, attempt=attempt)
            | decision_models.BlockAttemptChange(item=item, attempt=attempt)
            | decision_models.ResumeAttemptChange(item=item, attempt=attempt)
            | decision_models.ReviewSubmissionChange(item=item, attempt=attempt)
            | decision_models.ReviewReturnChange(item=item, attempt=attempt)
            | decision_models.ReviewAcceptanceChange(item=item, attempt=attempt)
            | decision_models.CheckpointAcceptanceChange(item=item, attempt=attempt)
        ):
            return item, attempt, False
        case decision_models.CompletionChange(item=item, attempt=attempt):
            return item, attempt, True
        case decision_models.AttemptClosureChange(item=item, attempt=attempt):
            return item, attempt, True
        case decision_models.ItemClosureChange(item=item):
            return item, None, True
        case decision_models.BlockItemChange(item=item):
            return item, None, False
        case decision_models.AcceptedProposalChange(accepted_item=accepted):
            return accepted.item, None, False
        case (
            decision_models.MergedProposalChange()
            | decision_models.ReturnedProposalChange()
            | decision_models.RejectedProposalChange()
            | decision_models.CoordinatorTransferChange()
        ):
            return None, None, False
        case _ as unreachable:
            assert_never(unreachable)


def _focus_after(decision: decision_models.Decision, revision: int) -> stored_state.StoredFocus | None:
    item, attempt, terminal = _change_subjects(decision.change)
    if item is None:
        return None
    if terminal:
        return stored_state.StoredFocus(None, None, "select", revision)
    match decision.action:
        case decision_models.PauseAction() | decision_models.BlockAttemptAction() | decision_models.BlockItemAction():
            next_action = "resume"
        case decision_models.SubmitReviewAction():
            next_action = "review"
        case (
            decision_models.AcceptReviewAndContinueAction()
            | decision_models.ReturnForCorrectionAction()
            | decision_models.ResumeAction()
            | decision_models.ReopenAction()
            | decision_models.MarkReadyAction()
        ):
            next_action = "continue"
        case decision_models.DeferAction():
            next_action = "reopen"
        case (
            decision_models.AcceptCheckpointAction()
            | decision_models.AcceptProposalAction()
            | decision_models.ActivateAction()
            | decision_models.CompleteAction()
            | decision_models.CloseAction()
            | decision_models.MergeProposalAction()
            | decision_models.RejectProposalAction()
            | decision_models.ReturnProposalAction()
            | decision_models.TransferCoordinatorAction()
        ):
            next_action = decision.action.kind.value
        case _ as unreachable:
            assert_never(unreachable)
    return stored_state.StoredFocus(item, attempt, next_action, revision)


def project_transition_mutation(
    before: stored_state.StoredWorkState,
    decision: decision_models.Decision,
    checkpoint_artifacts: CheckpointArtifacts | None = None,
) -> TransitionMutation:
    """Project one pure lifecycle decision into its focused accepted mutation."""

    capability = decision.action.capability
    actor_task_id: TaskId | None = None
    actor_host_id: HostId | None = None
    if capability.authorization == decision_models.AuthorizationKind.ATTEMPT and capability.lease_id is not None:
        anchor = next(
            (
                value
                for value in before.authority.attempt_generations
                if value.lease_id == capability.lease_id and value.generation == capability.coordinator_generation
            ),
            None,
        )
        if anchor is not None:
            actor_task_id, actor_host_id = anchor.task_id, anchor.host_id
    elif capability.authorization == decision_models.AuthorizationKind.COORDINATION:
        coordination = before.authority.coordination
        if coordination is not None:
            actor_task_id, actor_host_id = coordination.task_id, coordination.host_id
    checkpoint_changes: CheckpointArtifactChanges | None = None
    match decision.change:
        case decision_models.CheckpointAcceptanceChange():
            if checkpoint_artifacts is None:
                raise MutationContractError(
                    MutationContractErrorCode.CHECKPOINT_ARTIFACTS_INVALID,
                    "Checkpoint acceptance requires exact result and review artifacts.",
                )
            checkpoint_changes = _checkpoint_artifact_ids(before, checkpoint_artifacts)
        case (
            decision_models.AcceptedProposalChange()
            | decision_models.ActivationChange()
            | decision_models.AttemptClosureChange()
            | decision_models.AttemptStateChange()
            | decision_models.BlockAttemptChange()
            | decision_models.BlockItemChange()
            | decision_models.CompletionChange()
            | decision_models.CoordinatorTransferChange()
            | decision_models.ItemClosureChange()
            | decision_models.ItemStateChange()
            | decision_models.MergedProposalChange()
            | decision_models.RejectedProposalChange()
            | decision_models.ResumeAttemptChange()
            | decision_models.ReturnedProposalChange()
            | decision_models.ReviewAcceptanceChange()
            | decision_models.ReviewReturnChange()
            | decision_models.ReviewSubmissionChange()
        ):
            if checkpoint_artifacts is not None:
                raise MutationContractError(
                    MutationContractErrorCode.CHECKPOINT_ARTIFACTS_INVALID,
                    "Only checkpoint acceptance can accept checkpoint artifacts.",
                )
        case _ as unreachable:
            assert_never(unreachable)
    revision = before.lifecycle.project.revision + 1
    receipt = MutationReceipt(
        decision.receipt,
        HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
        revision,
        _history_action_kind(decision.action.kind),
        HistorySubjectId(capability.subject),
        checkpoint_changes.review_id if checkpoint_changes is not None else None,
        _history_authorization_kind(capability.authorization),
        actor_task_id,
        actor_host_id,
        "decision/v1",
        work_models.CanonicalJson(b"{}"),
    )
    return TransitionMutation(decision, receipt, _focus_after(decision, revision), checkpoint_changes)
