from typing import Annotated, Final, Literal, assert_never

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
from charlie_pinboard.domain.model import (
    AcceptCheckpointInput,
    AcceptedProposalState,
    AcceptProposalInput,
    ActivateInput,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    LegacyActivateInput,
    MergeProposalInput,
    ReasonInput,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
    TransitionInput,
)

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type TimingPayload = Literal["must-now", "cheaper-now", "safe-to-defer"]


class TransitionInputError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class EmptyInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner: NonEmptyLine


class StoredActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner: NonEmptyLine
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]


class SubmitReviewInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine


class ReasonInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine
    depends_on: tuple[Identity, ...] = ()


class EvidenceInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyLine


class AcceptCheckpointInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    checkpoint: Identity
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class CloseInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    outcome: CloseOutcome
    reason: NonEmptyLine


class DeferInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    timing: TimingPayload
    reopen_condition: NonEmptyLine


class AcceptProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    item: Identity
    state: AcceptedProposalState
    next_action: NonEmptyLine
    timing: TimingPayload | None = None
    depends_on: tuple[Identity, ...] = ()


class MergeProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Identity


class TransferCoordinatorInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: NonEmptyLine
    host_id: NonEmptyLine


type InputPayload = (
    EmptyInputPayload
    | ActivateInputPayload
    | StoredActivateInputPayload
    | SubmitReviewInputPayload
    | ReasonInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | AcceptCheckpointInputPayload
    | CloseInputPayload
    | DeferInputPayload
    | AcceptProposalInputPayload
    | MergeProposalInputPayload
    | TransferCoordinatorInputPayload
)
type InputModel = type[InputPayload]


TRANSITION_ACTION_KINDS: Final = (
    "accept-checkpoint",
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


def _input_model(kind: str, *, legacy: bool) -> InputModel:  # noqa: C901, PLR0912
    match kind:
        case "accept-checkpoint":
            return AcceptCheckpointInputPayload
        case "accept-proposal":
            return AcceptProposalInputPayload
        case "activate":
            return ActivateInputPayload if legacy else StoredActivateInputPayload
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
            return EmptyInputPayload
        case "submit-review":
            return EmptyInputPayload if legacy else SubmitReviewInputPayload
        case "transfer-coordinator":
            return TransferCoordinatorInputPayload
        case _:
            raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' is not a canonical transition.")


def _parse_transition_input(  # noqa: C901, PLR0912 - exhaustive boundary conversion
    kind: str,
    data: bytes | str,
    *,
    legacy: bool,
) -> TransitionInput:
    model = _input_model(kind, legacy=legacy)
    try:
        payload = msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"Cannot decode transition JSON: {error}") from error
    match payload:
        case EmptyInputPayload():
            return EmptyInput()
        case ActivateInputPayload(attempt=attempt, branch=branch, base_revision=base_revision, owner=owner):
            return LegacyActivateInput(AttemptId(attempt), branch, base_revision, owner)
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


def parse_transition_input(kind: str, data: bytes | str) -> TransitionInput:
    return _parse_transition_input(kind, data, legacy=False)


def parse_legacy_transition_input(kind: str, data: bytes | str) -> TransitionInput:
    """Decode the temporary Markdown route without presenting it as the SQLite command contract."""

    return _parse_transition_input(kind, data, legacy=True)


def encoded_transition_input_schema(kind: str) -> bytes:
    model = _input_model(kind, legacy=False)
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")


def encoded_legacy_transition_input_schema(kind: str) -> bytes:
    model = _input_model(kind, legacy=True)
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
