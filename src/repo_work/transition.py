import hashlib
from pathlib import Path

from repo_work.actions import Action, actions_for, coordinator_generation, state_revision
from repo_work.atomic import PlatformNotSupportedError, transition_lock
from repo_work.transaction_store import (
    AtomicCommitError,
    CommitFailpoint,
    commit_change_set,
    recover_pending_commit,
    validate_change_set,
)
from repo_work.transition_input import TransitionInputError, parse_transition_input
from repo_work.transition_plan import TransitionPlanError, plan_transition


class TransitionError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _message(error: Exception) -> str:
    return str(error).partition(": ")[2]


def _verify_action_tokens(work_root: Path, action: Action) -> None:
    if action.coordinator_generation != coordinator_generation(work_root):
        raise TransitionError(
            "COORDINATOR_OWNERSHIP_CONFLICT",
            "The action belongs to a different coordinator generation.",
        )
    if action.expected_revision != state_revision(work_root):
        raise TransitionError("STATE_REVISION_STALE", "Repository work state changed after this action was issued.")
    if not action.kind.endswith("-proposal"):
        return
    proposal_path = work_root / "inbox" / f"{action.subject}.json"
    if not proposal_path.is_file():
        raise TransitionError("PROPOSAL_NOT_FOUND", f"Proposal '{action.subject}' no longer exists.")
    proposal_revision = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
    if action.subject_revision != proposal_revision:
        raise TransitionError("PROPOSAL_REVISION_STALE", "Proposal changed after this action was issued.")


def _verify_action_available(work_root: Path, project_root: Path, action: Action) -> None:
    current_actions = {
        candidate.action_id: candidate for candidate in actions_for(work_root, project_root, "coordinator")
    }
    current = current_actions.get(action.action_id)
    if current is None or current.kind != action.kind or current.subject != action.subject:
        raise TransitionError("ACTION_NOT_AVAILABLE", f"Action '{action.action_id}' is no longer legal.")


def apply_action(
    work_root: Path,
    project_root: Path,
    action: Action,
    payload: bytes | str,
    *,
    failpoint: CommitFailpoint | None = None,
) -> None:
    try:
        with transition_lock(work_root):
            recover_pending_commit(work_root)
            _verify_action_tokens(work_root, action)
            _verify_action_available(work_root, project_root, action)
            value = parse_transition_input(action.kind, payload)
            changes = plan_transition(work_root, project_root, action, value)
            validate_change_set(work_root, project_root, changes)
            commit_change_set(work_root, project_root, changes, failpoint=failpoint)
    except (AtomicCommitError, TransitionInputError, TransitionPlanError, PlatformNotSupportedError) as error:
        code = getattr(error, "code", "PLATFORM_NOT_SUPPORTED")
        raise TransitionError(code, _message(error)) from error
