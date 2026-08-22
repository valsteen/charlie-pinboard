import hashlib
from dataclasses import dataclass
from pathlib import Path

from charlie_pinboard.domain.model import WorkState
from charlie_pinboard.legacy.atomic import PlatformNotSupportedError, transition_lock
from charlie_pinboard.legacy.authority import resolve_authority
from charlie_pinboard.legacy.markdown import parse_current, parse_item, parse_queue
from charlie_pinboard.legacy.validate import validate_live_work_state


class OverviewError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class OverviewItem:
    item_id: str
    label: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class WorkOverview:
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    inbox: tuple[str, ...]
    immediate_options: tuple[str, ...]


def _is_immediate(item: OverviewItem, live_ids: frozenset[str]) -> bool:
    if item.state in {WorkState.INTAKE, WorkState.READY, WorkState.DEFERRED}:
        return True
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED}:
        return not any(dependency in live_ids for dependency in item.depends_on)
    return False


def _overview_revision(root: Path, authority: str, item_ids: tuple[str, ...], inbox: tuple[str, ...]) -> str:
    digest = hashlib.sha256(authority.encode())
    paths = [root / "queue.md", root / "current.md"]
    paths.extend(root / "items" / f"{item_id}.md" for item_id in item_ids)
    for path in paths:
        relative = str(path.relative_to(root)).encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    for proposal_id in inbox:
        encoded = proposal_id.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def read_overview(work_root: Path, project_root: Path) -> WorkOverview:
    try:
        with transition_lock(work_root.resolve()):
            report = validate_live_work_state(work_root, project_root)
            if not report.valid:
                raise OverviewError("WORK_STATE_INVALID", report.render())
            authority = resolve_authority(work_root)
            root = authority.work_root
            queue = parse_queue(root / "queue.md")
            current = parse_current(root / "current.md")
            items = tuple(
                OverviewItem(
                    item.item,
                    parse_item(root / "items" / f"{item.item}.md").user_label,
                    item.state,
                    item.timing,
                    item.depends_on,
                    item.attempt,
                    item.next_action,
                    item.notes,
                )
                for item in queue.items
            )
            live_ids = frozenset(item.item_id for item in items)
            inbox = tuple(sorted(path.stem for path in (root / "inbox").glob("*.json")))
            return WorkOverview(
                "repo-work-overview/v1",
                authority.version.value,
                _overview_revision(root, authority.version.value, tuple(item.item_id for item in items), inbox),
                current.focus_item,
                current.focus_attempt,
                tuple(
                    item.attempt_id for item in items if item.state == WorkState.ACTIVE and item.attempt_id is not None
                ),
                items,
                inbox,
                tuple(item.item_id for item in items if _is_immediate(item, live_ids)),
            )
    except PlatformNotSupportedError as error:
        raise OverviewError("PLATFORM_NOT_SUPPORTED", str(error)) from error
