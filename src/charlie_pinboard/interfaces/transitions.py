from pathlib import Path

from charlie_pinboard.domain.decisions import LegacyTransitionCommand, bind_legacy_transition
from charlie_pinboard.interfaces.transition_input import TransitionInputError, parse_legacy_transition_input
from charlie_pinboard.legacy.actions import Action
from charlie_pinboard.legacy.transaction_store import CommitFailpoint
from charlie_pinboard.legacy.transition import TransitionError, apply_transition


def _decode_command(action: Action, payload: bytes | str) -> LegacyTransitionCommand:
    try:
        return bind_legacy_transition(action, parse_legacy_transition_input(action.kind.value, payload))
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
    return apply_transition(work_root, project_root, action, payload, _decode_command, failpoint=failpoint)
