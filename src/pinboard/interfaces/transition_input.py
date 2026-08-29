from typing import Final, assert_never

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    TaskId,
)
from pinboard.interfaces.errors import TransitionInputError
from pinboard.interfaces.transition_models import (
    AcceptCheckpointInputPayload,
    AcceptProposalInputPayload,
    AcceptReviewAndContinueInputPayload,
    BlockInputPayload,
    CloseInputPayload,
    DeferInputPayload,
    EmptyInputPayload,
    EvidenceInputPayload,
    InputModel,
    MergeProposalInputPayload,
    ReasonInputPayload,
    ResumeInputPayload,
    StoredActivateInputPayload,
    SubmitReviewInputPayload,
    TransferCoordinatorInputPayload,
)


def _input_model_or_none(kind: decision_models.ActionKind) -> InputModel | None:  # noqa: C901, PLR0912
    match kind:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return AcceptCheckpointInputPayload
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return AcceptReviewAndContinueInputPayload
        case decision_models.ActionKind.ACCEPT_PROPOSAL:
            return AcceptProposalInputPayload
        case decision_models.ActionKind.ACTIVATE:
            return StoredActivateInputPayload
        case decision_models.ActionKind.BLOCK | decision_models.ActionKind.BLOCK_ITEM:
            return BlockInputPayload
        case decision_models.ActionKind.CLOSE:
            return CloseInputPayload
        case decision_models.ActionKind.COMPLETE | decision_models.ActionKind.REOPEN:
            return EvidenceInputPayload
        case decision_models.ActionKind.DEFER:
            return DeferInputPayload
        case (
            decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
        ):
            return ReasonInputPayload
        case decision_models.ActionKind.MERGE_PROPOSAL:
            return MergeProposalInputPayload
        case decision_models.ActionKind.RESUME:
            return ResumeInputPayload
        case decision_models.ActionKind.SUBMIT_REVIEW:
            return SubmitReviewInputPayload
        case decision_models.ActionKind.TRANSFER_COORDINATOR:
            return TransferCoordinatorInputPayload
        case (
            decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


TRANSITION_ACTION_KINDS: Final = tuple(
    kind.value for kind in decision_models.ActionKind if _input_model_or_none(kind) is not None
)
INPUT_CONTRACT_ACTION_KINDS: Final = (*TRANSITION_ACTION_KINDS, decision_models.ActionKind.REPORT_BLOCKER.value)


def _input_model(kind: decision_models.ActionKind) -> InputModel:
    model = _input_model_or_none(kind)
    if model is None:
        raise TransitionInputError(
            DecisionFailureCode.ACTION_NOT_MUTATING,
            f"Action '{kind.value}' is not a canonical transition.",
        )
    return model


def parse_transition_input(  # noqa: C901, PLR0912
    kind: decision_models.ActionKind,
    data: bytes | str,
) -> work_models.TransitionInput:
    model = _input_model(kind)
    try:
        payload = msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        raise TransitionInputError(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode transition JSON: {error}",
        ) from error
    match payload:
        case EmptyInputPayload():
            return work_models.EmptyInput()
        case ResumeInputPayload(brief_artifact_ref_id=brief_artifact_ref_id):
            return work_models.ResumeInput(
                None if brief_artifact_ref_id is None else ArtifactRefId(brief_artifact_ref_id)
            )
        case StoredActivateInputPayload(
            attempt=attempt,
            branch=branch,
            base_revision=base_revision,
            owner=owner,
            brief_artifact_ref_id=brief_artifact_ref_id,
        ):
            return work_models.ActivateInput(
                AttemptId(attempt),
                branch,
                base_revision,
                owner,
                ArtifactRefId(brief_artifact_ref_id),
            )
        case SubmitReviewInputPayload(candidate=candidate):
            return work_models.SubmitReviewInput(CandidateId(candidate))
        case ReasonInputPayload(reason=reason):
            return work_models.ReasonInput(reason)
        case BlockInputPayload(reason=reason, depends_on=depends_on):
            return work_models.BlockInput(reason, tuple(ItemId(value) for value in depends_on))
        case EvidenceInputPayload(evidence=evidence):
            return work_models.EvidenceInput(evidence)
        case AcceptCheckpointInputPayload(checkpoint=checkpoint, candidate=candidate, evidence=evidence):
            return work_models.AcceptCheckpointInput(CheckpointId(checkpoint), CandidateId(candidate), evidence)
        case AcceptReviewAndContinueInputPayload(candidate=candidate, evidence=evidence):
            return work_models.AcceptReviewAndContinueInput(CandidateId(candidate), evidence)
        case CloseInputPayload(outcome=outcome, reason=reason):
            return work_models.CloseInput(outcome, reason)
        case DeferInputPayload(timing=timing, reopen_condition=reopen_condition):
            return work_models.DeferInput(work_models.Timing(timing), reopen_condition)
        case AcceptProposalInputPayload(
            item=item,
            state=state,
            next_action=next_action,
            timing=timing,
            depends_on=depends_on,
        ):
            return work_models.AcceptProposalInput(
                ItemId(item),
                state,
                next_action,
                work_models.Timing(timing) if timing is not None else None,
                tuple(ItemId(value) for value in depends_on),
            )
        case MergeProposalInputPayload(target=target):
            return work_models.MergeProposalInput(ItemId(target))
        case TransferCoordinatorInputPayload(task_id=task_id, host_id=host_id):
            return work_models.TransferCoordinatorInput(TaskId(task_id), HostId(host_id))
        case _ as unreachable:
            assert_never(unreachable)


def encoded_transition_input_schema(kind: decision_models.ActionKind) -> bytes:
    model = _input_model(kind)
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
