import json
from collections.abc import Callable
from copy import replace
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from repo_work.actions import Action
from repo_work.coordinator import CoordinatorRegistration
from repo_work.markdown import (
    CurrentPointer,
    parse_current,
    parse_queue,
    render_current,
    render_queue,
    replace_header_fields,
)
from repo_work.model import SCHEMA_V1, Queue, QueueItem, WorkState
from repo_work.proposals import Proposal, ProposalDispositionKind, ProposalHistory, read_proposal
from repo_work.transaction_store import ChangeSet, FileChange, delete_change, write_bytes_change, write_change
from repo_work.transition_input import (
    AcceptProposalInput,
    ActivateInput,
    BlockInput,
    DeferInput,
    EvidenceInput,
    MergeProposalInput,
    ReasonInput,
    TransferCoordinatorInput,
    TransitionInput,
)


class TransitionPlanError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class PlanContext:
    work_root: Path
    project_root: Path
    queue: Queue
    current: CurrentPointer

    @property
    def items(self) -> list[QueueItem]:
        return list(self.queue.items)


type PlanHandler = Callable[[PlanContext, Action, TransitionInput], ChangeSet]


def _queue_change(context: PlanContext, items: list[QueueItem]) -> FileChange:
    return write_change("queue.md", render_queue(context.queue, tuple(items)))


def _item_index(items: list[QueueItem], item_id: str) -> int:
    index = next((position for position, item in enumerate(items) if item.item == item_id), None)
    if index is None:
        raise TransitionPlanError("ITEM_NOT_FOUND", f"Item '{item_id}' does not exist.")
    return index


def _attempt_index(items: list[QueueItem], attempt: str) -> int:
    index = next((position for position, item in enumerate(items) if item.attempt == attempt), None)
    if index is None:
        raise TransitionPlanError("ATTEMPT_NOT_FOUND", f"Attempt '{attempt}' does not exist.")
    return index


def _attempt_text(item: str, value: ActivateInput) -> str:
    updated = date.today().isoformat()
    return (
        "---\n"
        "kind: work-attempt\n"
        f"schema: {SCHEMA_V1}\n"
        f"attempt: {value.attempt}\n"
        f"item: {item}\n"
        "state: active\n"
        f"branch: {value.branch}\n"
        f"base_revision: {value.base_revision}\n"
        f"owner: {value.owner}\n"
        f'updated: "{updated}"\n'
        "---\n\n"
        f"# Attempt: {item}\n"
    )


def _activate(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(ActivateInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    if items[index].state != WorkState.READY:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not ready.")
    attempt_path = context.work_root / "attempts" / value.attempt / "attempt.md"
    if attempt_path.exists():
        raise TransitionPlanError("ATTEMPT_ALREADY_EXISTS", f"Attempt '{value.attempt}' already exists.")
    items[index] = replace(items[index], state=WorkState.ACTIVE, attempt=value.attempt, next_action="continue")
    return ChangeSet.of(
        write_change(f"attempts/{value.attempt}/attempt.md", _attempt_text(action.subject, value)),
        _queue_change(context, items),
        write_change("current.md", render_current(action.subject, value.attempt, "continue")),
    )


def _pause_or_block(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(ReasonInput | BlockInput, value)
    items = context.items
    index = _attempt_index(items, action.subject)
    if items[index].state != WorkState.ACTIVE:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
    target = WorkState.PAUSED if action.kind == "pause" else WorkState.BLOCKED
    dependencies = items[index].depends_on
    if isinstance(value, BlockInput):
        dependencies = tuple(dict.fromkeys((*dependencies, *value.depends_on)))
    items[index] = replace(
        items[index],
        state=target,
        depends_on=dependencies,
        next_action="resume" if target == WorkState.PAUSED else None,
        notes=value.reason,
    )
    attempt_path = f"attempts/{action.subject}/attempt.md"
    attempt_text = replace_header_fields(
        (context.work_root / attempt_path).read_text(encoding="utf-8"),
        {"state": target.value, "updated": f'"{date.today().isoformat()}"'},
    )
    changes = [write_change(attempt_path, attempt_text), _queue_change(context, items)]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", render_current(None, None, "select")))
    return ChangeSet(tuple(changes))


def _complete(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(EvidenceInput, value)
    items = context.items
    index = _attempt_index(items, action.subject)
    item = items[index]
    if item.state != WorkState.ACTIVE:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
    item_path = f"items/{item.item}.md"
    history_path = f"history/items/{item.item}.md"
    if (context.work_root / history_path).exists():
        raise TransitionPlanError("HISTORY_RECORD_EXISTS", f"History already contains '{item.item}'.")
    history_text = replace_header_fields(
        (context.work_root / item_path).read_text(encoding="utf-8"),
        {"kind": "work-history", "updated": f'"{date.today().isoformat()}"'},
        {"state": "done", "evidence": json.dumps(value.evidence)},
    )
    attempt_path = f"attempts/{action.subject}/attempt.md"
    attempt_text = replace_header_fields(
        (context.work_root / attempt_path).read_text(encoding="utf-8"),
        {"state": "review", "updated": f'"{date.today().isoformat()}"'},
    )
    changes = [
        write_change(history_path, history_text),
        write_change(attempt_path, attempt_text),
        _queue_change(context, [candidate for candidate in items if candidate.item != item.item]),
    ]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", render_current(None, None, "select")))
    changes.append(delete_change(item_path))
    return ChangeSet(tuple(changes))


def _resume(context: PlanContext, action: Action, _value: TransitionInput) -> ChangeSet:
    items = context.items
    index = _item_index(items, action.subject)
    item = items[index]
    if item.state not in {WorkState.PAUSED, WorkState.BLOCKED}:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not paused or blocked.")
    live_ids = {candidate.item for candidate in items}
    if any(dependency in live_ids for dependency in item.depends_on):
        raise TransitionPlanError("DEPENDENCY_NOT_SATISFIED", f"Item '{action.subject}' still has a live dependency.")
    if item.attempt is None:
        items[index] = replace(item, state=WorkState.READY, next_action="activate")
        return ChangeSet.of(_queue_change(context, items))
    attempt_path = f"attempts/{item.attempt}/attempt.md"
    attempt_text = replace_header_fields(
        (context.work_root / attempt_path).read_text(encoding="utf-8"),
        {"state": "active", "updated": f'"{date.today().isoformat()}"'},
    )
    items[index] = replace(item, state=WorkState.ACTIVE, next_action="continue")
    return ChangeSet.of(
        write_change(attempt_path, attempt_text),
        _queue_change(context, items),
        write_change("current.md", render_current(item.item, item.attempt, "continue")),
    )


def _reopen(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(EvidenceInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    if items[index].state != WorkState.DEFERRED:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not deferred.")
    items[index] = replace(
        items[index],
        state=WorkState.INTAKE,
        timing=None,
        next_action="review-intake",
        notes=f"Reopened: {value.evidence}",
    )
    return ChangeSet.of(_queue_change(context, items))


def _mark_ready(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(ReasonInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    if items[index].state != WorkState.INTAKE:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not in intake.")
    items[index] = replace(items[index], state=WorkState.READY, next_action="activate", notes=f"Ready: {value.reason}")
    return ChangeSet.of(_queue_change(context, items))


def _block_item(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(BlockInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    if items[index].state not in {WorkState.INTAKE, WorkState.READY}:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' cannot be blocked now.")
    items[index] = replace(
        items[index], state=WorkState.BLOCKED, depends_on=value.depends_on, next_action=None, notes=value.reason
    )
    return ChangeSet.of(_queue_change(context, items))


def _defer(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(DeferInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    item = items[index]
    if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED} or item.attempt is not None:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' cannot be deferred now.")
    items[index] = replace(
        item, state=WorkState.DEFERRED, timing=value.timing, next_action=None, notes=value.reopen_condition
    )
    return ChangeSet.of(_queue_change(context, items))


def _proposal_history(
    proposal: Proposal,
    disposition: ProposalDispositionKind,
    target: str | None,
    reason: str | None = None,
) -> bytes:
    return ProposalHistory(
        proposal=proposal,
        disposition=disposition,
        target=target,
        coordinator_reason=reason,
    ).render()


def _proposal_paths(context: PlanContext, action: Action) -> tuple[str, str, Proposal]:
    inbox = f"inbox/{action.subject}.json"
    history = f"history/proposals/{action.subject}.json"
    if (context.work_root / history).exists():
        raise TransitionPlanError("PROPOSAL_HISTORY_EXISTS", f"Proposal history already contains '{action.subject}'.")
    return inbox, history, read_proposal(context.work_root / inbox)


def _accept_proposal(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(AcceptProposalInput, value)
    items = context.items
    if any(item.item == value.item for item in items) or (context.work_root / "items" / f"{value.item}.md").exists():
        raise TransitionPlanError("ITEM_ALREADY_EXISTS", f"Item '{value.item}' already exists.")
    inbox, history, proposal = _proposal_paths(context, action)
    item = QueueItem(
        item=value.item,
        state=WorkState(value.state.value),
        timing=value.timing,
        depends_on=value.depends_on,
        attempt=None,
        source=f"proposal:{action.subject}",
        next_action=value.next_action,
        notes=proposal.why_it_matters,
    )
    item_text = (
        "---\n"
        "kind: work-item\n"
        f"schema: {SCHEMA_V1}\n"
        f"item: {value.item}\n"
        f"user_label: {json.dumps(proposal.user_label)}\n"
        f'updated: "{date.today().isoformat()}"\n'
        "---\n\n"
        f"# {proposal.user_label}\n\n"
        "## Context arc\n\n"
        f"Before and trigger: {proposal.trigger}\n\n"
        f"Why it matters: {proposal.why_it_matters}\n\n"
        f"After and trajectory: {proposal.effect} {proposal.unlock}\n"
    )
    return ChangeSet.of(
        write_change(f"items/{value.item}.md", item_text),
        _queue_change(context, [*items, item]),
        write_bytes_change(history, _proposal_history(proposal, ProposalDispositionKind.ACCEPTED, value.item)),
        delete_change(inbox),
    )


def _merge_proposal(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(MergeProposalInput, value)
    item_path = f"items/{value.target}.md"
    if (
        not any(item.item == value.target for item in context.queue.items)
        or not (context.work_root / item_path).is_file()
    ):
        raise TransitionPlanError("ITEM_NOT_FOUND", f"Merge target '{value.target}' does not exist.")
    inbox, history, proposal = _proposal_paths(context, action)
    item_text = (context.work_root / item_path).read_text(encoding="utf-8")
    item_text += f"\n## Intake evidence: {action.subject}\n\n{proposal.trigger}\n"
    return ChangeSet.of(
        write_change(item_path, item_text),
        write_bytes_change(history, _proposal_history(proposal, ProposalDispositionKind.MERGED, value.target)),
        delete_change(inbox),
    )


def _dispose_proposal(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(ReasonInput, value)
    inbox, history, proposal = _proposal_paths(context, action)
    disposition = (
        ProposalDispositionKind.RETURNED if action.kind == "return-proposal" else ProposalDispositionKind.REJECTED
    )
    return ChangeSet.of(
        write_bytes_change(history, _proposal_history(proposal, disposition, None, value.reason)),
        delete_change(inbox),
    )


def _transfer_coordinator(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(TransferCoordinatorInput, value)
    replacement = CoordinatorRegistration(
        schema=SCHEMA_V1,
        project_root=str(context.project_root.resolve()),
        task_id=value.task_id,
        host_id=value.host_id,
        generation=action.coordinator_generation + 1,
        registered_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return ChangeSet.of(write_bytes_change("coordinator.json", replacement.render()))


HANDLERS: dict[str, PlanHandler] = {
    "activate": _activate,
    "pause": _pause_or_block,
    "block": _pause_or_block,
    "complete": _complete,
    "resume": _resume,
    "reopen": _reopen,
    "mark-ready": _mark_ready,
    "block-item": _block_item,
    "defer": _defer,
    "accept-proposal": _accept_proposal,
    "merge-proposal": _merge_proposal,
    "return-proposal": _dispose_proposal,
    "reject-proposal": _dispose_proposal,
    "transfer-coordinator": _transfer_coordinator,
}


def plan_transition(work_root: Path, project_root: Path, action: Action, value: TransitionInput) -> ChangeSet:
    context = PlanContext(
        work_root,
        project_root,
        parse_queue(work_root / "queue.md"),
        parse_current(work_root / "current.md"),
    )
    handler = HANDLERS.get(action.kind)
    if handler is None:
        raise TransitionPlanError("ACTION_NOT_MUTATING", f"Action '{action.kind}' is not a canonical transition.")
    return handler(context, action, value)
