from attrs import frozen
from cattrs.errors import BaseValidationError

from repo_work.json_codec import JsonCodecError, decode_json, nested_exception, validation_message
from repo_work.model import WorkState


class TransitionInputError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _required_text(field: str, value: str) -> None:
    if not value:
        raise TransitionInputError("TRANSITION_INPUT_REQUIRED", f"'{field}' must be a non-empty string.")
    if "\n" in value:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"'{field}' cannot contain a newline.")


@frozen
class EmptyInput:
    pass


@frozen
class ActivateInput:
    attempt: str
    branch: str
    base_revision: str
    owner: str

    def __attrs_post_init__(self) -> None:
        for field, value in (
            ("attempt", self.attempt),
            ("branch", self.branch),
            ("base_revision", self.base_revision),
            ("owner", self.owner),
        ):
            _required_text(field, value)


@frozen
class ReasonInput:
    reason: str
    depends_on: tuple[str, ...] = ()

    def __attrs_post_init__(self) -> None:
        _required_text("reason", self.reason)
        for dependency in self.depends_on:
            _required_text("depends_on", dependency)


@frozen
class EvidenceInput:
    evidence: str

    def __attrs_post_init__(self) -> None:
        _required_text("evidence", self.evidence)


@frozen
class DeferInput:
    timing: str
    reopen_condition: str

    def __attrs_post_init__(self) -> None:
        _required_text("timing", self.timing)
        _required_text("reopen_condition", self.reopen_condition)


@frozen(kw_only=True)
class AcceptProposalInput:
    item: str
    state: WorkState
    next_action: str
    timing: str | None = None
    depends_on: tuple[str, ...] = ()

    def __attrs_post_init__(self) -> None:
        _required_text("item", self.item)
        _required_text("next_action", self.next_action)
        for dependency in self.depends_on:
            _required_text("depends_on", dependency)
        if self.state in {WorkState.ACTIVE, WorkState.PAUSED}:
            raise TransitionInputError(
                "TRANSITION_INPUT_INVALID",
                "A proposal cannot enter an attempt-owned state.",
            )


@frozen
class MergeProposalInput:
    target: str

    def __attrs_post_init__(self) -> None:
        _required_text("target", self.target)


@frozen
class TransferCoordinatorInput:
    task_id: str
    host_id: str

    def __attrs_post_init__(self) -> None:
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
        value = decode_json(data, model)
    except JsonCodecError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", error.message) from error
    except BaseValidationError as error:
        domain_error = nested_exception(error, TransitionInputError)
        if domain_error is not None:
            raise domain_error from error
        if nested_exception(error, KeyError) is not None:
            raise TransitionInputError("TRANSITION_INPUT_REQUIRED", validation_message(error)) from error
        raise TransitionInputError("TRANSITION_INPUT_INVALID", validation_message(error)) from error
    if kind in {"pause", "return-proposal", "reject-proposal", "mark-ready"} and isinstance(value, ReasonInput):
        return ReasonInput(value.reason)
    return value
