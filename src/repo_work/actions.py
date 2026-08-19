import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import assert_never

from repo_work.authority import AuthorityVersion, resolve_authority
from repo_work.coordinator import read_coordinator
from repo_work.leases import (
    LeaseError,
    read_attempt_lease,
    read_coordination_lease,
    require_attempt,
    require_coordination,
)
from repo_work.markdown import parse_item, parse_queue
from repo_work.model import Queue, QueueItem, WorkState
from repo_work.resources import ResourceError, read_resource_claim, require_resource
from repo_work.revisions import subject_revision
from repo_work.validate import validate_work_state


class ActionError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ActionKind(Enum):
    ACCEPT_PROPOSAL = "accept-proposal"
    ACTIVATE = "activate"
    BLOCK = "block"
    BLOCK_ITEM = "block-item"
    COMPLETE = "complete"
    CLOSE = "close"
    CONTINUE = "continue"
    DEFER = "defer"
    DISPATCH = "dispatch"
    INSPECT = "inspect"
    MARK_READY = "mark-ready"
    MERGE_PROPOSAL = "merge-proposal"
    PAUSE = "pause"
    REJECT_PROPOSAL = "reject-proposal"
    REOPEN = "reopen"
    REPORT_BLOCKER = "report-blocker"
    RESUME = "resume"
    RETURN_PROPOSAL = "return-proposal"
    SUBMIT_REVIEW = "submit-review"
    TRANSFER_COORDINATOR = "transfer-coordinator"


class AuthorizationKind(Enum):
    COORDINATOR = "coordinator"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    OBSERVER = "observer"


class Role(Enum):
    COORDINATOR = "coordinator"
    WORKER = "worker"
    OBSERVER = "observer"


@dataclass(frozen=True, slots=True)
class ResourceToken:
    resource_id: str
    host_id: str
    lease_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class Action:
    action_id: str
    kind: ActionKind
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: str | None = None
    resource_claims: tuple[ResourceToken, ...] = ()


def state_revision(work_root: Path) -> str:
    authority = resolve_authority(work_root)
    root = authority.work_root
    paths: list[Path] = [root / "queue.md", root / "current.md"]
    coordinator = root / "coordinator.json"
    if coordinator.is_file():
        paths.append(coordinator)
    directories = [root / "items", root / "attempts"]
    if authority.version == AuthorityVersion.V2:
        directories.extend((root / "inbox", root / "resources"))
    for directory in directories:
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    digest = hashlib.sha256()

    def relative_name(candidate: Path) -> str:
        return str(candidate.relative_to(root))

    for path in sorted(paths, key=relative_name):
        relative = str(path.relative_to(root)).encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def coordinator_generation(work_root: Path) -> int:
    authority = resolve_authority(work_root)
    match authority.version:
        case AuthorityVersion.V1:
            return read_coordinator(authority.work_root / "coordinator.json").generation
        case AuthorityVersion.V2:
            lease = authority.work_root / "leases" / "coordination.md"
            if not lease.is_file():
                return 0
            record = read_coordination_lease(authority.work_root)
            return record.generation if record is not None else 0
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class ActionFactory:
    revision: str
    generation: int
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: str | None = None

    def make(
        self,
        kind: ActionKind,
        subject: str,
        label: str,
        subject_revision: str | None = None,
        resource_claims: tuple[ResourceToken, ...] = (),
    ) -> Action:
        return Action(
            action_id=f"{kind.value}:{subject}",
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=self.revision,
            coordinator_generation=self.generation,
            subject_revision=subject_revision,
            authorization=self.authorization,
            lease_id=self.lease_id,
            resource_claims=resource_claims,
        )


def _resource_tokens(work_root: Path, item: QueueItem, factory: ActionFactory) -> tuple[ResourceToken, ...]:
    if item.attempt is None or not factory.lease_id:
        return ()
    resources = parse_item(work_root / "items" / f"{item.item}.md").resources
    if not resources:
        return ()
    try:
        attempt = require_attempt(work_root, item.attempt, factory.lease_id, factory.generation)
        result: list[ResourceToken] = []
        for resource_id in resources:
            claim = read_resource_claim(work_root, resource_id, attempt.host_id)
            if claim.attempt_id != item.attempt or claim.task_id != attempt.task_id:
                raise ActionError("RESOURCE_BUSY", f"Resource '{resource_id}' is not held by this attempt.")
            require_resource(work_root, resource_id, attempt.host_id, claim.lease_id, claim.generation)
            result.append(ResourceToken(resource_id, attempt.host_id, claim.lease_id, claim.generation))
        return tuple(result)
    except (LeaseError, ResourceError) as error:
        raise ActionError(error.code, str(error).partition(": ")[2]) from error


def _worker_actions(work_root: Path, items: tuple[QueueItem, ...], factory: ActionFactory) -> tuple[Action, ...]:
    result: list[Action] = []
    for item in items:
        if item.state == WorkState.ACTIVE and item.attempt is not None:
            resource_claims = _resource_tokens(work_root, item, factory)
            result.extend(
                (
                    factory.make(
                        ActionKind.CONTINUE,
                        item.attempt,
                        f"Continue {item.item}",
                        subject_revision(work_root, item.item),
                        resource_claims,
                    ),
                    factory.make(
                        ActionKind.REPORT_BLOCKER,
                        item.attempt,
                        f"Report a blocker for {item.item}",
                        subject_revision(work_root, item.item),
                        resource_claims,
                    ),
                    factory.make(
                        ActionKind.SUBMIT_REVIEW,
                        item.attempt,
                        f"Submit {item.item} for review",
                        subject_revision(work_root, item.item),
                        resource_claims,
                    ),
                )
            )
    return tuple(result)


def _active_coordinator_actions(items: tuple[QueueItem, ...], factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    for item in items:
        if item.state not in {WorkState.ACTIVE, WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == WorkState.ACTIVE:
            result.extend(
                (
                    factory.make(ActionKind.CONTINUE, item.attempt, f"Continue {item.item}"),
                    factory.make(ActionKind.DISPATCH, item.attempt, f"Prepare a worker launch for {item.item}"),
                    factory.make(ActionKind.PAUSE, item.attempt, f"Pause and preserve {item.item}"),
                    factory.make(ActionKind.BLOCK, item.attempt, f"Block {item.item} on a named condition"),
                )
            )
        result.append(factory.make(ActionKind.COMPLETE, item.attempt, f"Accept and complete {item.item}"))
    return result


def _intake_actions(item: QueueItem, factory: ActionFactory) -> list[Action]:
    return [
        factory.make(ActionKind.MARK_READY, item.item, f"Mark {item.item} ready"),
        factory.make(ActionKind.BLOCK_ITEM, item.item, f"Block {item.item} on a named condition"),
        factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
    ]


def _item_actions(item: QueueItem, queue: Queue, factory: ActionFactory) -> list[Action]:
    close = factory.make(ActionKind.CLOSE, item.item, f"Record a terminal decision for {item.item}")
    if item.state == WorkState.INTAKE:
        return [*_intake_actions(item, factory), close]
    if item.state == WorkState.READY:
        return [
            factory.make(ActionKind.ACTIVATE, item.item, f"Activate {item.item}"),
            factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    dependencies_live = any(dependency in queue.by_id() for dependency in item.depends_on)
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not dependencies_live:
        result = [factory.make(ActionKind.RESUME, item.item, f"Return {item.item} to ready")]
        if item.attempt is None:
            result.append(factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"))
        return [*result, close]
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED}:
        return [close]
    if item.state == WorkState.DEFERRED:
        return [factory.make(ActionKind.REOPEN, item.item, f"Reopen {item.item} for intake"), close]
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
                    factory.make(
                        ActionKind.ACCEPT_PROPOSAL, proposal_id, f"Accept proposal {proposal_id}", proposal_revision
                    ),
                    factory.make(
                        ActionKind.MERGE_PROPOSAL, proposal_id, f"Merge proposal {proposal_id}", proposal_revision
                    ),
                    factory.make(
                        ActionKind.RETURN_PROPOSAL, proposal_id, f"Return proposal {proposal_id}", proposal_revision
                    ),
                    factory.make(
                        ActionKind.REJECT_PROPOSAL, proposal_id, f"Reject proposal {proposal_id}", proposal_revision
                    ),
                )
            )
    return result


def _coordinator_actions(work_root: Path, queue: Queue, factory: ActionFactory) -> tuple[Action, ...]:
    result = _active_coordinator_actions(queue.items, factory)
    for item in queue.items:
        result.extend(_item_actions(item, queue, factory))
    result.extend(_proposal_actions(work_root, factory))
    if (work_root / "coordinator.json").is_file():
        result.append(factory.make(ActionKind.TRANSFER_COORDINATOR, "ledger", "Transfer coordinator ownership"))
    return tuple(result)


def _v2_factory(
    base_work_root: Path,
    work_root: Path,
    role: Role,
    lease_id: str | None,
    generation: int | None,
) -> ActionFactory:
    match role:
        case Role.OBSERVER:
            return ActionFactory(state_revision(base_work_root), 0, AuthorizationKind.OBSERVER)
        case Role.WORKER:
            if lease_id is None or generation is None:
                raise ActionError("LEASE_REQUIRED", "A current worker lease identity and generation are required.")
            return ActionFactory("", generation, AuthorizationKind.ATTEMPT, lease_id)
        case Role.COORDINATOR:
            if lease_id is None or generation is None:
                raise ActionError("LEASE_REQUIRED", "A current coordinator lease identity and generation are required.")
            try:
                require_coordination(work_root, lease_id, generation)
            except LeaseError as error:
                raise ActionError(error.code, str(error).partition(": ")[2]) from error
            return ActionFactory(state_revision(base_work_root), generation, AuthorizationKind.COORDINATION, lease_id)
        case _ as unreachable:
            assert_never(unreachable)


def _v1_factory(base_work_root: Path, role: Role) -> ActionFactory:
    match role:
        case Role.OBSERVER:
            authorization = AuthorizationKind.OBSERVER
        case Role.WORKER:
            authorization = AuthorizationKind.ATTEMPT
        case Role.COORDINATOR:
            authorization = AuthorizationKind.COORDINATOR
        case _ as unreachable:
            assert_never(unreachable)
    return ActionFactory(state_revision(base_work_root), coordinator_generation(base_work_root), authorization)


def _owned_worker_items(
    work_root: Path,
    items: tuple[QueueItem, ...],
    lease_id: str,
    generation: int,
) -> tuple[QueueItem, ...]:
    owned: list[QueueItem] = []
    for item in items:
        if item.attempt is None:
            continue
        record = read_attempt_lease(work_root, item.attempt)
        if record.lease_id != lease_id or record.generation != generation:
            continue
        try:
            require_attempt(work_root, item.attempt, lease_id, generation)
        except LeaseError:
            continue
        owned.append(item)
    if not owned:
        raise ActionError("ATTEMPT_LEASE_REQUIRED", "The supplied attempt lease is not current for an active item.")
    return tuple(owned)


def actions_for(
    work_root: Path,
    project_root: Path,
    role: str | Role,
    *,
    lease_id: str | None = None,
    generation: int | None = None,
) -> tuple[Action, ...]:
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    try:
        selected_role = role if isinstance(role, Role) else Role(role)
    except ValueError as error:
        raise ActionError("ROLE_INVALID", f"Unsupported role '{role}'.") from error
    authority = resolve_authority(work_root)
    root = authority.work_root
    match authority.version:
        case AuthorityVersion.V1:
            factory = _v1_factory(work_root, selected_role)
        case AuthorityVersion.V2:
            factory = _v2_factory(work_root, root, selected_role, lease_id, generation)
        case _ as unreachable:
            assert_never(unreachable)
    queue = parse_queue(root / "queue.md")
    match selected_role:
        case Role.OBSERVER:
            return (factory.make(ActionKind.INSPECT, "ledger", "Inspect current work"),)
        case Role.WORKER:
            if authority.version == AuthorityVersion.V1:
                return _worker_actions(root, queue.items, factory)
            if lease_id is None or generation is None:
                raise ActionError("ATTEMPT_LEASE_REQUIRED", "A current attempt lease is required.")
            return _worker_actions(root, _owned_worker_items(root, queue.items, lease_id, generation), factory)
        case Role.COORDINATOR:
            return _coordinator_actions(root, queue, factory)
        case _ as unreachable:
            assert_never(unreachable)
