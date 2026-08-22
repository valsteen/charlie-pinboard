from pathlib import Path

from charlie_pinboard.domain.model import TransitionInput
from charlie_pinboard.interfaces.transition_input import TransitionInputError, parse_transition_input
from charlie_pinboard.legacy.actions import Action
from charlie_pinboard.legacy.transaction_store import CommitFailpoint
from charlie_pinboard.legacy.transition import TransitionError, apply_transition


def _decode_input(kind: str, payload: bytes | str) -> TransitionInput:
    try:
        return parse_transition_input(kind, payload)
    except TransitionInputError as error:
        raise TransitionError(error.code, str(error).partition(": ")[2]) from error


def apply_action(
    work_root: Path,
    project_root: Path,
    action: Action,
    payload: bytes | str,
    *,
    failpoint: CommitFailpoint | None = None,
) -> str:
    return apply_transition(work_root, project_root, action, payload, _decode_input, failpoint=failpoint)
