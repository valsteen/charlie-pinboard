from enum import Enum
from typing import Annotated, Final, Literal

import msgspec

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type Timing = Literal["must-now", "cheaper-now", "safe-to-defer"]


class TransitionInputError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class AcceptedProposalState(Enum):
    INTAKE = "intake"
    READY = "ready"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class CloseOutcome(Enum):
    DONE = "done"
    DROPPED = "dropped"


class EmptyInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ActivateInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner: NonEmptyLine


class ReasonInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine
    depends_on: tuple[Identity, ...] = ()


class EvidenceInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyLine


class CloseInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    outcome: CloseOutcome
    reason: NonEmptyLine


class DeferInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    timing: Timing
    reopen_condition: NonEmptyLine


class AcceptProposalInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    item: Identity
    state: AcceptedProposalState
    next_action: NonEmptyLine
    timing: Timing | None = None
    depends_on: tuple[Identity, ...] = ()


class MergeProposalInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Identity


class TransferCoordinatorInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: NonEmptyLine
    host_id: NonEmptyLine


type TransitionInput = (
    EmptyInput
    | ActivateInput
    | ReasonInput
    | BlockInput
    | EvidenceInput
    | CloseInput
    | DeferInput
    | AcceptProposalInput
    | MergeProposalInput
    | TransferCoordinatorInput
)


type InputModel = type[TransitionInput]


INPUT_MODELS: dict[str, InputModel] = {
    "activate": ActivateInput,
    "pause": ReasonInput,
    "return-proposal": ReasonInput,
    "reject-proposal": ReasonInput,
    "mark-ready": ReasonInput,
    "block": BlockInput,
    "block-item": BlockInput,
    "complete": EvidenceInput,
    "close": CloseInput,
    "reopen": EvidenceInput,
    "defer": DeferInput,
    "accept-proposal": AcceptProposalInput,
    "merge-proposal": MergeProposalInput,
    "transfer-coordinator": TransferCoordinatorInput,
    "resume": EmptyInput,
    "submit-review": EmptyInput,
}

TRANSITION_ACTION_KINDS: Final = tuple(INPUT_MODELS)


def parse_transition_input(kind: str, data: bytes | str) -> TransitionInput:
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' is not a canonical transition.")
    try:
        return msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"Cannot decode transition JSON: {error}") from error


def encoded_transition_input_schema(kind: str) -> bytes:
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' has no canonical transition input.")
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
