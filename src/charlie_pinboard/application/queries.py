from datetime import UTC, datetime
from typing import assert_never

from charlie_pinboard.application.errors import QueryError, QueryErrorCode
from charlie_pinboard.application.ports import WorkStore
from charlie_pinboard.application.query_models import (
    DependencyReason,
    ItemStatus,
    ItemStatusAttempt,
    ItemStatusState,
    OverviewItem,
    ParallelItem,
    ParallelOutcome,
    ParallelPreview,
    ParallelReason,
    ParallelReasonCode,
    ParallelSelection,
    ReviewFlag,
    WorkOverview,
)
from charlie_pinboard.application.stored_state import (
    ItemDependency,
    StoredAttempt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.authority_models import AttemptLeaseStatus
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.work_models import (
    AttemptState,
    ProposalDispositionKind,
    ProposalRelationKind,
    WorkState,
)


def _dependency_key(value: ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: ItemDependency) -> int:
    return value.position


def _item_key(value: StoredWorkItem) -> tuple[int, str]:
    return value.queue_position or 0, str(value.item_id)


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _parallel_item_key(value: ParallelItem) -> str:
    return value.item_id


_TERMINAL_ITEM_STATES = {
    StoredWorkItemState.DONE,
    StoredWorkItemState.SUPERSEDED,
    StoredWorkItemState.DROPPED,
}


def _work_state(value: StoredWorkItemState) -> WorkState:
    try:
        return WorkState(value.value)
    except ValueError as error:
        raise QueryError(QueryErrorCode.WORK_STATE_INVALID, f"Item state {value.value!r} is not live.") from error


def _item_status_state(value: StoredWorkItemState) -> ItemStatusState:
    match value:
        case (
            StoredWorkItemState.INTAKE
            | StoredWorkItemState.READY
            | StoredWorkItemState.ACTIVE
            | StoredWorkItemState.PAUSED
            | StoredWorkItemState.BLOCKED
            | StoredWorkItemState.DEFERRED
            | StoredWorkItemState.REVIEW
            | StoredWorkItemState.DONE
            | StoredWorkItemState.SUPERSEDED
            | StoredWorkItemState.DROPPED
        ):
            return ItemStatusState(value.value)
        case _ as unreachable:
            assert_never(unreachable)


def overview_from_state(state: StoredWorkState) -> WorkOverview:
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
    }
    dependency_links = {
        item.item_id: tuple(
            link
            for link in sorted(
                (candidate for candidate in state.lifecycle.dependencies if candidate.item_id == item.item_id),
                key=_dependency_key,
            )
        )
        for item in state.lifecycle.work_items
    }
    proposals = {ItemId(proposal.proposal_id): proposal for proposal in state.proposals.proposals}
    live_ids = frozenset(item.item_id for item in state.lifecycle.work_items if item.state not in _TERMINAL_ITEM_STATES)

    def dependency_reason(item_id: ItemId, link: ItemDependency) -> DependencyReason:
        proposal = proposals.get(item_id)
        if (
            proposal is not None
            and proposal.relation == ProposalRelationKind.FOLLOW_UP
            and proposal.relation_item_id == link.dependency_id
        ):
            reason = f"Follow-up to {link.dependency_id}: {proposal.why_it_matters}"
        else:
            prerequisite = next(
                (
                    value
                    for value in proposals.values()
                    if value.relation == ProposalRelationKind.PREREQUISITE
                    and value.relation_item_id == item_id
                    and ItemId(value.proposal_id) == link.dependency_id
                ),
                None,
            )
            reason = (
                f"Inferred prerequisite {link.dependency_id}: {prerequisite.why_it_matters}"
                if prerequisite is not None
                else "Recorded dependency."
            )
        return DependencyReason(str(link.dependency_id), reason)

    def review_flags(item_id: ItemId) -> tuple[ReviewFlag, ...]:
        proposal = proposals.get(item_id)
        if proposal is None or proposal.disposition not in {None, ProposalDispositionKind.RETURNED}:
            return ()
        if proposal.disposition == ProposalDispositionKind.RETURNED:
            return (
                ReviewFlag(
                    ProposalRelationKind.CLARIFICATION,
                    str(proposal.relation_item_id) if proposal.relation_item_id is not None else None,
                    proposal.disposition_reason or proposal.why_it_matters,
                ),
            )
        if proposal.relation not in {
            ProposalRelationKind.DUPLICATE,
            ProposalRelationKind.CONTRADICTION,
            ProposalRelationKind.CLARIFICATION,
        }:
            return ()
        return (
            ReviewFlag(
                proposal.relation,
                str(proposal.relation_item_id) if proposal.relation_item_id is not None else None,
                proposal.why_it_matters,
            ),
        )

    items = tuple(
        OverviewItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            item.queue_position or 0,
            not any(link.dependency_id in live_ids for link in dependency_links[item.item_id]),
            item.timing.value if item.timing is not None else None,
            tuple(str(link.dependency_id) for link in dependency_links[item.item_id]),
            tuple(dependency_reason(item.item_id, link) for link in dependency_links[item.item_id]),
            review_flags(item.item_id),
            str(attempts[item.item_id]) if item.item_id in attempts else None,
            item.next_action,
            item.notes or "",
        )
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if item.state not in _TERMINAL_ITEM_STATES
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (
            item.state in {WorkState.INTAKE, WorkState.READY, WorkState.DEFERRED}
            or item.state in {WorkState.PAUSED, WorkState.BLOCKED}
        )
    )
    return WorkOverview(
        "pinboard-overview/v2",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(state.focus.item_id) if state.focus.item_id is not None else None,
        str(state.focus.attempt_id) if state.focus.attempt_id is not None else None,
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == AttemptState.ACTIVE
        ),
        items,
        immediate,
    )


def item_status(store: WorkStore, item_id: ItemId) -> ItemStatus:
    state = store.snapshot()
    item = next((candidate for candidate in state.lifecycle.work_items if candidate.item_id == item_id), None)
    if item is None:
        raise QueryError(QueryErrorCode.ITEM_NOT_FOUND, f"Item '{item_id}' was not found.")
    attempts = tuple(
        ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in sorted(
            (candidate for candidate in state.lifecycle.attempts if candidate.item_id == item_id),
            key=_attempt_key,
        )
    )
    return ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(item.item_id),
        item.user_label,
        _item_status_state(item.state),
        item.timing,
        item.outcome_evidence,
        item.next_action,
        item.notes or "",
        attempts,
    )


def _preview_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise QueryError(QueryErrorCode.PARALLEL_TIME_INVALID, "Preview time must be timezone-aware.")
    return current.astimezone(UTC)


def _parallel_reasons(
    state: StoredWorkState,
    item_id: ItemId,
    live_items: frozenset[str],
    current: datetime,
) -> tuple[ParallelReason, ...]:
    item = next(value for value in state.lifecycle.work_items if value.item_id == item_id)
    if item.state not in {StoredWorkItemState.READY, StoredWorkItemState.ACTIVE}:
        return (
            ParallelReason(
                ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    live_dependencies = tuple(
        str(link.dependency_id)
        for link in sorted(state.lifecycle.dependencies, key=_dependency_position)
        if link.item_id == item_id and str(link.dependency_id) in live_items
    )
    if live_dependencies:
        return (
            ParallelReason(
                ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    attempt = next(
        (
            candidate
            for candidate in state.lifecycle.attempts
            if candidate.item_id == item_id and candidate.state == AttemptState.ACTIVE
        ),
        None,
    )
    if attempt is not None:
        lease = next(
            (candidate for candidate in state.authority.attempt_leases if candidate.attempt_id == attempt.attempt_id),
            None,
        )
        if lease is not None and lease.state == AttemptLeaseStatus.ACTIVE and current < lease.expires_at:
            return (
                ParallelReason(
                    ParallelReasonCode.ATTEMPT_OWNED,
                    f"Active attempt '{attempt.attempt_id}' is owned until {lease.expires_at.isoformat()}.",
                ),
            )
    return ()


def preview_parallel(
    store: WorkStore,
    *,
    selected: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ParallelPreview:
    state = store.snapshot()
    current = _preview_time(now)
    live = tuple(item for item in state.lifecycle.work_items if item.state not in _TERMINAL_ITEM_STATES)
    by_id = {str(item.item_id): item for item in live}
    if len(selected) != len(set(selected)) or any(item_id not in by_id for item_id in selected):
        raise QueryError(
            QueryErrorCode.PARALLEL_SELECTION_INVALID,
            "Selected item identities must be unique current items.",
        )
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else tuple(sorted(live, key=_item_key))
    live_ids = frozenset(by_id)
    launchable: list[ParallelItem] = []
    excluded: list[ParallelItem] = []
    for item in candidates:
        reasons = _parallel_reasons(state, item.item_id, live_ids, current)
        value = ParallelItem(
            str(item.item_id),
            item.user_label,
            _work_state(item.state),
            str(
                next(
                    (
                        attempt.attempt_id
                        for attempt in state.lifecycle.attempts
                        if attempt.item_id == item.item_id
                        and attempt.state not in {AttemptState.DONE, AttemptState.CLOSED}
                    ),
                    "",
                )
            )
            or None,
            ParallelOutcome.EXCLUDED if reasons else ParallelOutcome.LAUNCHABLE,
            reasons,
        )
        (excluded if reasons else launchable).append(value)
    return ParallelPreview(
        "pinboard-parallel-preview/v1",
        str(state.lifecycle.project.revision),
        ParallelSelection.SELECTED if selected else ParallelSelection.ALL_SAFE,
        not selected or not excluded,
        tuple(sorted(launchable, key=_parallel_item_key)),
        tuple(sorted(excluded, key=_parallel_item_key)),
    )
