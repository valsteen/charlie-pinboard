from typing import Final, assert_never

import msgspec

from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    TaskId,
)
from charlie_pinboard.domain.work_models import (
    AcceptCheckpointInput,
    AcceptProposalInput,
    AcceptReviewAndContinueInput,
    ActivateInput,
    BlockInput,
    CloseInput,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    MergeProposalInput,
    ReasonInput,
    ResumeInput,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
    TransitionInput,
)
from charlie_pinboard.interfaces.errors import TransitionInputError, TransitionInputErrorCode
from charlie_pinboard.interfaces.transition_models import (
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

TRANSITION_ACTION_KINDS: Final = (
    "accept-checkpoint",
    "accept-review-and-continue",
    "accept-proposal",
    "activate",
    "block",
    "block-item",
    "close",
    "complete",
    "defer",
    "mark-ready",
    "merge-proposal",
    "pause",
    "reject-proposal",
    "reopen",
    "resume",
    "return-for-correction",
    "return-proposal",
    "submit-review",
    "transfer-coordinator",
)


def _input_model(kind: str) -> InputModel:  # noqa: C901, PLR0912
    match kind:
        case "accept-checkpoint":
            return AcceptCheckpointInputPayload
        case "accept-review-and-continue":
            return AcceptReviewAndContinueInputPayload
        case "accept-proposal":
            return AcceptProposalInputPayload
        case "activate":
            return StoredActivateInputPayload
        case "block" | "block-item":
            return BlockInputPayload
        case "close":
            return CloseInputPayload
        case "complete" | "reopen":
            return EvidenceInputPayload
        case "defer":
            return DeferInputPayload
        case "mark-ready" | "pause" | "reject-proposal" | "return-for-correction" | "return-proposal":
            return ReasonInputPayload
        case "merge-proposal":
            return MergeProposalInputPayload
        case "resume":
            return ResumeInputPayload
        case "submit-review":
            return SubmitReviewInputPayload
        case "transfer-coordinator":
            return TransferCoordinatorInputPayload
        case _:
            raise TransitionInputError(
                TransitionInputErrorCode.ACTION_NOT_MUTATING,
                f"Action '{kind}' is not a canonical transition.",
            )


def parse_transition_input(kind: str, data: bytes | str) -> TransitionInput:  # noqa: C901, PLR0912
    model = _input_model(kind)
    try:
        payload = msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        raise TransitionInputError(
            TransitionInputErrorCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode transition JSON: {error}",
        ) from error
    match payload:
        case EmptyInputPayload():
            return EmptyInput()
        case ResumeInputPayload(brief_artifact_ref_id=brief_artifact_ref_id):
            return ResumeInput(None if brief_artifact_ref_id is None else ArtifactRefId(brief_artifact_ref_id))
        case StoredActivateInputPayload(
            attempt=attempt,
            branch=branch,
            base_revision=base_revision,
            owner=owner,
            brief_artifact_ref_id=brief_artifact_ref_id,
        ):
            return ActivateInput(
                AttemptId(attempt),
                branch,
                base_revision,
                owner,
                ArtifactRefId(brief_artifact_ref_id),
            )
        case SubmitReviewInputPayload(candidate=candidate):
            return SubmitReviewInput(CandidateId(candidate))
        case ReasonInputPayload(reason=reason):
            return ReasonInput(reason)
        case BlockInputPayload(reason=reason, depends_on=depends_on):
            return BlockInput(reason, tuple(ItemId(value) for value in depends_on))
        case EvidenceInputPayload(evidence=evidence):
            return EvidenceInput(evidence)
        case AcceptCheckpointInputPayload(checkpoint=checkpoint, candidate=candidate, evidence=evidence):
            return AcceptCheckpointInput(CheckpointId(checkpoint), CandidateId(candidate), evidence)
        case AcceptReviewAndContinueInputPayload(candidate=candidate, evidence=evidence):
            return AcceptReviewAndContinueInput(CandidateId(candidate), evidence)
        case CloseInputPayload(outcome=outcome, reason=reason):
            return CloseInput(outcome, reason)
        case DeferInputPayload(timing=timing, reopen_condition=reopen_condition):
            return DeferInput(Timing(timing), reopen_condition)
        case AcceptProposalInputPayload(
            item=item,
            state=state,
            next_action=next_action,
            timing=timing,
            depends_on=depends_on,
        ):
            return AcceptProposalInput(
                ItemId(item),
                state,
                next_action,
                Timing(timing) if timing is not None else None,
                tuple(ItemId(value) for value in depends_on),
            )
        case MergeProposalInputPayload(target=target):
            return MergeProposalInput(ItemId(target))
        case TransferCoordinatorInputPayload(task_id=task_id, host_id=host_id):
            return TransferCoordinatorInput(TaskId(task_id), HostId(host_id))
        case _ as unreachable:
            assert_never(unreachable)


def encoded_transition_input_schema(kind: str) -> bytes:
    model = _input_model(kind)
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
