from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import assert_never

from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    ItemId,
    LeaseId,
    LedgerId,
    ProposalId,
    SubjectId,
)
from charlie_pinboard.domain.model import (
    AcceptProposalInput,
    ActivateInput,
    AttemptAuthority,
    AttemptState,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    LedgerSnapshot,
    MergeProposalInput,
    ReasonInput,
    ReservationState,
    ResourceAuthority,
    TransferCoordinatorInput,
    TransitionInput,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.domain.resource_decisions import (
    ReservationChange,
    ResourceToken,
    ResourceUseLeaseChange,
)


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
    RETURN_FOR_CORRECTION = "return-for-correction"
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
class Action:
    action_id: ActionId
    kind: ActionKind
    subject: SubjectId
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: LeaseId | None = None
    resource_claims: tuple[ResourceToken, ...] = ()


@dataclass(frozen=True, slots=True)
class ActorAuthority:
    role: Role
    authorization: AuthorizationKind
    generation: int
    lease_id: LeaseId | None = None
    attempts: tuple[AttemptId, ...] = ()
    revision_scoped: bool = True


@dataclass(frozen=True, slots=True)
class ActionFactory:
    revision: str
    actor: ActorAuthority

    def make(
        self,
        kind: ActionKind,
        subject: SubjectId,
        label: str,
        subject_revision: str | None = None,
        resource_claims: tuple[ResourceToken, ...] = (),
    ) -> Action:
        return Action(
            action_id=ActionId(f"{kind.value}:{subject}"),
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=self.revision,
            coordinator_generation=self.actor.generation,
            subject_revision=subject_revision,
            authorization=self.actor.authorization,
            lease_id=self.actor.lease_id,
            resource_claims=resource_claims,
        )


@dataclass(frozen=True, slots=True)
class ItemChange:
    item: ItemId
    before: WorkState | None
    after: WorkState | None
    attempt: AttemptId | None = None
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptChange:
    attempt: AttemptId
    before: AttemptState | None
    after: AttemptState | None


@dataclass(frozen=True, slots=True)
class AttemptAuthorityChange:
    before: AttemptAuthority
    after: AttemptAuthority


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    action_id: ActionId
    item: ItemId | None
    outcome: str
    evidence: str | None
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    item_change: ItemChange | None
    attempt_change: AttemptChange | None
    receipt: TransitionReceipt
    attempt_authority_change: AttemptAuthorityChange | None = None
    reservation_changes: tuple[ReservationChange, ...] = ()
    resource_use_lease_changes: tuple[ResourceUseLeaseChange, ...] = ()


def _resource_token(value: ResourceAuthority) -> ResourceToken:
    return ResourceToken(value.resource_id, value.host_id, value.lease_id, value.generation)


def _authority(snapshot: LedgerSnapshot, actor: ActorAuthority, attempt: AttemptId) -> AttemptAuthority | None:
    if attempt not in actor.attempts:
        return None
    return snapshot.authority_for(attempt, actor.lease_id, actor.generation)


def _unresolved_target(snapshot: LedgerSnapshot, item: ItemId) -> bool:
    return any(
        obligation.target == item and obligation.disposition is None
        for impact in snapshot.planning_impacts
        for obligation in impact.obligations
    )


def _unresolved_source(snapshot: LedgerSnapshot, item: ItemId) -> bool:
    return any(
        impact.source_item == item and any(value.disposition is None for value in impact.obligations)
        for impact in snapshot.planning_impacts
    )


def _scope_stale(snapshot: LedgerSnapshot, item: WorkItem) -> bool:
    if item.attempt is None:
        return False
    attempt = snapshot.attempts_by_id().get(item.attempt)
    scope = next((value for value in snapshot.scopes if value.item == item.item), None)
    if attempt is None or scope is None or attempt.accepted_scope_revision is None:
        return False
    return (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (scope.revision, scope.digest)


def _item_for_attempt(snapshot: LedgerSnapshot, attempt: AttemptId) -> WorkItem | None:
    return next((item for item in snapshot.items if item.attempt == attempt), None)


def _worker_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> tuple[Action, ...]:
    result: list[Action] = []
    for attempt in factory.actor.attempts:
        item = _item_for_attempt(snapshot, attempt)
        authority = _authority(snapshot, factory.actor, attempt)
        if item is None or authority is None or item.state != WorkState.ACTIVE:
            continue
        claims = tuple(_resource_token(value) for value in authority.resources)
        revision = snapshot.subject_revision(item.item)
        result.extend(
            (
                factory.make(ActionKind.CONTINUE, attempt, f"Continue {item.item}", revision, claims),
                factory.make(ActionKind.REPORT_BLOCKER, attempt, f"Report a blocker for {item.item}", revision, claims),
            )
        )
        if not _unresolved_target(snapshot, item.item) and not _scope_stale(snapshot, item):
            result.append(
                factory.make(ActionKind.SUBMIT_REVIEW, attempt, f"Submit {item.item} for review", revision, claims)
            )
    return tuple(result)


def _active_coordinator_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    for item in snapshot.items:
        if item.state not in {WorkState.ACTIVE, WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == WorkState.ACTIVE:
            result.append(factory.make(ActionKind.CONTINUE, item.attempt, f"Continue {item.item}"))
            if not _unresolved_target(snapshot, item.item) and not _scope_stale(snapshot, item):
                result.append(
                    factory.make(ActionKind.DISPATCH, item.attempt, f"Prepare a worker launch for {item.item}")
                )
            result.extend(
                (
                    factory.make(ActionKind.PAUSE, item.attempt, f"Pause and preserve {item.item}"),
                    factory.make(ActionKind.BLOCK, item.attempt, f"Block {item.item} on a named condition"),
                )
            )
        if (
            not _unresolved_target(snapshot, item.item)
            and not _unresolved_source(snapshot, item.item)
            and not _scope_stale(snapshot, item)
        ):
            result.append(factory.make(ActionKind.COMPLETE, item.attempt, f"Accept and complete {item.item}"))
        if item.state == WorkState.REVIEW and factory.actor.authorization == AuthorizationKind.COORDINATION:
            result.append(
                factory.make(
                    ActionKind.RETURN_FOR_CORRECTION,
                    item.attempt,
                    f"Return {item.item} for correction",
                )
            )
    return result


def _item_actions(snapshot: LedgerSnapshot, item: WorkItem, factory: ActionFactory) -> list[Action]:
    close = factory.make(ActionKind.CLOSE, item.item, f"Record a terminal decision for {item.item}")
    if item.state == WorkState.INTAKE:
        return [
            factory.make(ActionKind.MARK_READY, item.item, f"Mark {item.item} ready"),
            factory.make(ActionKind.BLOCK_ITEM, item.item, f"Block {item.item} on a named condition"),
            factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    if item.state == WorkState.READY:
        actions = [factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"), close]
        if not _unresolved_target(snapshot, item.item):
            actions.insert(0, factory.make(ActionKind.ACTIVATE, item.item, f"Activate {item.item}"))
        return actions
    dependencies_live = any(dependency in snapshot.items_by_id() for dependency in item.depends_on)
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


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    revision = snapshot.revision if actor.revision_scoped else ""
    factory = ActionFactory(revision, actor)
    match actor.role:
        case Role.OBSERVER:
            return (factory.make(ActionKind.INSPECT, LedgerId("ledger"), "Inspect current work"),)
        case Role.WORKER:
            result = _worker_actions(snapshot, factory)
            if not result:
                raise DecisionError(
                    DecisionErrorCode.ATTEMPT_LEASE_REQUIRED,
                    "The supplied attempt lease is not current for an active item.",
                )
            return result
        case Role.COORDINATOR:
            result = _active_coordinator_actions(snapshot, factory)
            for item in snapshot.items:
                result.extend(_item_actions(snapshot, item, factory))
            for proposal in snapshot.proposals:
                for kind, verb in (
                    (ActionKind.ACCEPT_PROPOSAL, "Accept"),
                    (ActionKind.MERGE_PROPOSAL, "Merge"),
                    (ActionKind.RETURN_PROPOSAL, "Return"),
                    (ActionKind.REJECT_PROPOSAL, "Reject"),
                ):
                    result.append(
                        factory.make(kind, proposal.proposal, f"{verb} proposal {proposal.proposal}", proposal.revision)
                    )
            if snapshot.can_transfer_coordinator:
                result.append(
                    factory.make(ActionKind.TRANSFER_COORDINATOR, LedgerId("ledger"), "Transfer coordinator ownership")
                )
            return tuple(result)
        case _ as unreachable:
            assert_never(unreachable)


def _item(snapshot: LedgerSnapshot, item_id: ItemId) -> WorkItem:
    item = snapshot.items_by_id().get(item_id)
    if item is None:
        raise DecisionError(DecisionErrorCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    return item


def _attempt_item(snapshot: LedgerSnapshot, attempt: AttemptId) -> WorkItem:
    item = _item_for_attempt(snapshot, attempt)
    if item is None:
        raise DecisionError(DecisionErrorCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt}' does not exist.")
    return item


def _receipt(
    action: Action,
    item: ItemId | None,
    outcome: str,
    evidence: str | None,
    now: datetime,
) -> TransitionReceipt:
    return TransitionReceipt(action.action_id, item, outcome, evidence, now)


type DecisionHandler = Callable[[LedgerSnapshot, Action, TransitionInput, datetime], Decision]


def _result(
    action: Action,
    now: datetime,
    *,
    item: ItemId | None = None,
    item_change: ItemChange | None = None,
    attempt_change: AttemptChange | None = None,
    attempt_authority_change: AttemptAuthorityChange | None = None,
    reservation_changes: tuple[ReservationChange, ...] = (),
    resource_use_lease_changes: tuple[ResourceUseLeaseChange, ...] = (),
    outcome: str | None = None,
    evidence: str | None = None,
) -> Decision:
    return Decision(
        action,
        item_change,
        attempt_change,
        _receipt(action, item, outcome or action.kind.value, evidence, now),
        attempt_authority_change,
        reservation_changes,
        resource_use_lease_changes,
    )


def _activate(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, ItemId(action.subject))
    if item.state != WorkState.READY or _unresolved_target(snapshot, item.item):
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not ready for activation.")
    if not isinstance(value, ActivateInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Activate requires activation input.")
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.ACTIVE, value.attempt),
        attempt_change=AttemptChange(value.attempt, None, AttemptState.ACTIVE),
    )


def _pause_or_block(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    attempt_id = AttemptId(action.subject)
    item = _attempt_item(snapshot, attempt_id)
    if item.state != WorkState.ACTIVE:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "The named attempt is not active.")
    if action.kind == ActionKind.PAUSE and not isinstance(value, ReasonInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Pause requires a reason.")
    if action.kind == ActionKind.BLOCK and not isinstance(value, BlockInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Block requires a reason and dependencies.")
    target = WorkState.PAUSED if action.kind == ActionKind.PAUSE else WorkState.BLOCKED
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=AttemptChange(attempt_id, AttemptState.ACTIVE, AttemptState(target.value)),
    )


def _complete(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    attempt_id = AttemptId(action.subject)
    item = _attempt_item(snapshot, attempt_id)
    if item.state not in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "The named attempt is not active or in review.")
    if _unresolved_target(snapshot, item.item) or _unresolved_source(snapshot, item.item):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_UNRESOLVED, "Resolve planning impacts before completion.")
    if _scope_stale(snapshot, item):
        raise DecisionError(
            DecisionErrorCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope."
        )
    if not isinstance(value, EvidenceInput) or not value.evidence.strip():
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Completion requires outcome evidence.")
    if item.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    before = AttemptState.REVIEW if item.state == WorkState.REVIEW else AttemptState.ACTIVE
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.evidence),
        attempt_change=AttemptChange(attempt_id, before, AttemptState.DONE),
        evidence=value.evidence,
    )


def _close(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, ItemId(action.subject))
    if item.state in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_AVAILABLE, "Active or review work requires the acceptance path."
        )
    if not isinstance(value, CloseInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Close requires terminal outcome input.")
    if value.outcome == CloseOutcome.DROPPED and any(item.item in candidate.depends_on for candidate in snapshot.items):
        raise DecisionError(DecisionErrorCode.LIVE_DEPENDENTS, f"Item '{item.item}' still has live dependents.")
    if item.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    attempt_change = None
    if item.attempt is not None:
        attempt = snapshot.attempts_by_id().get(item.attempt)
        attempt_change = AttemptChange(item.attempt, None if attempt is None else attempt.state, AttemptState.DONE)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.reason),
        attempt_change=attempt_change,
        outcome=value.outcome.value,
        evidence=value.reason,
    )


def _resume(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, ItemId(action.subject))
    if item.state not in {WorkState.PAUSED, WorkState.BLOCKED}:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not paused or blocked.")
    if any(dependency in snapshot.items_by_id() for dependency in item.depends_on):
        raise DecisionError(
            DecisionErrorCode.DEPENDENCY_NOT_SATISFIED, f"Item '{item.item}' still has a live dependency."
        )
    if not isinstance(value, EmptyInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Resume does not accept transition data.")
    target = WorkState.ACTIVE if item.attempt is not None else WorkState.READY
    attempt_change = None
    if item.attempt is not None:
        before = AttemptState.PAUSED if item.state == WorkState.PAUSED else AttemptState.BLOCKED
        attempt_change = AttemptChange(item.attempt, before, AttemptState.ACTIVE)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=attempt_change,
    )


def _submit_review(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    attempt_id = AttemptId(action.subject)
    item = _attempt_item(snapshot, attempt_id)
    if item.state != WorkState.ACTIVE:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_AVAILABLE, "Only an active attempt can be submitted for review."
        )
    if _unresolved_target(snapshot, item.item):
        raise DecisionError(
            DecisionErrorCode.PLANNING_IMPACT_UNRESOLVED, "Resolve target planning impacts before review."
        )
    if _scope_stale(snapshot, item):
        raise DecisionError(
            DecisionErrorCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope."
        )
    if not isinstance(value, EmptyInput):
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID, "Submit review does not accept transition data."
        )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.REVIEW, item.attempt),
        attempt_change=AttemptChange(attempt_id, AttemptState.ACTIVE, AttemptState.REVIEW),
    )


def _return_for_correction(
    snapshot: LedgerSnapshot,
    action: Action,
    value: TransitionInput,
    now: datetime,
) -> Decision:
    attempt_id = AttemptId(action.subject)
    item = _attempt_item(snapshot, attempt_id)
    if item.state != WorkState.REVIEW:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_AVAILABLE, "Only an attempt in review can be returned for correction."
        )
    if not isinstance(value, ReasonInput) or not value.reason.strip():
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID, "Returning a review requires a correction reason."
        )
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == attempt_id)
    if len(authorities) != 1:
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Returning a review requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1, resources=()),
    )
    reservations = tuple(candidate for candidate in snapshot.resource_reservations if candidate.attempt == attempt_id)
    reservation_changes = tuple(
        ReservationChange(
            reservation,
            replace(reservation, generation=reservation.generation + 1, state=ReservationState.REVOKED),
        )
        for reservation in reservations
    )
    reservation_ids = {reservation.reservation_id for reservation in reservations}
    use_lease_changes = tuple(
        ResourceUseLeaseChange(
            use_lease,
            replace(use_lease, generation=use_lease.generation + 1, state=UseLeaseState.REVOKED),
        )
        for use_lease in snapshot.resource_use_leases
        if use_lease.reservation_id in reservation_ids
    )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, WorkState.REVIEW, WorkState.ACTIVE, item.attempt),
        attempt_change=AttemptChange(attempt_id, AttemptState.REVIEW, AttemptState.ACTIVE),
        attempt_authority_change=authority_change,
        reservation_changes=reservation_changes,
        resource_use_lease_changes=use_lease_changes,
        evidence=value.reason,
    )


def _simple_item_transition(
    snapshot: LedgerSnapshot,
    action: Action,
    value: TransitionInput,
    now: datetime,
) -> Decision:
    item = _item(snapshot, ItemId(action.subject))
    if action.kind == ActionKind.REOPEN:
        expected, target, valid = WorkState.DEFERRED, WorkState.INTAKE, isinstance(value, EvidenceInput)
    elif action.kind == ActionKind.MARK_READY:
        expected, target, valid = WorkState.INTAKE, WorkState.READY, isinstance(value, ReasonInput)
    else:
        expected, target, valid = item.state, WorkState.BLOCKED, isinstance(value, BlockInput)
        if item.state not in {WorkState.INTAKE, WorkState.READY}:
            raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be blocked now.")
    if item.state != expected:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot perform '{action.kind.value}' now."
        )
    if not valid:
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, f"Input for '{action.kind.value}' is invalid.")
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, target))


def _defer(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, ItemId(action.subject))
    if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED} or item.attempt is not None:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be deferred now.")
    if not isinstance(value, DeferInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Defer requires a reopen condition.")
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, WorkState.DEFERRED))


def _accept_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, ProposalId(action.subject))
    if not isinstance(value, AcceptProposalInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Accept proposal requires item input.")
    if value.item in snapshot.items_by_id() or value.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.ITEM_ALREADY_EXISTS, f"Item '{value.item}' already exists.")
    change = ItemChange(value.item, None, WorkState(value.state.value))
    return _result(action, now, item=value.item, item_change=change)


def _require_proposal(snapshot: LedgerSnapshot, proposal: ProposalId) -> None:
    if proposal not in snapshot.proposal_revisions():
        raise DecisionError(DecisionErrorCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal}' does not exist.")


def _merge_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, ProposalId(action.subject))
    if not isinstance(value, MergeProposalInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Merge proposal requires a target item.")
    return _result(action, now)


def _dispose_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, ProposalId(action.subject))
    if not isinstance(value, ReasonInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Proposal disposition requires a reason.")
    return _result(action, now, evidence=value.reason)


def _transfer(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    if not snapshot.can_transfer_coordinator:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_AVAILABLE, "This ledger does not use transferable coordinator ownership."
        )
    if not isinstance(value, TransferCoordinatorInput):
        raise DecisionError(
            DecisionErrorCode.TRANSITION_INPUT_INVALID, "Coordinator transfer requires a task and host."
        )
    return _result(action, now)


DECISION_HANDLERS: dict[ActionKind, DecisionHandler] = {
    ActionKind.ACTIVATE: _activate,
    ActionKind.PAUSE: _pause_or_block,
    ActionKind.BLOCK: _pause_or_block,
    ActionKind.COMPLETE: _complete,
    ActionKind.CLOSE: _close,
    ActionKind.RESUME: _resume,
    ActionKind.SUBMIT_REVIEW: _submit_review,
    ActionKind.RETURN_FOR_CORRECTION: _return_for_correction,
    ActionKind.REOPEN: _simple_item_transition,
    ActionKind.MARK_READY: _simple_item_transition,
    ActionKind.BLOCK_ITEM: _simple_item_transition,
    ActionKind.DEFER: _defer,
    ActionKind.ACCEPT_PROPOSAL: _accept_proposal,
    ActionKind.MERGE_PROPOSAL: _merge_proposal,
    ActionKind.RETURN_PROPOSAL: _dispose_proposal,
    ActionKind.REJECT_PROPOSAL: _dispose_proposal,
    ActionKind.TRANSFER_COORDINATOR: _transfer,
}


def decide(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    handler = DECISION_HANDLERS.get(action.kind)
    if handler is None:
        raise DecisionError(
            DecisionErrorCode.ACTION_NOT_MUTATING, f"Action '{action.kind.value}' is not a canonical transition."
        )
    return handler(snapshot, action, value, now)
