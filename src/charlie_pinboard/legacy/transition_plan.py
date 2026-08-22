import hashlib
from collections.abc import Callable
from copy import replace
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, assert_never, cast

from charlie_pinboard.domain.decisions import (
    AttemptAuthorityChange,
    AttemptChange,
    CheckpointAcceptanceChange,
    Decision,
    ItemChange,
    TransitionReceipt,
    decide,
)
from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
)
from charlie_pinboard.domain.model import (
    SCHEMA_V1,
    SCHEMA_V2,
    AcceptCheckpointInput,
    AcceptProposalInput,
    AttemptAuthority,
    AttemptRecord,
    AttemptState,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    LedgerSnapshot,
    LegacyActivateInput,
    MergeProposalInput,
    ProposalRecord,
    ReasonInput,
    ReservationState,
    ResourceAuthority,
    ResourceReservation,
    ResourceReservationCounter,
    ResourceUseLease,
    TransferCoordinatorInput,
    TransitionInput,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.legacy.actions import Action, ActionKind
from charlie_pinboard.legacy.coordinator import CoordinatorRegistration
from charlie_pinboard.legacy.leases import read_attempt_lease
from charlie_pinboard.legacy.markdown import (
    CurrentPointer,
    Queue,
    QueueItem,
    V2HeaderValue,
    encode_string_scalar,
    parse_attempt,
    parse_current,
    parse_header,
    parse_item,
    parse_queue,
    parse_queue_text,
    render_current,
    render_queue,
    render_v2_header,
    render_v2_item,
    replace_header_fields,
    replace_v2_header_fields,
)
from charlie_pinboard.legacy.proposals import Proposal, ProposalDispositionKind, ProposalHistory, read_proposal
from charlie_pinboard.legacy.resources import ResourceClaimStatus, read_resource_claim
from charlie_pinboard.legacy.transaction_store import (
    ChangeSet,
    FileChange,
    delete_change,
    write_bytes_change,
    write_change,
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
    decision: Decision | None = None

    @property
    def items(self) -> list[QueueItem]:
        return list(self.queue.items)


type PlanHandler = Callable[[PlanContext, Action, TransitionInput], ChangeSet]
type PauseActionKind = Literal[ActionKind.PAUSE, ActionKind.BLOCK]
type DispositionActionKind = Literal[ActionKind.RETURN_PROPOSAL, ActionKind.REJECT_PROPOSAL]


def _pause_target(kind: PauseActionKind) -> WorkState:
    match kind:
        case ActionKind.PAUSE:
            return WorkState.PAUSED
        case ActionKind.BLOCK:
            return WorkState.BLOCKED
        case _ as unreachable:
            assert_never(unreachable)


def _proposal_disposition(kind: DispositionActionKind) -> ProposalDispositionKind:
    match kind:
        case ActionKind.RETURN_PROPOSAL:
            return ProposalDispositionKind.RETURNED
        case ActionKind.REJECT_PROPOSAL:
            return ProposalDispositionKind.REJECTED
        case _ as unreachable:
            assert_never(unreachable)


def _queue_change(context: PlanContext, items: list[QueueItem]) -> FileChange:
    return write_change("queue.md", render_queue(context.queue, tuple(items)))


def _current_text(context: PlanContext, focus_item: str | None, focus_attempt: str | None, next_action: str) -> str:
    schema = SCHEMA_V2 if context.queue.header.get("schema") == SCHEMA_V2 else SCHEMA_V1
    return render_current(focus_item, focus_attempt, next_action, schema)


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


def _attempt_text(context: PlanContext, item: str, value: LegacyActivateInput) -> str:
    updated = date.today().isoformat()
    schema = SCHEMA_V2 if context.queue.header.get("schema") == SCHEMA_V2 else SCHEMA_V1
    if schema == SCHEMA_V2:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return (
            render_v2_header(
                {
                    "kind": "work-attempt",
                    "schema": schema,
                    "attempt": value.attempt,
                    "item": item,
                    "state": "active",
                    "branch": value.branch,
                    "base_revision": value.base_revision,
                    "provenance": value.owner,
                    "owner_task_id": "unclaimed",
                    "owner_host_id": "unclaimed",
                    "lease_id": "unclaimed",
                    "lease_generation": 0,
                    "lease_acquired_at": timestamp,
                    "lease_expires_at": timestamp,
                    "lease_status": "released",
                    "updated": updated,
                }
            )
            + f"\n# Attempt: {item}\n"
        )
    return (
        "---\n"
        "kind: work-attempt\n"
        f"schema: {schema}\n"
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
    value = cast(LegacyActivateInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    if items[index].state != WorkState.READY:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", f"Item '{action.subject}' is not ready.")
    attempt_path = context.work_root / "attempts" / value.attempt / "attempt.md"
    if attempt_path.exists():
        raise TransitionPlanError("ATTEMPT_ALREADY_EXISTS", f"Attempt '{value.attempt}' already exists.")
    items[index] = replace(items[index], state=WorkState.ACTIVE, attempt=value.attempt, next_action="continue")
    return ChangeSet.of(
        write_change(f"attempts/{value.attempt}/attempt.md", _attempt_text(context, action.subject, value)),
        _queue_change(context, items),
        write_change("current.md", _current_text(context, action.subject, value.attempt, "continue")),
    )


def _pause_or_block(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(ReasonInput | BlockInput, value)
    items = context.items
    index = _attempt_index(items, action.subject)
    if items[index].state != WorkState.ACTIVE:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
    target = _pause_target(cast(PauseActionKind, action.kind))
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
    attempt_source = (context.work_root / attempt_path).read_text(encoding="utf-8")
    attempt_text = (
        replace_v2_header_fields(attempt_source, {"state": target.value, "updated": date.today().isoformat()})
        if context.queue.header.get("schema") == SCHEMA_V2
        else replace_header_fields(
            attempt_source,
            {"state": target.value, "updated": f'"{date.today().isoformat()}"'},
        )
    )
    changes = [write_change(attempt_path, attempt_text), _queue_change(context, items)]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", _current_text(context, None, None, "select")))
    return ChangeSet(tuple(changes))


def _complete(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(EvidenceInput, value)
    items = context.items
    index = _attempt_index(items, action.subject)
    item = items[index]
    if item.state not in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "The named attempt is not active.")
    item_path = f"items/{item.item}.md"
    history_path = f"history/items/{item.item}.md"
    if (context.work_root / history_path).exists():
        raise TransitionPlanError("HISTORY_RECORD_EXISTS", f"History already contains '{item.item}'.")
    item_text = (context.work_root / item_path).read_text(encoding="utf-8")
    item_header = parse_header(context.work_root / item_path)
    replacements = {"kind": "work-history", "updated": date.today().isoformat()}
    additions = {"evidence": value.evidence}
    if "state" in item_header:
        replacements["state"] = "done"
    else:
        additions["state"] = "done"
    history_text = (
        replace_v2_header_fields(item_text, replacements, additions)
        if context.queue.header.get("schema") == SCHEMA_V2
        else replace_header_fields(
            item_text,
            {**replacements, "updated": f'"{replacements["updated"]}"'},
            {"evidence": encode_string_scalar(value.evidence), "state": additions["state"]}
            if "state" in additions
            else {"evidence": encode_string_scalar(value.evidence)},
        )
    )
    attempt_path = f"attempts/{action.subject}/attempt.md"
    attempt_source = context.work_root / attempt_path
    attempt_header = parse_header(attempt_source)
    attempt_replacements: dict[str, V2HeaderValue] = {
        "state": "done",
        "updated": date.today().isoformat(),
    }
    if attempt_header.get("schema") == SCHEMA_V2:
        generation_value = attempt_header.get("lease_generation")
        if not isinstance(generation_value, str):
            raise TransitionPlanError("LEASE_INVALID", "The completed attempt has no lease generation.")
        try:
            generation = int(generation_value)
        except ValueError as error:
            raise TransitionPlanError(
                "LEASE_INVALID", "The completed attempt has an invalid lease generation."
            ) from error
        attempt_replacements.update(
            {
                "lease_generation": generation + 1,
                "lease_status": "revoked",
            }
        )
    attempt_source_text = attempt_source.read_text(encoding="utf-8")
    attempt_text = (
        replace_v2_header_fields(attempt_source_text, attempt_replacements)
        if attempt_header.get("schema") == SCHEMA_V2
        else replace_header_fields(
            attempt_source_text,
            {"state": "done", "updated": f'"{attempt_replacements["updated"]}"'},
        )
    )
    changes = [
        write_change(history_path, history_text),
        write_change(attempt_path, attempt_text),
        *_terminal_resource_changes(context, cast(Decision, context.decision)),
        _queue_change(context, [candidate for candidate in items if candidate.item != item.item]),
    ]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", _current_text(context, None, None, "select")))
    changes.append(delete_change(item_path))
    return ChangeSet(tuple(changes))


def _close_history_text(context: PlanContext, item_path: str, outcome: CloseOutcome, reason: str) -> str:
    source_path = context.work_root / item_path
    item_text = source_path.read_text(encoding="utf-8")
    item_header = parse_header(source_path)
    updated = date.today().isoformat()
    if context.queue.header.get("schema") == SCHEMA_V2:
        return replace_v2_header_fields(
            item_text,
            {"kind": "work-history", "state": outcome.value, "updated": updated},
            {"evidence": reason},
        )
    replacements = {"kind": "work-history", "updated": f'"{updated}"'}
    additions: dict[str, str] = {}
    for field, field_value in (("state", outcome.value), ("evidence", encode_string_scalar(reason))):
        (replacements if field in item_header else additions)[field] = field_value
    return replace_header_fields(item_text, replacements, additions)


def _closed_attempt_change(context: PlanContext, attempt: str) -> FileChange:
    attempt_path = f"attempts/{attempt}/attempt.md"
    attempt_source = context.work_root / attempt_path
    attempt_header = parse_header(attempt_source)
    updated = date.today().isoformat()
    source_text = attempt_source.read_text(encoding="utf-8")
    if attempt_header.get("schema") != SCHEMA_V2:
        return write_change(
            attempt_path,
            replace_header_fields(source_text, {"state": "done", "updated": f'"{updated}"'}),
        )
    generation_value = attempt_header.get("lease_generation")
    if not isinstance(generation_value, str):
        raise TransitionPlanError("LEASE_INVALID", "The closed attempt has no lease generation.")
    try:
        generation = int(generation_value)
    except ValueError as error:
        raise TransitionPlanError("LEASE_INVALID", "The closed attempt has an invalid lease generation.") from error
    return write_change(
        attempt_path,
        replace_v2_header_fields(
            source_text,
            {
                "state": "done",
                "updated": updated,
                "lease_generation": generation + 1,
                "lease_status": "revoked",
            },
        ),
    )


def _terminal_resource_changes(context: PlanContext, decision: Decision) -> list[FileChange]:
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    changes: list[FileChange] = []
    for reservation_change in decision.reservation_changes:
        reservation = cast(ResourceReservation, reservation_change.before)
        path = context.work_root / "leases" / "resources" / f"{reservation.reservation_id}.md"
        text = replace_v2_header_fields(
            path.read_text(encoding="utf-8"),
            {
                "lease_expires_at": timestamp,
                "lease_status": ResourceClaimStatus.RELEASED.value,
            },
        )
        changes.append(write_change(str(path.relative_to(context.work_root)), text))
    return changes


def _close(context: PlanContext, action: Action, value: TransitionInput) -> ChangeSet:
    value = cast(CloseInput, value)
    items = context.items
    index = _item_index(items, action.subject)
    item = items[index]
    if item.state in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "Active or review work requires the acceptance path.")
    if value.outcome == CloseOutcome.DROPPED and any(item.item in candidate.depends_on for candidate in items):
        raise TransitionPlanError(
            "LIVE_DEPENDENTS",
            f"Item '{item.item}' still has live dependents; resolve their dependency before dropping it.",
        )
    item_path = f"items/{item.item}.md"
    history_path = f"history/items/{item.item}.md"
    if (context.work_root / history_path).exists():
        raise TransitionPlanError("HISTORY_RECORD_EXISTS", f"History already contains '{item.item}'.")
    changes = [
        write_change(history_path, _close_history_text(context, item_path, value.outcome, value.reason)),
        _queue_change(context, [candidate for candidate in items if candidate.item != item.item]),
        delete_change(item_path),
    ]
    if item.attempt is not None:
        changes.append(_closed_attempt_change(context, item.attempt))
        changes.extend(_terminal_resource_changes(context, cast(Decision, context.decision)))
    if context.current.focus_item == item.item or context.current.focus_attempt == item.attempt:
        changes.append(write_change("current.md", _current_text(context, None, None, "select")))
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
    attempt_source = (context.work_root / attempt_path).read_text(encoding="utf-8")
    attempt_text = (
        replace_v2_header_fields(attempt_source, {"state": "active", "updated": date.today().isoformat()})
        if context.queue.header.get("schema") == SCHEMA_V2
        else replace_header_fields(
            attempt_source,
            {"state": "active", "updated": f'"{date.today().isoformat()}"'},
        )
    )
    items[index] = replace(item, state=WorkState.ACTIVE, next_action="continue")
    return ChangeSet.of(
        write_change(attempt_path, attempt_text),
        _queue_change(context, items),
        write_change("current.md", _current_text(context, item.item, item.attempt, "continue")),
    )


def _submit_review(context: PlanContext, action: Action, _value: TransitionInput) -> ChangeSet:
    items = context.items
    index = _attempt_index(items, action.subject)
    item = items[index]
    if item.state != WorkState.ACTIVE:
        raise TransitionPlanError("ACTION_NOT_AVAILABLE", "Only an active attempt can be submitted for review.")
    items[index] = replace(item, state=WorkState.REVIEW, next_action="review")
    attempt_path = f"attempts/{action.subject}/attempt.md"
    attempt_source = (context.work_root / attempt_path).read_text(encoding="utf-8")
    attempt_text = (
        replace_v2_header_fields(attempt_source, {"state": "review", "updated": date.today().isoformat()})
        if context.queue.header.get("schema") == SCHEMA_V2
        else replace_header_fields(
            attempt_source,
            {"state": "review", "updated": f'"{date.today().isoformat()}"'},
        )
    )
    changes = [write_change(attempt_path, attempt_text), _queue_change(context, items)]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", _current_text(context, None, None, "select")))
    return ChangeSet(tuple(changes))


def _return_resource_changes(context: PlanContext, decision: Decision, timestamp: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for use_lease_change in decision.resource_use_lease_changes:
        path = context.work_root / "leases" / "resources" / f"{use_lease_change.before.reservation_id}.md"
        text = replace_v2_header_fields(
            path.read_text(encoding="utf-8"),
            {
                "lease_expires_at": timestamp,
                "lease_status": ResourceClaimStatus.RESERVED.value,
            },
        )
        changes.append(write_change(str(path.relative_to(context.work_root)), text))
    return changes


def _return_for_correction(
    context: PlanContext,
    action: Action,
    value: TransitionInput,
) -> ChangeSet:
    value = cast(ReasonInput, value)
    decision = cast(Decision, context.decision)
    items = context.items
    index = _attempt_index(items, action.subject)
    item = items[index]
    attempt_path = context.work_root / "attempts" / action.subject / "attempt.md"
    authority_change = cast(AttemptAuthorityChange, decision.attempt_authority_change)
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    attempt_text = replace_v2_header_fields(
        attempt_path.read_text(encoding="utf-8"),
        {
            "state": "active",
            "owner_task_id": "unclaimed",
            "owner_host_id": "unclaimed",
            "lease_id": "unclaimed",
            "lease_generation": authority_change.after.generation,
            "lease_acquired_at": timestamp,
            "lease_expires_at": timestamp,
            "lease_status": "revoked",
            "updated": date.today().isoformat(),
        },
    )
    items[index] = replace(
        item,
        state=WorkState.ACTIVE,
        next_action="reacquire-and-continue",
        notes=f"Correction requested: {value.reason}",
    )
    return ChangeSet.of(
        write_change(str(attempt_path.relative_to(context.work_root)), attempt_text),
        *_return_resource_changes(context, decision, timestamp),
        _queue_change(context, items),
        write_change(
            "current.md",
            _current_text(context, item.item, action.subject, "reacquire-and-continue"),
        ),
    )


def _checkpoint_receipt(
    acceptance: CheckpointAcceptanceChange,
    result: bytes,
    review: bytes,
) -> str:
    accepted_at = acceptance.accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return (
        render_v2_header(
            {
                "kind": "work-checkpoint",
                "schema": SCHEMA_V2,
                "checkpoint": acceptance.checkpoint,
                "attempt": acceptance.attempt,
                "candidate": acceptance.candidate,
                "evidence": acceptance.evidence,
                "accepted_at": accepted_at,
                "result_sha256": hashlib.sha256(result).hexdigest(),
                "review_sha256": hashlib.sha256(review).hexdigest(),
            }
        )
        + f"\n# Accepted checkpoint: {acceptance.checkpoint}\n\n"
        + f"Candidate: `{acceptance.candidate}`\n\n"
        + f"Acceptance evidence: {acceptance.evidence}\n"
    )


def _checkpoint_resource_changes(context: PlanContext, decision: Decision) -> list[FileChange]:
    changes: list[FileChange] = []
    for use_lease_change in decision.resource_use_lease_changes:
        path = context.work_root / "leases" / "resources" / f"{use_lease_change.before.reservation_id}.md"
        text = replace_v2_header_fields(
            path.read_text(encoding="utf-8"),
            {"lease_status": ResourceClaimStatus.RESERVED.value},
        )
        changes.append(write_change(str(path.relative_to(context.work_root)), text))
    return changes


def _accept_checkpoint(
    context: PlanContext,
    action: Action,
    value: TransitionInput,
) -> ChangeSet:
    value = cast(AcceptCheckpointInput, value)
    decision = cast(Decision, context.decision)
    acceptance = cast(CheckpointAcceptanceChange, decision.checkpoint_acceptance_change)
    items = context.items
    index = _attempt_index(items, action.subject)
    item = items[index]
    attempt_directory = context.work_root / "attempts" / action.subject
    result_path = attempt_directory / "result.md"
    review_path = attempt_directory / "review.md"
    missing = [path.name for path in (result_path, review_path) if not path.is_file()]
    if missing:
        raise TransitionPlanError(
            "CHECKPOINT_EVIDENCE_MISSING",
            f"Checkpoint acceptance requires top-level {', '.join(missing)}.",
        )
    checkpoint_directory = attempt_directory / "checkpoints" / value.checkpoint
    if checkpoint_directory.exists():
        raise TransitionPlanError(
            "CHECKPOINT_ALREADY_EXISTS",
            f"Checkpoint '{value.checkpoint}' already exists for attempt '{action.subject}'.",
        )
    result = result_path.read_bytes()
    review = review_path.read_bytes()
    authority_change = cast(AttemptAuthorityChange, decision.attempt_authority_change)
    timestamp = acceptance.accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    attempt_relative = f"attempts/{action.subject}/attempt.md"
    attempt_path = context.work_root / attempt_relative
    attempt_text = replace_v2_header_fields(
        attempt_path.read_text(encoding="utf-8"),
        {
            "state": "paused",
            "owner_task_id": "unclaimed",
            "owner_host_id": "unclaimed",
            "lease_id": "unclaimed",
            "lease_generation": authority_change.after.generation,
            "lease_acquired_at": timestamp,
            "lease_expires_at": timestamp,
            "lease_status": "revoked",
            "updated": date.today().isoformat(),
        },
    )
    items[index] = replace(
        item,
        state=WorkState.PAUSED,
        next_action="resume",
        notes=f"Checkpoint {value.checkpoint} accepted; update the next checkpoint brief before resume.",
    )
    checkpoint_relative = f"attempts/{action.subject}/checkpoints/{value.checkpoint}"
    changes = [
        write_bytes_change(f"{checkpoint_relative}/result.md", result),
        write_bytes_change(f"{checkpoint_relative}/review.md", review),
        write_change(f"{checkpoint_relative}/receipt.md", _checkpoint_receipt(acceptance, result, review)),
        write_change(attempt_relative, attempt_text),
        *_checkpoint_resource_changes(context, decision),
        _queue_change(context, items),
    ]
    if context.current.focus_attempt == action.subject:
        changes.append(write_change("current.md", _current_text(context, None, None, "select")))
    changes.extend(
        (
            delete_change(str(result_path.relative_to(context.work_root))),
            delete_change(str(review_path.relative_to(context.work_root))),
        )
    )
    return ChangeSet(tuple(changes))


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
        item,
        state=WorkState.DEFERRED,
        timing=value.timing.value,
        next_action=None,
        notes=value.reopen_condition,
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
        timing=value.timing.value if value.timing is not None else None,
        depends_on=value.depends_on,
        attempt=None,
        source=f"proposal:{action.subject}",
        next_action=value.next_action,
        notes=proposal.why_it_matters,
    )
    header = (
        render_v2_header(
            {
                "kind": "work-item",
                "schema": SCHEMA_V2,
                "item": value.item,
                "user_label": proposal.user_label,
                "updated": date.today().isoformat(),
            }
        )
        if context.queue.header.get("schema") == SCHEMA_V2
        else (
            "---\n"
            "kind: work-item\n"
            f"schema: {SCHEMA_V1}\n"
            f"item: {value.item}\n"
            f"user_label: {encode_string_scalar(proposal.user_label)}\n"
            f'updated: "{date.today().isoformat()}"\n'
            "---\n"
        )
    )
    item_text = (
        header + "\n"
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
    disposition = _proposal_disposition(cast(DispositionActionKind, action.kind))
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


HANDLERS: dict[ActionKind, PlanHandler] = {
    ActionKind.ACCEPT_CHECKPOINT: _accept_checkpoint,
    ActionKind.ACTIVATE: _activate,
    ActionKind.PAUSE: _pause_or_block,
    ActionKind.BLOCK: _pause_or_block,
    ActionKind.COMPLETE: _complete,
    ActionKind.CLOSE: _close,
    ActionKind.RESUME: _resume,
    ActionKind.SUBMIT_REVIEW: _submit_review,
    ActionKind.RETURN_FOR_CORRECTION: _return_for_correction,
    ActionKind.REOPEN: _reopen,
    ActionKind.MARK_READY: _mark_ready,
    ActionKind.BLOCK_ITEM: _block_item,
    ActionKind.DEFER: _defer,
    ActionKind.ACCEPT_PROPOSAL: _accept_proposal,
    ActionKind.MERGE_PROPOSAL: _merge_proposal,
    ActionKind.RETURN_PROPOSAL: _dispose_proposal,
    ActionKind.REJECT_PROPOSAL: _dispose_proposal,
    ActionKind.TRANSFER_COORDINATOR: _transfer_coordinator,
}


def _attempt_authority_records(
    context: PlanContext,
    attempt: str,
    item: str,
) -> tuple[
    AttemptAuthority,
    tuple[ResourceReservationCounter, ...],
    tuple[ResourceReservation, ...],
    tuple[ResourceUseLease, ...],
]:
    attempt_lease = read_attempt_lease(context.work_root, attempt)
    reservations: list[ResourceReservation] = []
    counters: list[ResourceReservationCounter] = []
    use_leases: list[ResourceUseLease] = []
    resources: list[ResourceAuthority] = []
    directory = context.work_root / "leases" / "resources"
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            header = parse_header(path)
            if header.get("kind") != "resource-claim" or header.get("attempt") != attempt:
                continue
            resource_id = header.get("resource")
            host_id = header.get("owner_host_id")
            if not isinstance(resource_id, str) or not isinstance(host_id, str):
                raise TransitionPlanError("RESOURCE_CLAIM_INVALID", f"'{path}' has incomplete identity fields.")
            claim = read_resource_claim(context.work_root, resource_id, host_id)
            reservation_id = path.stem
            match claim.status:
                case ResourceClaimStatus.ACTIVE:
                    reservation_state = ReservationState.ACTIVE
                    use_lease_state = UseLeaseState.ACTIVE
                case ResourceClaimStatus.RESERVED:
                    reservation_state = ReservationState.ACTIVE
                    use_lease_state = UseLeaseState.REVOKED
                case ResourceClaimStatus.RELEASED:
                    reservation_state = ReservationState.RELEASED
                    use_lease_state = UseLeaseState.RELEASED
                case ResourceClaimStatus.REVOKED:
                    reservation_state = ReservationState.REVOKED
                    use_lease_state = UseLeaseState.REVOKED
                case _ as unreachable:
                    assert_never(unreachable)
            reservations.append(
                ResourceReservation(
                    ReservationId(reservation_id),
                    ResourceId(claim.resource_id),
                    ResourceInstanceId(reservation_id),
                    AttemptId(claim.attempt_id),
                    claim.generation,
                    reservation_state,
                )
            )
            counters.append(ResourceReservationCounter(ResourceInstanceId(reservation_id), claim.generation))
            use_leases.append(
                ResourceUseLease(
                    LeaseId(claim.lease_id),
                    ReservationId(reservation_id),
                    LeaseId(claim.attempt_lease_id),
                    claim.attempt_lease_generation,
                    claim.generation,
                    use_lease_state,
                )
            )
            if claim.status == ResourceClaimStatus.ACTIVE:
                resources.append(
                    ResourceAuthority(
                        ResourceId(claim.resource_id),
                        HostId(claim.host_id),
                        LeaseId(claim.lease_id),
                        claim.generation,
                    )
                )
    return (
        AttemptAuthority(
            AttemptId(attempt),
            ItemId(item),
            LeaseId(attempt_lease.lease_id),
            attempt_lease.generation,
            tuple(resources),
        ),
        tuple(counters),
        tuple(reservations),
        tuple(use_leases),
    )


def _decision_authority_records(
    context: PlanContext,
    action: Action,
) -> tuple[
    tuple[AttemptAuthority, ...],
    tuple[ResourceReservationCounter, ...],
    tuple[ResourceReservation, ...],
    tuple[ResourceUseLease, ...],
]:
    if action.kind in {ActionKind.ACCEPT_CHECKPOINT, ActionKind.RETURN_FOR_CORRECTION}:
        item = context.items[_attempt_index(context.items, action.subject)]
        authority, counters, reservations, use_leases = _attempt_authority_records(
            context,
            action.subject,
            item.item,
        )
        return (authority,), counters, reservations, use_leases
    if action.kind == ActionKind.COMPLETE and context.queue.header.get("schema") == SCHEMA_V2:
        item = context.items[_attempt_index(context.items, action.subject)]
        authority, counters, reservations, use_leases = _attempt_authority_records(
            context,
            action.subject,
            item.item,
        )
        return (authority,), counters, reservations, use_leases
    if action.kind == ActionKind.CLOSE and context.queue.header.get("schema") == SCHEMA_V2:
        item = context.items[_item_index(context.items, action.subject)]
        if item.attempt is not None:
            authority, counters, reservations, use_leases = _attempt_authority_records(
                context,
                item.attempt,
                item.item,
            )
            return (authority,), counters, reservations, use_leases
    return (), (), (), ()


def _decide_legacy_transition(
    snapshot: LedgerSnapshot,
    action: Action,
    value: TransitionInput,
    now: datetime,
) -> Decision:
    """Keep the temporary Markdown command shape separate from the richer SQLite contract."""

    if action.kind == ActionKind.ACTIVATE:
        item = snapshot.items_by_id().get(ItemId(action.subject))
        if item is None:
            raise DecisionError(DecisionErrorCode.ITEM_NOT_FOUND, f"Item '{action.subject}' does not exist.")
        if item.state != WorkState.READY:
            raise DecisionError(
                DecisionErrorCode.ACTION_NOT_AVAILABLE,
                f"Item '{item.item}' is not ready for activation.",
            )
        if not isinstance(value, LegacyActivateInput):
            raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Activate requires activation input.")
        return Decision(
            action,
            ItemChange(item.item, item.state, WorkState.ACTIVE, value.attempt),
            AttemptChange(value.attempt, None, AttemptState.ACTIVE),
            TransitionReceipt(action.action_id, item.item, action.kind.value, None, now),
        )
    if action.kind == ActionKind.SUBMIT_REVIEW:
        if not isinstance(value, EmptyInput):
            raise DecisionError(
                DecisionErrorCode.TRANSITION_INPUT_INVALID,
                "The temporary Markdown submit-review route accepts no transition data.",
            )
        item = next((candidate for candidate in snapshot.items if candidate.attempt == action.subject), None)
        if item is None:
            raise DecisionError(
                DecisionErrorCode.ATTEMPT_NOT_FOUND,
                f"Attempt '{action.subject}' does not name a live item.",
            )
        if item.state != WorkState.ACTIVE:
            raise DecisionError(
                DecisionErrorCode.ACTION_NOT_AVAILABLE,
                "Only an active attempt can be submitted for review.",
            )
        attempt_id = AttemptId(action.subject)
        return Decision(
            action,
            ItemChange(item.item, item.state, WorkState.REVIEW, item.attempt),
            AttemptChange(attempt_id, AttemptState.ACTIVE, AttemptState.REVIEW),
            TransitionReceipt(action.action_id, item.item, action.kind.value, None, now),
        )
    return decide(snapshot, action, value, now)


def plan_transition(work_root: Path, project_root: Path, action: Action, value: TransitionInput) -> ChangeSet:
    context = PlanContext(
        work_root,
        project_root,
        parse_queue(work_root / "queue.md"),
        parse_current(work_root / "current.md"),
    )
    handler = HANDLERS.get(action.kind)
    if handler is None:
        raise TransitionPlanError("ACTION_NOT_MUTATING", f"Action '{action.kind.value}' is not a canonical transition.")
    attempts = tuple(
        AttemptRecord(AttemptId(attempt.attempt), ItemId(attempt.item), attempt.state)
        for item in context.queue.items
        if item.attempt is not None
        for attempt in (parse_attempt(work_root / "attempts" / item.attempt / "attempt.md"),)
    )
    inbox = work_root / "inbox"
    proposals = (
        tuple(
            ProposalRecord(ProposalId(path.stem), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(inbox.glob("*.json"))
        )
        if inbox.is_dir()
        else ()
    )
    (
        attempt_authorities,
        resource_reservation_counters,
        resource_reservations,
        resource_use_leases,
    ) = _decision_authority_records(context, action)
    history = work_root / "history" / "items"
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
        for item in context.queue.items
    )
    snapshot = LedgerSnapshot(
        revision=context.queue.revision,
        generation=action.coordinator_generation,
        items=snapshot_items,
        attempts=attempts,
        proposals=proposals,
        attempt_authorities=attempt_authorities,
        history_items=tuple(ItemId(path.stem) for path in sorted(history.glob("*.md"))) if history.is_dir() else (),
        resource_reservation_counters=resource_reservation_counters,
        resource_reservations=resource_reservations,
        resource_use_leases=resource_use_leases,
        can_transfer_coordinator=(work_root / "coordinator.json").is_file(),
    )
    try:
        decision = _decide_legacy_transition(snapshot, action, value, datetime.now(UTC))
    except DecisionError as error:
        raise TransitionPlanError(error.code.value, str(error).partition(": ")[2]) from error
    context = replace(context, decision=decision)
    changes = handler(context, action, value)
    if context.queue.header.get("schema") != SCHEMA_V2:
        return changes
    queue_change = next((change for change in changes.changes if str(change.path) == "queue.md"), None)
    if queue_change is None or queue_change.data is None:
        return changes
    updated_queue = parse_queue_text(queue_change.data.decode(), work_root / "queue.md")
    synchronized = list(changes.changes)
    for item in updated_queue.items:
        relative = f"items/{item.item}.md"
        existing_index = next(
            (index for index, change in enumerate(synchronized) if str(change.path) == relative), None
        )
        if existing_index is not None:
            existing_data = synchronized[existing_index].data
            if existing_data is None:
                continue
            source_text = existing_data.decode()
            resources: tuple[str, ...] = ()
        else:
            source_path = work_root / relative
            source_text = source_path.read_text(encoding="utf-8")
            resources = parse_item(source_path).resources
        item_change = write_change(relative, render_v2_item(source_text, item, resources))
        if existing_index is None:
            synchronized.append(item_change)
        else:
            synchronized[existing_index] = item_change
    return ChangeSet(tuple(synchronized))
