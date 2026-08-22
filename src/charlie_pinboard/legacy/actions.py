import hashlib
from pathlib import Path
from typing import assert_never

from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Role,
    available_actions,
)
from charlie_pinboard.domain.errors import DecisionError
from charlie_pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, ProposalId, ResourceId
from charlie_pinboard.domain.model import (
    AttemptAuthority,
    AttemptRecord,
    AttemptState,
    LedgerSnapshot,
    ProposalRecord,
    ResourceAuthority,
    SubjectRevision,
    WorkItem,
    WorkState,
)
from charlie_pinboard.domain.resource_decisions import ResourceToken
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority
from charlie_pinboard.legacy.coordinator import read_coordinator
from charlie_pinboard.legacy.leases import (
    LeaseError,
    read_attempt_lease,
    read_coordination_lease,
    require_attempt,
    require_coordination,
)
from charlie_pinboard.legacy.markdown import QueueItem, parse_item, parse_queue
from charlie_pinboard.legacy.resources import ResourceError, read_resource_claim, require_resource
from charlie_pinboard.legacy.revisions import subject_revision
from charlie_pinboard.legacy.validate import validate_work_state

__all__ = ["Action", "ActionError", "ActionKind", "AuthorizationKind", "Role", "actions_for"]


class ActionError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


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


def _resource_tokens(
    work_root: Path,
    item: QueueItem,
    lease_id: str | None,
    generation: int,
) -> tuple[ResourceToken, ...]:
    if item.attempt is None or not lease_id:
        return ()
    resources = parse_item(work_root / "items" / f"{item.item}.md").resources
    if not resources:
        return ()
    try:
        attempt = require_attempt(work_root, item.attempt, lease_id, generation)
        result: list[ResourceToken] = []
        for resource_id in resources:
            claim = read_resource_claim(work_root, resource_id, attempt.host_id)
            if claim.attempt_id != item.attempt or claim.task_id != attempt.task_id:
                raise ActionError("RESOURCE_BUSY", f"Resource '{resource_id}' is not held by this attempt.")
            require_resource(work_root, resource_id, attempt.host_id, claim.lease_id, claim.generation)
            result.append(
                ResourceToken(
                    ResourceId(resource_id),
                    HostId(attempt.host_id),
                    LeaseId(claim.lease_id),
                    claim.generation,
                )
            )
        return tuple(result)
    except (LeaseError, ResourceError) as error:
        raise ActionError(error.code, str(error).partition(": ")[2]) from error


def _proposal_records(work_root: Path) -> tuple[ProposalRecord, ...]:
    inbox = work_root / "inbox"
    if not inbox.is_dir():
        return ()
    return tuple(
        ProposalRecord(ProposalId(path.stem), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(inbox.glob("*.json"))
    )


def _v2_actor(
    work_root: Path,
    role: Role,
    lease_id: str | None,
    generation: int | None,
) -> ActorAuthority:
    match role:
        case Role.OBSERVER:
            return ActorAuthority(role, AuthorizationKind.OBSERVER, 0)
        case Role.WORKER:
            if lease_id is None or generation is None:
                raise ActionError("LEASE_REQUIRED", "A current worker lease identity and generation are required.")
            return ActorAuthority(role, AuthorizationKind.ATTEMPT, generation, LeaseId(lease_id), (), False)
        case Role.COORDINATOR:
            if lease_id is None or generation is None:
                raise ActionError("LEASE_REQUIRED", "A current coordinator lease identity and generation are required.")
            try:
                require_coordination(work_root, lease_id, generation)
            except LeaseError as error:
                raise ActionError(error.code, str(error).partition(": ")[2]) from error
            return ActorAuthority(role, AuthorizationKind.COORDINATION, generation, LeaseId(lease_id))
        case _ as unreachable:
            assert_never(unreachable)


def _v1_actor(base_work_root: Path, role: Role) -> ActorAuthority:
    match role:
        case Role.OBSERVER:
            authorization = AuthorizationKind.OBSERVER
        case Role.WORKER:
            authorization = AuthorizationKind.ATTEMPT
        case Role.COORDINATOR:
            authorization = AuthorizationKind.COORDINATOR
        case _ as unreachable:
            assert_never(unreachable)
    return ActorAuthority(role, authorization, coordinator_generation(base_work_root))


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


def _attempt_state(state: WorkState) -> AttemptState:
    match state:
        case WorkState.ACTIVE:
            return AttemptState.ACTIVE
        case WorkState.PAUSED:
            return AttemptState.PAUSED
        case WorkState.BLOCKED:
            return AttemptState.BLOCKED
        case WorkState.REVIEW:
            return AttemptState.REVIEW
        case WorkState.INTAKE | WorkState.READY | WorkState.DEFERRED:
            raise ActionError("WORK_STATE_INVALID", f"State '{state.value}' cannot own an attempt.")
        case _ as unreachable:
            assert_never(unreachable)


def _snapshot(
    base_work_root: Path,
    work_root: Path,
    items: tuple[QueueItem, ...],
    actor: ActorAuthority,
) -> LedgerSnapshot:
    snapshot_items = tuple(
        WorkItem(
            ItemId(item.item),
            item.state,
            item.timing,
            tuple(ItemId(value) for value in item.depends_on),
            AttemptId(item.attempt) if item.attempt is not None else None,
            item.source,
            item.next_action,
            item.notes,
            item.outcome_evidence,
        )
        for item in items
    )
    attempt_items = tuple(item for item in items if item.attempt is not None)
    attempts = tuple(
        AttemptRecord(AttemptId(item.attempt), ItemId(item.item), _attempt_state(item.state))
        for item in attempt_items
        if item.attempt is not None
    )
    authorities: list[AttemptAuthority] = []
    subject_revisions: list[SubjectRevision] = []
    if actor.role == Role.WORKER:
        for item in attempt_items:
            if item.attempt not in actor.attempts:
                continue
            tokens = _resource_tokens(work_root, item, actor.lease_id, actor.generation)
            resources = tuple(
                ResourceAuthority(token.resource_id, token.host_id, token.lease_id, token.generation)
                for token in tokens
            )
            authorities.append(
                AttemptAuthority(
                    AttemptId(item.attempt), ItemId(item.item), actor.lease_id, actor.generation, resources
                )
            )
            subject_revisions.append(SubjectRevision(ItemId(item.item), subject_revision(work_root, item.item)))
    history = work_root / "history" / "items"
    return LedgerSnapshot(
        revision=state_revision(base_work_root),
        generation=actor.generation,
        items=snapshot_items,
        attempts=attempts,
        proposals=_proposal_records(work_root),
        subject_revisions=tuple(subject_revisions),
        attempt_authorities=tuple(authorities),
        history_items=tuple(ItemId(path.stem) for path in sorted(history.glob("*.md"))) if history.is_dir() else (),
        can_transfer_coordinator=(work_root / "coordinator.json").is_file(),
    )


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
            actor = _v1_actor(work_root, selected_role)
        case AuthorityVersion.V2:
            actor = _v2_actor(root, selected_role, lease_id, generation)
        case _ as unreachable:
            assert_never(unreachable)
    queue = parse_queue(root / "queue.md")
    if selected_role == Role.WORKER:
        if authority.version == AuthorityVersion.V1:
            attempts = tuple(
                AttemptId(item.attempt) for item in queue.items if item.state == WorkState.ACTIVE and item.attempt
            )
        else:
            if lease_id is None or generation is None:
                raise ActionError("ATTEMPT_LEASE_REQUIRED", "A current attempt lease is required.")
            attempts = tuple(
                AttemptId(item.attempt)
                for item in _owned_worker_items(root, queue.items, lease_id, generation)
                if item.attempt
            )
        actor = ActorAuthority(
            actor.role,
            actor.authorization,
            actor.generation,
            actor.lease_id,
            attempts,
            actor.revision_scoped,
        )
    try:
        return available_actions(_snapshot(work_root, root, queue.items, actor), actor)
    except DecisionError as error:
        raise ActionError(error.code.value, str(error).partition(": ")[2]) from error
