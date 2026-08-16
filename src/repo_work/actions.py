import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from repo_work.markdown import parse_queue
from repo_work.model import WorkState
from repo_work.validate import validate_work_state


class ActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
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
    paths: list[Path] = [work_root / "queue.md", work_root / "current.md", work_root / "coordinator.json"]
    for directory in (work_root / "items", work_root / "attempts"):
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    digest = hashlib.sha256()

    def relative_name(candidate: Path) -> str:
        return str(candidate.relative_to(work_root))

    for path in sorted(paths, key=relative_name):
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
    active_items = [item for item in queue.items if item.state == WorkState.ACTIVE]
    if role == "worker":
        worker_actions: list[Action] = []
        for item in active_items:
            if item.attempt is None:
                continue
            worker_actions.extend(
                (
                    action("continue", item.attempt, f"Continue {item.item}"),
                    action("report-blocker", item.attempt, f"Report a blocker for {item.item}"),
                    action("submit-review", item.attempt, f"Submit {item.item} for review"),
                )
            )
        return tuple(worker_actions)

    coordinator_actions: list[Action] = []
    for item in active_items:
        if item.attempt is None:
            continue
        coordinator_actions.extend(
            (
                action("continue", item.attempt, f"Continue {item.item}"),
                action("pause", item.attempt, f"Pause and preserve {item.item}"),
                action("block", item.attempt, f"Block {item.item} on a named condition"),
                action("complete", item.attempt, f"Accept and complete {item.item}"),
            )
        )
    for item in queue.items:
        if item.state == WorkState.INTAKE:
            coordinator_actions.extend(
                (
                    action("mark-ready", item.item, f"Mark {item.item} ready"),
                    action("block-item", item.item, f"Block {item.item} on a named condition"),
                    action("defer", item.item, f"Defer {item.item} with a reopen condition"),
                )
            )
        elif item.state == WorkState.READY:
            coordinator_actions.append(action("activate", item.item, f"Activate {item.item}"))
            coordinator_actions.append(action("defer", item.item, f"Defer {item.item} with a reopen condition"))
        elif item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not any(
            dependency in queue.by_id() for dependency in item.depends_on
        ):
            coordinator_actions.append(action("resume", item.item, f"Return {item.item} to ready"))
            if item.attempt is None:
                coordinator_actions.append(action("defer", item.item, f"Defer {item.item} with a reopen condition"))
        elif item.state == WorkState.DEFERRED:
            coordinator_actions.append(action("reopen", item.item, f"Reopen {item.item} for intake"))
    inbox = work_root / "inbox"
    if inbox.is_dir():
        for path in sorted(inbox.glob("*.json")):
            proposal_id = path.stem
            proposal_revision = hashlib.sha256(path.read_bytes()).hexdigest()
            coordinator_actions.extend(
                (
                    action("accept-proposal", proposal_id, f"Accept proposal {proposal_id}", proposal_revision),
                    action("merge-proposal", proposal_id, f"Merge proposal {proposal_id}", proposal_revision),
                    action("return-proposal", proposal_id, f"Return proposal {proposal_id}", proposal_revision),
                    action("reject-proposal", proposal_id, f"Reject proposal {proposal_id}", proposal_revision),
                )
            )
    coordinator_actions.append(action("transfer-coordinator", "ledger", "Transfer coordinator ownership"))
    return tuple(coordinator_actions)
