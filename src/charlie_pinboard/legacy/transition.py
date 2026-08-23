import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import assert_never

from charlie_pinboard.domain.decisions import LegacyTransitionCommand
from charlie_pinboard.legacy.actions import (
    Action,
    AuthorizationKind,
    Role,
    actions_for,
    coordinator_generation,
    state_revision,
)
from charlie_pinboard.legacy.atomic import PlatformNotSupportedError
from charlie_pinboard.legacy.authority import AuthorityVersion, authority_transaction
from charlie_pinboard.legacy.leases import LeaseError, require_attempt, require_coordination
from charlie_pinboard.legacy.markdown import parse_item, parse_queue
from charlie_pinboard.legacy.resources import ResourceError, require_resource
from charlie_pinboard.legacy.revisions import subject_revision
from charlie_pinboard.legacy.transaction_store import (
    AtomicCommitError,
    CommitFailpoint,
    commit_change_set,
    recover_pending_commit,
    validate_change_set,
)
from charlie_pinboard.legacy.transition_plan import TransitionPlanError, plan_transition


class TransitionError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _message(error: Exception) -> str:
    return str(error).partition(": ")[2]


def _item_for_attempt(work_root: Path, attempt_id: str) -> str:
    item = next(
        (candidate.item for candidate in parse_queue(work_root / "queue.md").items if candidate.attempt == attempt_id),
        None,
    )
    if item is None:
        raise TransitionError("ATTEMPT_NOT_FOUND", f"Attempt '{attempt_id}' does not name a live item.")
    return item


def _resource_names(work_root: Path, item_id: str) -> tuple[str, ...]:
    return parse_item(work_root / "items" / f"{item_id}.md").resources


def _verify_worker_resources(work_root: Path, item_id: str, action: Action) -> None:
    attempt = require_attempt(work_root, action.subject, action.lease_id or "", action.coordinator_generation)
    required = {(resource_id, attempt.host_id) for resource_id in _resource_names(work_root, item_id)}
    supplied = {(token.resource_id, token.host_id) for token in action.resource_claims}
    if len(supplied) != len(action.resource_claims) or supplied != required:
        raise TransitionError(
            "RESOURCE_CLAIM_REQUIRED",
            "The action must carry exactly the current resource claims required by this item and host.",
        )
    for token in action.resource_claims:
        claim = require_resource(
            work_root,
            token.resource_id,
            token.host_id,
            token.lease_id,
            token.generation,
        )
        if claim.attempt_id != action.subject or claim.task_id != attempt.task_id:
            raise TransitionError("RESOURCE_BUSY", f"Resource '{token.resource_id}' is not held by this attempt.")


def _verify_v1_tokens(base_work_root: Path, action: Action) -> None:
    if action.coordinator_generation != coordinator_generation(base_work_root):
        raise TransitionError(
            "COORDINATOR_OWNERSHIP_CONFLICT",
            "The action belongs to a different coordinator generation.",
        )
    if action.expected_revision != state_revision(base_work_root):
        raise TransitionError("STATE_REVISION_STALE", "Repository work state changed after this action was issued.")


def _verify_v2_tokens(base_work_root: Path, work_root: Path, action: Action) -> None:
    match action.authorization:
        case AuthorizationKind.COORDINATION:
            require_coordination(work_root, action.lease_id or "", action.coordinator_generation)
            if action.expected_revision != state_revision(base_work_root):
                raise TransitionError(
                    "STATE_REVISION_STALE", "Repository work graph changed after this action was issued."
                )
        case AuthorizationKind.ATTEMPT:
            item_id = _item_for_attempt(work_root, action.subject)
            require_attempt(work_root, action.subject, action.lease_id or "", action.coordinator_generation)
            if action.subject_revision != subject_revision(work_root, item_id):
                raise TransitionError(
                    "SUBJECT_REVISION_STALE", f"Work item '{item_id}' changed after this action was issued."
                )
            _verify_worker_resources(work_root, item_id, action)
        case AuthorizationKind.COORDINATOR | AuthorizationKind.OBSERVER:
            raise TransitionError(
                "LEASE_REQUIRED", "A mutating v2 action must carry coordination or attempt authority."
            )
        case _ as unreachable:
            assert_never(unreachable)


def _verify_action_tokens(base_work_root: Path, work_root: Path, version: AuthorityVersion, action: Action) -> None:
    match version:
        case AuthorityVersion.V1:
            _verify_v1_tokens(base_work_root, action)
        case AuthorityVersion.V2:
            _verify_v2_tokens(base_work_root, work_root, action)
        case _ as unreachable:
            assert_never(unreachable)
    if not action.kind.value.endswith("-proposal"):
        return
    proposal_path = work_root / "inbox" / f"{action.subject}.json"
    if not proposal_path.is_file():
        raise TransitionError("PROPOSAL_NOT_FOUND", f"Proposal '{action.subject}' no longer exists.")
    proposal_revision = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
    if action.subject_revision != proposal_revision:
        raise TransitionError("PROPOSAL_REVISION_STALE", "Proposal changed after this action was issued.")


def _verify_action_available(base_work_root: Path, project_root: Path, action: Action) -> None:
    match action.authorization:
        case AuthorizationKind.ATTEMPT:
            role = Role.WORKER
        case AuthorizationKind.COORDINATOR | AuthorizationKind.COORDINATION:
            role = Role.COORDINATOR
        case AuthorizationKind.OBSERVER:
            raise TransitionError("ACTION_NOT_AVAILABLE", "Observer actions cannot mutate repository work.")
        case _ as unreachable:
            assert_never(unreachable)
    current_actions = {
        candidate.action_id: candidate
        for candidate in actions_for(
            base_work_root,
            project_root,
            role,
            lease_id=action.lease_id,
            generation=action.coordinator_generation,
        )
    }
    current = current_actions.get(action.action_id)
    if current is None or (
        current.kind,
        current.subject,
        current.expected_revision,
        current.coordinator_generation,
        current.subject_revision,
        current.authorization,
        current.lease_id,
        current.resource_claims,
    ) != (
        action.kind,
        action.subject,
        action.expected_revision,
        action.coordinator_generation,
        action.subject_revision,
        action.authorization,
        action.lease_id,
        action.resource_claims,
    ):
        raise TransitionError("ACTION_NOT_AVAILABLE", f"Action '{action.action_id}' is no longer legal.")


def apply_transition(
    work_root: Path,
    project_root: Path,
    action: Action,
    payload: bytes | str,
    decode_command: Callable[[Action, bytes | str], LegacyTransitionCommand],
    *,
    failpoint: CommitFailpoint | None = None,
) -> str:
    try:
        with authority_transaction(work_root) as authority:
            active_root = authority.work_root
            recover_pending_commit(active_root)
            _verify_action_tokens(work_root, active_root, authority.version, action)
            _verify_action_available(work_root, project_root, action)
            command = decode_command(action, payload)
            changes = plan_transition(active_root, project_root, command)
            validate_change_set(active_root, project_root, changes, authority.version)
            commit_change_set(active_root, project_root, changes, authority.version, failpoint=failpoint)
            return state_revision(work_root)
    except PlatformNotSupportedError as error:
        raise TransitionError("PLATFORM_NOT_SUPPORTED", _message(error)) from error
    except (
        AtomicCommitError,
        LeaseError,
        ResourceError,
        TransitionPlanError,
    ) as error:
        raise TransitionError(error.code, _message(error)) from error
