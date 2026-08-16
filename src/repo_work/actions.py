from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from repo_work.markdown import parse_current, parse_queue
from repo_work.model import WorkState
from repo_work.validate import validate_work_state


class ActionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class Action:
    action_id: str
    kind: str
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None

    def as_dict(self) -> dict[str, str | int]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "subject": self.subject,
            "label": self.label,
            "expected_revision": self.expected_revision,
            "coordinator_generation": self.coordinator_generation,
            "subject_revision": self.subject_revision or "",
        }


def state_revision(work_root: Path) -> str:
    paths = [work_root / "queue.md", work_root / "current.md", work_root / "coordinator.json"]
    for directory in (work_root / "items", work_root / "attempts"):
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda candidate: str(candidate.relative_to(work_root))):
        relative = str(path.relative_to(work_root)).encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def coordinator_generation(work_root: Path) -> int:
    value = json.loads((work_root / "coordinator.json").read_text(encoding="utf-8"))
    generation = value.get("generation")
    if not isinstance(generation, int) or generation < 1:
        raise ActionError("COORDINATOR_GENERATION_INVALID", "Coordinator generation is not a positive integer.")
    return generation


def actions_for(work_root: Path, project_root: Path, role: str) -> tuple[Action, ...]:
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    if role not in {"coordinator", "worker", "observer"}:
        raise ActionError("ROLE_INVALID", f"Unsupported role '{role}'.")
    revision = state_revision(work_root)
    generation = coordinator_generation(work_root)
    queue = parse_queue(work_root / "queue.md")
    current = parse_current(work_root / "current.md")

    def action(kind: str, subject: str, label: str, subject_revision: str | None = None) -> Action:
        return Action(
            action_id=f"{kind}:{subject}",
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=revision,
            coordinator_generation=generation,
            subject_revision=subject_revision,
        )

    if role == "observer":
        return (action("inspect", "ledger", "Inspect current work"),)
    if role == "worker":
        if current.active_attempt is None:
            return ()
        return (
            action("continue", current.active_attempt, "Continue the active attempt"),
            action("report-blocker", current.active_attempt, "Report a blocker"),
            action("submit-review", current.active_attempt, "Submit the attempt for review"),
        )
    if current.active_attempt is not None:
        return (
            action("continue", current.active_attempt, "Continue the active attempt"),
            action("pause", current.active_attempt, "Pause and preserve the attempt"),
            action("block", current.active_attempt, "Block the attempt on a named condition"),
            action("complete", current.active_attempt, "Accept and complete the item"),
        )

    result: list[Action] = []
    for item in queue.items:
        if item.state == WorkState.INTAKE:
            result.extend(
                (
                    action("mark-ready", item.item, f"Mark {item.item} ready"),
                    action("block-item", item.item, f"Block {item.item} on a named condition"),
                    action("defer", item.item, f"Defer {item.item} with a reopen condition"),
                )
            )
        elif item.state == WorkState.READY:
            result.append(action("activate", item.item, f"Activate {item.item}"))
            result.append(action("defer", item.item, f"Defer {item.item} with a reopen condition"))
        elif item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not any(
            dependency in queue.by_id() for dependency in item.depends_on
        ):
            result.append(action("resume", item.item, f"Return {item.item} to ready"))
            if item.attempt is None:
                result.append(action("defer", item.item, f"Defer {item.item} with a reopen condition"))
        elif item.state == WorkState.DEFERRED:
            result.append(action("reopen", item.item, f"Reopen {item.item} for intake"))
    inbox = work_root / "inbox"
    if inbox.is_dir():
        for path in sorted(inbox.glob("*.json")):
            proposal_id = path.stem
            proposal_revision = hashlib.sha256(path.read_bytes()).hexdigest()
            result.extend(
                (
                    action("accept-proposal", proposal_id, f"Accept proposal {proposal_id}", proposal_revision),
                    action("merge-proposal", proposal_id, f"Merge proposal {proposal_id}", proposal_revision),
                    action("return-proposal", proposal_id, f"Return proposal {proposal_id}", proposal_revision),
                    action("reject-proposal", proposal_id, f"Reject proposal {proposal_id}", proposal_revision),
                )
            )
    result.append(action("transfer-coordinator", "ledger", "Transfer coordinator ownership"))
    return tuple(result)
