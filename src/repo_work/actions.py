import hashlib
from pathlib import Path

from repo_work.coordinator import read_coordinator
from repo_work.markdown import parse_queue
from repo_work.model import Queue, QueueItem, WorkState
from repo_work.records import Record
from repo_work.validate import validate_work_state


class ActionError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class Action(Record):
    action_id: str
    kind: str
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None


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
    return read_coordinator(work_root / "coordinator.json").generation


class ActionFactory(Record):
    revision: str
    generation: int

    def make(self, kind: str, subject: str, label: str, subject_revision: str | None = None) -> Action:
        return Action(
            action_id=f"{kind}:{subject}",
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=self.revision,
            coordinator_generation=self.generation,
            subject_revision=subject_revision,
        )


def _worker_actions(items: tuple[QueueItem, ...], factory: ActionFactory) -> tuple[Action, ...]:
    result: list[Action] = []
    for item in items:
        if item.state == WorkState.ACTIVE and item.attempt is not None:
            result.extend(
                (
                    factory.make("continue", item.attempt, f"Continue {item.item}"),
                    factory.make("report-blocker", item.attempt, f"Report a blocker for {item.item}"),
                    factory.make("submit-review", item.attempt, f"Submit {item.item} for review"),
                )
            )
    return tuple(result)


def _active_coordinator_actions(items: tuple[QueueItem, ...], factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    for item in items:
        if item.state != WorkState.ACTIVE or item.attempt is None:
            continue
        result.extend(
            (
                factory.make("continue", item.attempt, f"Continue {item.item}"),
                factory.make("pause", item.attempt, f"Pause and preserve {item.item}"),
                factory.make("block", item.attempt, f"Block {item.item} on a named condition"),
                factory.make("complete", item.attempt, f"Accept and complete {item.item}"),
            )
        )
    return result


def _intake_actions(item: QueueItem, factory: ActionFactory) -> list[Action]:
    return [
        factory.make("mark-ready", item.item, f"Mark {item.item} ready"),
        factory.make("block-item", item.item, f"Block {item.item} on a named condition"),
        factory.make("defer", item.item, f"Defer {item.item} with a reopen condition"),
    ]


def _item_actions(item: QueueItem, queue: Queue, factory: ActionFactory) -> list[Action]:
    if item.state == WorkState.INTAKE:
        return _intake_actions(item, factory)
    if item.state == WorkState.READY:
        return [
            factory.make("activate", item.item, f"Activate {item.item}"),
            factory.make("defer", item.item, f"Defer {item.item} with a reopen condition"),
        ]
    dependencies_live = any(dependency in queue.by_id() for dependency in item.depends_on)
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not dependencies_live:
        result = [factory.make("resume", item.item, f"Return {item.item} to ready")]
        if item.attempt is None:
            result.append(factory.make("defer", item.item, f"Defer {item.item} with a reopen condition"))
        return result
    if item.state == WorkState.DEFERRED:
        return [factory.make("reopen", item.item, f"Reopen {item.item} for intake")]
    return []


def _proposal_actions(work_root: Path, factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    inbox = work_root / "inbox"
    if inbox.is_dir():
        for path in sorted(inbox.glob("*.json")):
            proposal_id = path.stem
            proposal_revision = hashlib.sha256(path.read_bytes()).hexdigest()
            result.extend(
                (
                    factory.make("accept-proposal", proposal_id, f"Accept proposal {proposal_id}", proposal_revision),
                    factory.make("merge-proposal", proposal_id, f"Merge proposal {proposal_id}", proposal_revision),
                    factory.make("return-proposal", proposal_id, f"Return proposal {proposal_id}", proposal_revision),
                    factory.make("reject-proposal", proposal_id, f"Reject proposal {proposal_id}", proposal_revision),
                )
            )
    return result


def _coordinator_actions(work_root: Path, queue: Queue, factory: ActionFactory) -> tuple[Action, ...]:
    result = _active_coordinator_actions(queue.items, factory)
    for item in queue.items:
        result.extend(_item_actions(item, queue, factory))
    result.extend(_proposal_actions(work_root, factory))
    result.append(factory.make("transfer-coordinator", "ledger", "Transfer coordinator ownership"))
    return tuple(result)


def actions_for(work_root: Path, project_root: Path, role: str) -> tuple[Action, ...]:
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    if role not in {"coordinator", "worker", "observer"}:
        raise ActionError("ROLE_INVALID", f"Unsupported role '{role}'.")
    factory = ActionFactory(state_revision(work_root), coordinator_generation(work_root))
    queue = parse_queue(work_root / "queue.md")
    if role == "observer":
        return (factory.make("inspect", "ledger", "Inspect current work"),)
    if role == "worker":
        return _worker_actions(queue.items, factory)
    return _coordinator_actions(work_root, queue, factory)
