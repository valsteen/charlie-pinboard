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
from pinboard.interfaces.errors import TransitionInputFailure, TransitionInputResult
from pinboard.interfaces.transition_models import (
    AcceptCheckpointInputPayload,
    AcceptProposalInputPayload,
    AcceptReviewAndContinueInputPayload,
    BlockInputPayload,
    CloseInputPayload,
    DeferInputPayload,
    EvidenceInputPayload,
    InputModel,
    InputPayload,
    MergeProposalInputPayload,
    ReasonInputPayload,
    ResumeInputPayload,
    ReviseItemInputPayload,
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
        case decision_models.ActionKind.REVISE_ITEM:
            return ReviseItemInputPayload
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
INPUT_CONTRACT_ACTION_KINDS: Final = tuple(kind.value for kind in decision_models.ActionKind)


def _input_model(kind: decision_models.ActionKind) -> TransitionInputResult[InputModel]:
    model = _input_model_or_none(kind)
    if model is None:
        return TransitionInputFailure(
            DecisionFailureCode.ACTION_NOT_MUTATING,
            f"Action '{kind.value}' is not a canonical transition.",
        )
    return model


def _decode[PayloadT: InputPayload](
    data: bytes | str,
    model: type[PayloadT],
) -> TransitionInputResult[PayloadT]:
    try:
        return msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        return TransitionInputFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode transition JSON: {error}",
        )


def _revise_item_input(payload: ReviseItemInputPayload) -> work_models.ReviseItemDefinitionInput:
    definition = payload.definition
    return work_models.ReviseItemDefinitionInput(
        ItemId(payload.item_id),
        payload.expected_revision,
        payload.expected_digest,
        TaskId(payload.source_task),
        payload.reason,
        work_models.WorkItemDefinition(
            definition.title,
            definition.objective,
            definition.hypothesis,
            definition.evidence,
            definition.scope,
            definition.non_scope,
            definition.acceptance_criteria,
            tuple(ItemId(value) for value in definition.dependencies),
            definition.effect,
            definition.unlock,
        ),
    )


def parse_item_revision_input(data: bytes | str) -> TransitionInputResult[work_models.ReviseItemDefinitionInput]:
    if isinstance(payload := _decode(data, ReviseItemInputPayload), TransitionInputFailure):
        return payload
    return _revise_item_input(payload)


def parse_transition_command(  # noqa: C901, PLR0912
    action: decision_models.Action,
    data: bytes | str,
) -> TransitionInputResult[decision_models.TransitionCommand]:
    match action:
        case decision_models.AcceptCheckpointAction():
            if isinstance(payload := _decode(data, AcceptCheckpointInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.AcceptCheckpointInput(
                    CheckpointId(payload.checkpoint), CandidateId(payload.candidate), payload.evidence
                )
            )
        case decision_models.AcceptReviewAndContinueAction():
            if isinstance(payload := _decode(data, AcceptReviewAndContinueInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.AcceptReviewAndContinueInput(CandidateId(payload.candidate), payload.evidence)
            )
        case decision_models.AcceptProposalAction():
            if isinstance(payload := _decode(data, AcceptProposalInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.AcceptProposalInput(
                    ItemId(payload.item),
                    payload.state,
                    payload.next_action,
                    work_models.Timing(payload.timing) if payload.timing is not None else None,
                    tuple(ItemId(value) for value in payload.depends_on),
                )
            )
        case decision_models.ActivateAction():
            if isinstance(payload := _decode(data, StoredActivateInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.ActivateInput(
                    AttemptId(payload.attempt),
                    payload.branch,
                    payload.base_revision,
                    payload.owner,
                    ArtifactRefId(payload.brief_artifact_ref_id),
                )
            )
        case decision_models.BlockAttemptAction() | decision_models.BlockItemAction():
            if isinstance(payload := _decode(data, BlockInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.BlockInput(payload.reason, tuple(ItemId(value) for value in payload.depends_on))
            )
        case decision_models.CloseAction():
            if isinstance(payload := _decode(data, CloseInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.CloseInput(payload.outcome, payload.reason))
        case decision_models.CompleteAction() | decision_models.ReopenAction():
            if isinstance(payload := _decode(data, EvidenceInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.EvidenceInput(payload.evidence))
        case decision_models.DeferAction():
            if isinstance(payload := _decode(data, DeferInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.DeferInput(work_models.Timing(payload.timing), payload.reopen_condition))
        case (
            decision_models.MarkReadyAction()
            | decision_models.PauseAction()
            | decision_models.RejectProposalAction()
            | decision_models.ReturnForCorrectionAction()
            | decision_models.ReturnProposalAction()
        ):
            if isinstance(payload := _decode(data, ReasonInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.ReasonInput(payload.reason))
        case decision_models.MergeProposalAction():
            if isinstance(payload := _decode(data, MergeProposalInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.MergeProposalInput(ItemId(payload.target)))
        case decision_models.ResumeAction():
            if isinstance(payload := _decode(data, ResumeInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.ResumeInput(
                    None if payload.brief_artifact_ref_id is None else ArtifactRefId(payload.brief_artifact_ref_id)
                )
            )
        case decision_models.ReviseItemAction():
            if isinstance(payload := _decode(data, ReviseItemInputPayload), TransitionInputFailure):
                return payload
            return action.command(_revise_item_input(payload))
        case decision_models.SubmitReviewAction():
            if isinstance(payload := _decode(data, SubmitReviewInputPayload), TransitionInputFailure):
                return payload
            return action.command(work_models.SubmitReviewInput(CandidateId(payload.candidate)))
        case decision_models.TransferCoordinatorAction():
            if isinstance(payload := _decode(data, TransferCoordinatorInputPayload), TransitionInputFailure):
                return payload
            return action.command(
                work_models.TransferCoordinatorInput(TaskId(payload.task_id), HostId(payload.host_id))
            )
        case (
            decision_models.ContinueAction()
            | decision_models.DispatchAction()
            | decision_models.InspectAction()
            | decision_models.ReportBlockerAction()
        ):
            return TransitionInputFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                f"Action '{action.kind.value}' is not a canonical transition.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def encoded_transition_input_schema(kind: decision_models.ActionKind) -> TransitionInputResult[bytes]:
    model = _input_model(kind)
    if isinstance(model, TransitionInputFailure):
        return model
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
