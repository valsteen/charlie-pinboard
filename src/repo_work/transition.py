import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from repo_work.actions import Action, actions_for, coordinator_generation, state_revision
from repo_work.atomic import atomic_write_text, transition_lock
from repo_work.markdown import (
    parse_current,
    parse_queue,
    render_current,
    render_queue,
    replace_header_fields,
)
from repo_work.model import QueueItem, WorkState
from repo_work.proposals import read_proposal
from repo_work.validate import validate_work_state


class TransitionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _required(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise TransitionError("TRANSITION_INPUT_REQUIRED", f"'{field}' must be a non-empty string.")
    if "\n" in value:
        raise TransitionError("TRANSITION_INPUT_INVALID", f"'{field}' cannot contain a newline.")
    return value


def _dependencies(payload: dict[str, object]) -> tuple[str, ...]:
    value = payload.get("depends_on", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise TransitionError("TRANSITION_INPUT_INVALID", "depends_on must be a list of item identities.")
    return tuple(value)


def _attempt_text(item: str, payload: dict[str, object]) -> str:
    attempt = _required(payload, "attempt")
    branch = _required(payload, "branch")
    base_revision = _required(payload, "base_revision")
    owner = _required(payload, "owner")
    updated = date.today().isoformat()
    return (
        "---\n"
        "kind: work-attempt\n"
        "schema: repo-work/v1\n"
        f"attempt: {attempt}\n"
        f"item: {item}\n"
        "state: active\n"
        f"branch: {branch}\n"
        f"base_revision: {base_revision}\n"
        f"owner: {owner}\n"
        f'updated: "{updated}"\n'
        "---\n\n"
        f"# Attempt: {item}\n"
    )


def _ensure_current_action(work_root: Path, project_root: Path, action: Action) -> None:
    current_actions = {
        candidate.action_id: candidate for candidate in actions_for(work_root, project_root, "coordinator")
    }
    current = current_actions.get(action.action_id)
    if current is None or current.kind != action.kind or current.subject != action.subject:
        raise TransitionError("ACTION_NOT_AVAILABLE", f"Action '{action.action_id}' is no longer legal.")


def _proposal_disposition(
    work_root: Path,
    proposal_path: Path,
    proposal: dict[str, object],
    disposition: str,
    target: str | None,
) -> None:
    history_path = work_root / "history" / "proposals" / proposal_path.name
    if history_path.exists():
        raise TransitionError("PROPOSAL_HISTORY_EXISTS", f"Proposal history already contains '{proposal_path.stem}'.")
    result = dict(proposal)
    result["disposition"] = disposition
    result["target"] = target
    atomic_write_text(history_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    proposal_path.unlink()


def apply_action(
    work_root: Path,
    project_root: Path,
    action: Action,
    payload: dict[str, object],
) -> None:
    with transition_lock(work_root):
        if action.coordinator_generation != coordinator_generation(work_root):
            raise TransitionError(
                "COORDINATOR_OWNERSHIP_CONFLICT",
                "The action belongs to a different coordinator generation.",
            )
        if action.expected_revision != state_revision(work_root):
            raise TransitionError("STATE_REVISION_STALE", "Repository work state changed after this action was issued.")
        if action.kind.endswith("-proposal"):
            proposal_path = work_root / "inbox" / f"{action.subject}.json"
            if not proposal_path.is_file():
                raise TransitionError("PROPOSAL_NOT_FOUND", f"Proposal '{action.subject}' no longer exists.")
            proposal_revision = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
            if action.subject_revision != proposal_revision:
                raise TransitionError("PROPOSAL_REVISION_STALE", "Proposal changed after this action was issued.")
        _ensure_current_action(work_root, project_root, action)
        queue = parse_queue(work_root / "queue.md")
        current = parse_current(work_root / "current.md")
        items = list(queue.items)

        if action.kind == "activate":
            attempt = _required(payload, "attempt")
            attempt_path = work_root / "attempts" / attempt / "attempt.md"
            if attempt_path.exists():
                raise TransitionError("ATTEMPT_ALREADY_EXISTS", f"Attempt '{attempt}' already exists.")
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state != WorkState.READY:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not ready.")
            items[index] = replace(
                items[index],
                state=WorkState.ACTIVE,
                attempt=attempt,
                next_action="continue",
            )
            atomic_write_text(attempt_path, _attempt_text(action.subject, payload))
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
            atomic_write_text(work_root / "current.md", render_current(action.subject, attempt, "continue"))
        elif action.kind in {"pause", "block"}:
            index = next((i for i, item in enumerate(items) if item.attempt == action.subject), None)
            if index is None or items[index].state != WorkState.ACTIVE:
                raise TransitionError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
            target = WorkState.PAUSED if action.kind == "pause" else WorkState.BLOCKED
            items[index] = replace(
                items[index],
                state=target,
                depends_on=(
                    tuple(dict.fromkeys(items[index].depends_on + _dependencies(payload)))
                    if action.kind == "block"
                    else items[index].depends_on
                ),
                next_action="resume" if target == WorkState.PAUSED else None,
                notes=_required(payload, "reason"),
            )
            attempt_path = work_root / "attempts" / action.subject / "attempt.md"
            attempt_text = attempt_path.read_text(encoding="utf-8")
            attempt_text = replace_header_fields(
                attempt_text,
                {"state": target.value, "updated": f'"{date.today().isoformat()}"'},
            )
            atomic_write_text(attempt_path, attempt_text)
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
            if current.focus_attempt == action.subject:
                atomic_write_text(work_root / "current.md", render_current(None, None, "select"))
        elif action.kind == "complete":
            index = next((i for i, item in enumerate(items) if item.attempt == action.subject), None)
            if index is None or items[index].state != WorkState.ACTIVE:
                raise TransitionError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
            _required(payload, "evidence")
            item_id = items[index].item
            item_path = work_root / "items" / f"{item_id}.md"
            history_path = work_root / "history" / "items" / f"{item_id}.md"
            if history_path.exists():
                raise TransitionError("HISTORY_RECORD_EXISTS", f"History already contains '{item_id}'.")
            history_text = replace_header_fields(
                item_path.read_text(encoding="utf-8"),
                {"kind": "work-history", "updated": f'"{date.today().isoformat()}"'},
                {"state": "done", "evidence": json.dumps(payload["evidence"])},
            )
            attempt_path = work_root / "attempts" / action.subject / "attempt.md"
            attempt_text = replace_header_fields(
                attempt_path.read_text(encoding="utf-8"),
                {"state": "review", "updated": f'"{date.today().isoformat()}"'},
            )
            remaining = tuple(item for item in items if item.item != item_id)
            atomic_write_text(history_path, history_text)
            atomic_write_text(attempt_path, attempt_text)
            atomic_write_text(work_root / "queue.md", render_queue(queue, remaining))
            if current.focus_attempt == action.subject:
                atomic_write_text(work_root / "current.md", render_current(None, None, "select"))
            item_path.unlink()
        elif action.kind == "resume":
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state not in {WorkState.PAUSED, WorkState.BLOCKED}:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not paused or blocked.")
            live_ids = {item.item for item in items}
            if any(dependency in live_ids for dependency in items[index].depends_on):
                raise TransitionError(
                    "DEPENDENCY_NOT_SATISFIED", f"Item '{action.subject}' still has a live dependency."
                )
            resume_attempt = items[index].attempt
            if resume_attempt is None:
                items[index] = replace(items[index], state=WorkState.READY, next_action="activate")
                atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
            else:
                attempt_path = work_root / "attempts" / resume_attempt / "attempt.md"
                attempt_text = replace_header_fields(
                    attempt_path.read_text(encoding="utf-8"),
                    {"state": "active", "updated": f'"{date.today().isoformat()}"'},
                )
                items[index] = replace(items[index], state=WorkState.ACTIVE, next_action="continue")
                atomic_write_text(attempt_path, attempt_text)
                atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
                atomic_write_text(work_root / "current.md", render_current(action.subject, resume_attempt, "continue"))
        elif action.kind == "reopen":
            evidence = _required(payload, "evidence")
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state != WorkState.DEFERRED:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not deferred.")
            items[index] = replace(
                items[index],
                state=WorkState.INTAKE,
                timing=None,
                next_action="review-intake",
                notes=f"Reopened with evidence: {evidence}",
            )
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
        elif action.kind == "mark-ready":
            reason = _required(payload, "reason")
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state != WorkState.INTAKE:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not in intake.")
            items[index] = replace(
                items[index],
                state=WorkState.READY,
                next_action="activate",
                notes=f"Ready: {reason}",
            )
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
        elif action.kind == "block-item":
            reason = _required(payload, "reason")
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state not in {WorkState.INTAKE, WorkState.READY}:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' cannot be blocked now.")
            items[index] = replace(
                items[index],
                state=WorkState.BLOCKED,
                depends_on=_dependencies(payload),
                next_action=None,
                notes=reason,
            )
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
        elif action.kind == "defer":
            timing = _required(payload, "timing")
            reopen_condition = _required(payload, "reopen_condition")
            if timing not in {"must-now", "cheaper-now", "safe-to-defer"}:
                raise TransitionError("TRANSITION_INPUT_INVALID", f"Unsupported timing '{timing}'.")
            index = next((i for i, item in enumerate(items) if item.item == action.subject), None)
            if index is None or items[index].state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED}:
                raise TransitionError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' cannot be deferred now.")
            if items[index].attempt is not None:
                raise TransitionError("ACTION_NOT_AVAILABLE", "An attempt must be paused rather than deferred.")
            items[index] = replace(
                items[index],
                state=WorkState.DEFERRED,
                timing=timing,
                next_action=None,
                notes=reopen_condition,
            )
            atomic_write_text(work_root / "queue.md", render_queue(queue, tuple(items)))
        elif action.kind == "accept-proposal":
            proposal_path = work_root / "inbox" / f"{action.subject}.json"
            proposal = read_proposal(proposal_path)
            item_id = _required(payload, "item")
            if any(item.item == item_id for item in items) or (work_root / "items" / f"{item_id}.md").exists():
                raise TransitionError("ITEM_ALREADY_EXISTS", f"Item '{item_id}' already exists.")
            try:
                state = WorkState(_required(payload, "state"))
            except ValueError as error:
                raise TransitionError("TRANSITION_INPUT_INVALID", "Unsupported initial item state.") from error
            if state == WorkState.ACTIVE or state == WorkState.PAUSED:
                raise TransitionError("TRANSITION_INPUT_INVALID", "A proposal cannot enter an attempt-owned state.")
            timing_value = payload.get("timing")
            if timing_value is not None and not isinstance(timing_value, str):
                raise TransitionError("TRANSITION_INPUT_INVALID", "timing must be null or a string.")
            dependencies_value = payload.get("depends_on", [])
            if not isinstance(dependencies_value, list) or not all(
                isinstance(dependency, str) and dependency for dependency in dependencies_value
            ):
                raise TransitionError("TRANSITION_INPUT_INVALID", "depends_on must be a list of item identities.")
            next_action = _required(payload, "next_action")
            notes = str(proposal["why_it_matters"])
            item = QueueItem(
                item=item_id,
                state=state,
                timing=timing_value,
                depends_on=tuple(dependencies_value),
                attempt=None,
                source=f"proposal:{action.subject}",
                next_action=next_action,
                notes=notes,
            )
            item_text = (
                "---\n"
                "kind: work-item\n"
                "schema: repo-work/v1\n"
                f"item: {item_id}\n"
                f"user_label: {json.dumps(proposal['user_label'])}\n"
                f'updated: "{date.today().isoformat()}"\n'
                "---\n\n"
                f"# {proposal['user_label']}\n\n"
                "## Context arc\n\n"
                f"Before and trigger: {proposal['trigger']}\n\n"
                f"Why it matters: {proposal['why_it_matters']}\n\n"
                f"After and trajectory: {proposal['effect']} {proposal['unlock']}\n"
            )
            atomic_write_text(work_root / "items" / f"{item_id}.md", item_text)
            atomic_write_text(work_root / "queue.md", render_queue(queue, (*items, item)))
            _proposal_disposition(work_root, proposal_path, proposal, "accepted", item_id)
        elif action.kind == "merge-proposal":
            proposal_path = work_root / "inbox" / f"{action.subject}.json"
            proposal = read_proposal(proposal_path)
            merge_target = _required(payload, "target")
            item_path = work_root / "items" / f"{merge_target}.md"
            if not any(item.item == merge_target for item in items) or not item_path.is_file():
                raise TransitionError("ITEM_NOT_FOUND", f"Merge target '{merge_target}' does not exist.")
            text = item_path.read_text(encoding="utf-8")
            text += f"\n## Intake evidence: {action.subject}\n\n{proposal['trigger']}\n"
            atomic_write_text(item_path, text)
            _proposal_disposition(work_root, proposal_path, proposal, "merged", merge_target)
        elif action.kind in {"return-proposal", "reject-proposal"}:
            proposal_path = work_root / "inbox" / f"{action.subject}.json"
            proposal = read_proposal(proposal_path)
            reason = _required(payload, "reason")
            proposal["coordinator_reason"] = reason
            disposition = "returned" if action.kind == "return-proposal" else "rejected"
            _proposal_disposition(work_root, proposal_path, proposal, disposition, None)
        else:
            raise TransitionError("ACTION_NOT_MUTATING", f"Action '{action.kind}' is not a canonical transition.")

        report = validate_work_state(work_root, project_root)
        if not report.valid:
            raise TransitionError("TRANSITION_POSTCONDITION_FAILED", report.render())
