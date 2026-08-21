import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from repo_work.actions import state_revision
from repo_work.atomic import transition_lock
from repo_work.authority import AuthorityVersion, resolve_authority
from repo_work.leases import LeaseError, LeaseStatus, read_attempt_lease
from repo_work.markdown import ITEM_PATTERN, parse_item, parse_queue
from repo_work.model import QueueItem, WorkState
from repo_work.resources import ResourceError, read_resource_claim, require_resource
from repo_work.storage_layout import PathIdentityError, identity_child
from repo_work.validate import validate_work_state


class ParallelError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ParallelOutcome(Enum):
    LAUNCHABLE = "launchable"
    REQUIRES_SELECTION = "requires-selection"
    EXCLUDED = "excluded"


class ParallelSelection(Enum):
    ALL_SAFE = "all-safe"
    SELECTED = "selected"


class ParallelReasonCode(Enum):
    ATTEMPT_OWNED = "attempt-owned"
    DEPENDENCY_LIVE = "dependency-live"
    RESOURCE_BUSY = "resource-busy"
    RESOURCE_CONFLICT = "resource-conflict"
    RESOURCE_SELECTION_REQUIRED = "resource-selection-required"
    STATE_NOT_LAUNCHABLE = "state-not-launchable"


@dataclass(frozen=True, slots=True)
class ParallelReason:
    code: ParallelReasonCode
    message: str


@dataclass(frozen=True, slots=True)
class ParallelItem:
    item_id: str
    label: str
    state: WorkState
    attempt_id: str | None
    resources: tuple[str, ...]
    outcome: ParallelOutcome
    reasons: tuple[ParallelReason, ...] = ()


@dataclass(frozen=True, slots=True)
class ParallelPreview:
    schema: str
    revision: str
    host_id: str
    selection: ParallelSelection
    safe: bool
    launchable: tuple[ParallelItem, ...]
    requires_selection: tuple[ParallelItem, ...]
    excluded: tuple[ParallelItem, ...]


def _current_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ParallelError("PARALLEL_TIME_INVALID", "Preview time must be timezone-aware.")
    return current.astimezone(UTC)


def _validate_host(root: Path, host_id: str) -> None:
    if not host_id:
        raise ParallelError("PARALLEL_HOST_INVALID", "A host identity is required.")
    try:
        identity_child(root, root / "leases" / "resources", host_id)
    except PathIdentityError as error:
        raise ParallelError("PARALLEL_HOST_INVALID", f"Invalid host identity '{host_id}'.") from error


def _attempt_owner_reason(root: Path, item: QueueItem, current: datetime) -> ParallelReason | None:
    if item.state != WorkState.ACTIVE or item.attempt is None:
        return None
    try:
        lease = read_attempt_lease(root, item.attempt)
    except LeaseError as error:
        raise ParallelError(error.code, str(error).partition(": ")[2]) from error
    if lease.status != LeaseStatus.ACTIVE or current >= lease.expires_at:
        return None
    return ParallelReason(
        ParallelReasonCode.ATTEMPT_OWNED,
        f"Active attempt '{item.attempt}' is owned by task '{lease.task_id}' until {lease.expires_at.isoformat()}.",
    )


def _busy_resource_reason(root: Path, resource_id: str, host_id: str, current: datetime) -> ParallelReason | None:
    try:
        claim = read_resource_claim(root, resource_id, host_id)
    except ResourceError as error:
        if error.code == "RESOURCE_CLAIM_REQUIRED":
            return None
        raise ParallelError(error.code, str(error).partition(": ")[2]) from error
    if claim.status != LeaseStatus.ACTIVE or current >= claim.expires_at:
        return None
    try:
        require_resource(root, resource_id, host_id, claim.lease_id, claim.generation, now=current)
    except ResourceError as error:
        if error.code in {"ATTEMPT_LEASE_EXPIRED", "LEASE_FENCED", "RESOURCE_CLAIM_REQUIRED"}:
            return None
        raise ParallelError(error.code, str(error).partition(": ")[2]) from error
    return ParallelReason(
        ParallelReasonCode.RESOURCE_BUSY,
        f"Resource '{resource_id}' on '{host_id}' is held by attempt '{claim.attempt_id}' until "
        f"{claim.expires_at.isoformat()}.",
    )


def _base_reasons(
    root: Path,
    item: QueueItem,
    live_items: frozenset[str],
    resources: tuple[str, ...],
    host_id: str,
    current: datetime,
) -> tuple[ParallelReason, ...]:
    if item.state not in {WorkState.READY, WorkState.ACTIVE}:
        return (
            ParallelReason(
                ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item.item}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    live_dependencies = tuple(dependency for dependency in item.depends_on if dependency in live_items)
    if live_dependencies:
        return (
            ParallelReason(
                ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item.item}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    owner_reason = _attempt_owner_reason(root, item, current)
    if owner_reason is not None:
        return (owner_reason,)
    return tuple(
        reason
        for resource_id in resources
        if (reason := _busy_resource_reason(root, resource_id, host_id, current)) is not None
    )


def _preview_item(
    root: Path,
    item: QueueItem,
    outcome: ParallelOutcome,
    reasons: tuple[ParallelReason, ...] = (),
) -> ParallelItem:
    record = parse_item(root / "items" / f"{item.item}.md")
    return ParallelItem(
        item.item,
        record.user_label,
        item.state,
        item.attempt,
        record.resources,
        outcome,
        reasons,
    )


def _resource_conflicts(items: tuple[ParallelItem, ...]) -> dict[str, tuple[str, ...]]:
    by_resource: dict[str, list[str]] = {}
    for item in items:
        for resource_id in item.resources:
            by_resource.setdefault(resource_id, []).append(item.item_id)
    result: dict[str, list[str]] = {}
    for resource_id, item_ids in by_resource.items():
        if len(item_ids) < 2:
            continue
        for item_id in item_ids:
            result.setdefault(item_id, []).append(resource_id)
    return {item_id: tuple(resources) for item_id, resources in result.items()}


def _item_id(item: ParallelItem) -> str:
    return item.item_id


def _preview_revision(work_root: Path, root: Path) -> str:
    digest = hashlib.sha256(state_revision(work_root).encode())
    claim_root = root / "leases" / "resources"
    paths = sorted(claim_root.glob("*.md")) if claim_root.is_dir() else []
    for path in paths:
        relative = str(path.relative_to(root)).encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def preview_parallel(
    work_root: Path,
    project_root: Path,
    host_id: str,
    *,
    selected: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ParallelPreview:
    with transition_lock(work_root.resolve()):
        current = _current_time(now)
        return _preview_parallel_locked(work_root, project_root, host_id, selected, current)


def _preview_parallel_locked(
    work_root: Path,
    project_root: Path,
    host_id: str,
    selected: tuple[str, ...],
    current: datetime,
) -> ParallelPreview:
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ParallelError("WORK_STATE_INVALID", report.render())
    authority = resolve_authority(work_root)
    if authority.version != AuthorityVersion.V2:
        raise ParallelError(
            "MIGRATION_REQUIRED",
            "Parallel preview requires schema v2; run 'charlie migrate --to v2' first.",
        )
    root = authority.work_root
    _validate_host(root, host_id)
    revision = _preview_revision(work_root, root)
    queue = parse_queue(root / "queue.md")
    by_id = queue.by_id()
    if len(selected) != len(set(selected)):
        raise ParallelError("PARALLEL_SELECTION_INVALID", "Selected item identities must be unique.")
    invalid = tuple(item_id for item_id in selected if ITEM_PATTERN.fullmatch(item_id) is None or item_id not in by_id)
    if invalid:
        raise ParallelError("PARALLEL_SELECTION_INVALID", f"Unknown selected items: {', '.join(invalid)}.")
    items = tuple(by_id[item_id] for item_id in selected) if selected else queue.items
    live_items = frozenset(by_id)
    launchable: list[ParallelItem] = []
    excluded: list[ParallelItem] = []
    for item in items:
        record = parse_item(root / "items" / f"{item.item}.md")
        reasons = _base_reasons(root, item, live_items, record.resources, host_id, current)
        if reasons:
            excluded.append(_preview_item(root, item, ParallelOutcome.EXCLUDED, reasons))
        else:
            launchable.append(_preview_item(root, item, ParallelOutcome.LAUNCHABLE))
    conflicts = _resource_conflicts(tuple(launchable))
    requires_selection: list[ParallelItem] = []
    if conflicts:
        retained: list[ParallelItem] = []
        for item in launchable:
            resources = conflicts.get(item.item_id)
            if resources is None:
                retained.append(item)
                continue
            code = ParallelReasonCode.RESOURCE_CONFLICT if selected else ParallelReasonCode.RESOURCE_SELECTION_REQUIRED
            message = (
                f"Selected items share host-local resources: {', '.join(resources)}."
                if selected
                else f"Multiple candidates need host-local resources: {', '.join(resources)}; select one explicitly."
            )
            revised = ParallelItem(
                item.item_id,
                item.label,
                item.state,
                item.attempt_id,
                item.resources,
                ParallelOutcome.EXCLUDED if selected else ParallelOutcome.REQUIRES_SELECTION,
                (ParallelReason(code, message),),
            )
            (excluded if selected else requires_selection).append(revised)
        launchable = retained
    final_revision = _preview_revision(work_root, root)
    if final_revision != revision:
        raise ParallelError("STATE_REVISION_STALE", "Repository work state changed while the preview was prepared.")
    return ParallelPreview(
        "repo-work-parallel-preview/v1",
        revision,
        host_id,
        ParallelSelection.SELECTED if selected else ParallelSelection.ALL_SAFE,
        not selected or not excluded,
        tuple(sorted(launchable, key=_item_id)),
        tuple(sorted(requires_selection, key=_item_id)),
        tuple(sorted(excluded, key=_item_id)),
    )
