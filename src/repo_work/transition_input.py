import msgspec

from repo_work.model import WorkState
from repo_work.records import JsonRecord


class TransitionInputError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _required_text(field: str, value: str) -> None:
    if not value:
        raise TransitionInputError("TRANSITION_INPUT_REQUIRED", f"'{field}' must be a non-empty string.")
    if "\n" in value:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"'{field}' cannot contain a newline.")


class EmptyInput(JsonRecord):
    pass


class ActivateInput(JsonRecord):
    attempt: str
    branch: str
    base_revision: str
    owner: str

    def __post_init__(self) -> None:
        for field, value in (
            ("attempt", self.attempt),
            ("branch", self.branch),
            ("base_revision", self.base_revision),
            ("owner", self.owner),
        ):
            _required_text(field, value)


class ReasonInput(JsonRecord):
    reason: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text("reason", self.reason)
        for dependency in self.depends_on:
            _required_text("depends_on", dependency)


class EvidenceInput(JsonRecord):
    evidence: str

    def __post_init__(self) -> None:
        _required_text("evidence", self.evidence)


class DeferInput(JsonRecord):
    timing: str
    reopen_condition: str

    def __post_init__(self) -> None:
        _required_text("timing", self.timing)
        _required_text("reopen_condition", self.reopen_condition)


class AcceptProposalInput(JsonRecord, kw_only=True):
    item: str
    state: WorkState
    next_action: str
    timing: str | None = None
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text("item", self.item)
        _required_text("next_action", self.next_action)
        for dependency in self.depends_on:
            _required_text("depends_on", dependency)
        if self.state in {WorkState.ACTIVE, WorkState.PAUSED}:
            raise TransitionInputError(
                "TRANSITION_INPUT_INVALID",
                "A proposal cannot enter an attempt-owned state.",
            )


class MergeProposalInput(JsonRecord):
    target: str

    def __post_init__(self) -> None:
        _required_text("target", self.target)


class TransferCoordinatorInput(JsonRecord):
    task_id: str
    host_id: str

    def __post_init__(self) -> None:
        _required_text("task_id", self.task_id)
        _required_text("host_id", self.host_id)


type TransitionInput = (
    EmptyInput
    | ActivateInput
    | ReasonInput
    | EvidenceInput
    | DeferInput
    | AcceptProposalInput
    | MergeProposalInput
    | TransferCoordinatorInput
)


type InputModel = (
    type[EmptyInput]
    | type[ActivateInput]
    | type[ReasonInput]
    | type[EvidenceInput]
    | type[DeferInput]
    | type[AcceptProposalInput]
    | type[MergeProposalInput]
    | type[TransferCoordinatorInput]
)


INPUT_MODELS: dict[str, InputModel] = {
    "activate": ActivateInput,
    "pause": ReasonInput,
    "return-proposal": ReasonInput,
    "reject-proposal": ReasonInput,
    "mark-ready": ReasonInput,
    "block": ReasonInput,
    "block-item": ReasonInput,
    "complete": EvidenceInput,
    "reopen": EvidenceInput,
    "defer": DeferInput,
    "accept-proposal": AcceptProposalInput,
    "merge-proposal": MergeProposalInput,
    "transfer-coordinator": TransferCoordinatorInput,
    "resume": EmptyInput,
}


def parse_transition_input(kind: str, data: bytes | str) -> TransitionInput:
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' is not a canonical transition.")
    try:
        value = msgspec.json.decode(data, type=model, strict=True)
    except msgspec.ValidationError as error:
        message = str(error)
        if message.startswith("Object missing required field"):
            raise TransitionInputError("TRANSITION_INPUT_REQUIRED", message) from error
        if message.startswith("Expected `object`"):
            raise TransitionInputError("TRANSITION_INPUT_INVALID", "JSON root must be an object.") from error
        raise TransitionInputError("TRANSITION_INPUT_INVALID", message) from error
    except msgspec.DecodeError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"Cannot parse JSON: {error}") from error
    if kind in {"pause", "return-proposal", "reject-proposal", "mark-ready"} and isinstance(value, ReasonInput):
        return ReasonInput(value.reason)
    return value
