from collections.abc import Callable
from dataclasses import dataclass

from repo_work.model import WorkState


class TransitionInputError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class EmptyInput:
    pass


@dataclass(frozen=True, slots=True)
class ActivateInput:
    attempt: str
    branch: str
    base_revision: str
    owner: str


@dataclass(frozen=True, slots=True)
class ReasonInput:
    reason: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    evidence: str


@dataclass(frozen=True, slots=True)
class DeferInput:
    timing: str
    reopen_condition: str


@dataclass(frozen=True, slots=True)
class AcceptProposalInput:
    item: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    next_action: str


@dataclass(frozen=True, slots=True)
class MergeProposalInput:
    target: str


@dataclass(frozen=True, slots=True)
class TransferCoordinatorInput:
    task_id: str
    host_id: str


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


def _required(value: dict[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise TransitionInputError("TRANSITION_INPUT_REQUIRED", f"'{field}' must be a non-empty string.")
    if "\n" in result:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", f"'{field}' cannot contain a newline.")
    return result


def _dependencies(value: dict[str, object]) -> tuple[str, ...]:
    dependencies = value.get("depends_on", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) and item for item in dependencies):
        raise TransitionInputError("TRANSITION_INPUT_INVALID", "depends_on must be a list of item identities.")
    return tuple(dependencies)


def _activate(value: dict[str, object]) -> ActivateInput:
    return ActivateInput(
        attempt=_required(value, "attempt"),
        branch=_required(value, "branch"),
        base_revision=_required(value, "base_revision"),
        owner=_required(value, "owner"),
    )


def _reason(value: dict[str, object], *, dependencies: bool) -> ReasonInput:
    return ReasonInput(_required(value, "reason"), _dependencies(value) if dependencies else ())


def _accept_proposal(value: dict[str, object]) -> AcceptProposalInput:
    try:
        state = WorkState(_required(value, "state"))
    except ValueError as error:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", "Unsupported initial item state.") from error
    if state in {WorkState.ACTIVE, WorkState.PAUSED}:
        raise TransitionInputError("TRANSITION_INPUT_INVALID", "A proposal cannot enter an attempt-owned state.")
    timing = value.get("timing")
    if timing is not None and not isinstance(timing, str):
        raise TransitionInputError("TRANSITION_INPUT_INVALID", "timing must be null or a string.")
    return AcceptProposalInput(
        item=_required(value, "item"),
        state=state,
        timing=timing,
        depends_on=_dependencies(value),
        next_action=_required(value, "next_action"),
    )


type InputParser = Callable[[dict[str, object]], TransitionInput]


def _reason_only(value: dict[str, object]) -> ReasonInput:
    return _reason(value, dependencies=False)


def _reason_with_dependencies(value: dict[str, object]) -> ReasonInput:
    return _reason(value, dependencies=True)


def _evidence(value: dict[str, object]) -> EvidenceInput:
    return EvidenceInput(_required(value, "evidence"))


def _defer(value: dict[str, object]) -> DeferInput:
    return DeferInput(_required(value, "timing"), _required(value, "reopen_condition"))


def _merge(value: dict[str, object]) -> MergeProposalInput:
    return MergeProposalInput(_required(value, "target"))


def _transfer(value: dict[str, object]) -> TransferCoordinatorInput:
    return TransferCoordinatorInput(_required(value, "task_id"), _required(value, "host_id"))


def _empty(_value: dict[str, object]) -> EmptyInput:
    return EmptyInput()


PARSERS: dict[str, InputParser] = {
    "activate": _activate,
    "pause": _reason_only,
    "return-proposal": _reason_only,
    "reject-proposal": _reason_only,
    "mark-ready": _reason_only,
    "block": _reason_with_dependencies,
    "block-item": _reason_with_dependencies,
    "complete": _evidence,
    "reopen": _evidence,
    "defer": _defer,
    "accept-proposal": _accept_proposal,
    "merge-proposal": _merge,
    "transfer-coordinator": _transfer,
    "resume": _empty,
}


def parse_transition_input(kind: str, value: dict[str, object]) -> TransitionInput:
    parser = PARSERS.get(kind)
    if parser is None:
        raise TransitionInputError("ACTION_NOT_MUTATING", f"Action '{kind}' is not a canonical transition.")
    return parser(value)
