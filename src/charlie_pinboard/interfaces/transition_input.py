from typing import Annotated, Final, Literal, assert_never

import msgspec

from charlie_pinboard.domain.identifiers import AttemptId, HostId, ItemId, TaskId
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AcceptProposalInput,
    ActivateInput,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    MergeProposalInput,
    ReasonInput,
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


class ReasonInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine
    depends_on: tuple[Identity, ...] = ()


class EvidenceInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    | ReasonInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | CloseInputPayload
    | DeferInputPayload
    | AcceptProposalInputPayload
    | MergeProposalInputPayload
    | TransferCoordinatorInputPayload
)
type InputModel = type[InputPayload]


INPUT_MODELS: dict[str, InputModel] = {
    "activate": ActivateInputPayload,
    "pause": ReasonInputPayload,
    "return-proposal": ReasonInputPayload,
    "reject-proposal": ReasonInputPayload,
    "mark-ready": ReasonInputPayload,
    "block": BlockInputPayload,
    "block-item": BlockInputPayload,
    "complete": EvidenceInputPayload,
    "close": CloseInputPayload,
    "reopen": EvidenceInputPayload,
    "defer": DeferInputPayload,
    "accept-proposal": AcceptProposalInputPayload,
    "merge-proposal": MergeProposalInputPayload,
    "transfer-coordinator": TransferCoordinatorInputPayload,
    "resume": EmptyInputPayload,
    "submit-review": EmptyInputPayload,
    "return-for-correction": ReasonInputPayload,
}

TRANSITION_ACTION_KINDS: Final = tuple(INPUT_MODELS)


def parse_transition_input(kind: str, data: bytes | str) -> TransitionInput:  # noqa: C901 - exhaustive boundary conversion
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' is not a canonical transition.")
    try:
        payload = msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"Cannot decode transition JSON: {error}") from error
    if isinstance(payload, EmptyInputPayload):
        return EmptyInput()
    if isinstance(payload, ActivateInputPayload):
        return ActivateInput(AttemptId(payload.attempt), payload.branch, payload.base_revision, payload.owner)
    if isinstance(payload, ReasonInputPayload):
        return ReasonInput(payload.reason)
    if isinstance(payload, BlockInputPayload):
        return BlockInput(payload.reason, tuple(ItemId(value) for value in payload.depends_on))
    if isinstance(payload, EvidenceInputPayload):
        return EvidenceInput(payload.evidence)
    if isinstance(payload, CloseInputPayload):
        return CloseInput(payload.outcome, payload.reason)
    if isinstance(payload, DeferInputPayload):
        return DeferInput(Timing(payload.timing), payload.reopen_condition)
    if isinstance(payload, AcceptProposalInputPayload):
        return AcceptProposalInput(
            ItemId(payload.item),
            payload.state,
            payload.next_action,
            Timing(payload.timing) if payload.timing is not None else None,
            tuple(ItemId(value) for value in payload.depends_on),
        )
    if isinstance(payload, MergeProposalInputPayload):
        return MergeProposalInput(ItemId(payload.target))
    if isinstance(payload, TransferCoordinatorInputPayload):
        return TransferCoordinatorInput(TaskId(payload.task_id), HostId(payload.host_id))
    assert_never(payload)


def encoded_transition_input_schema(kind: str) -> bytes:
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' has no canonical transition input.")
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
