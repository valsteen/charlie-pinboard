from datetime import UTC, datetime

from pinboard.application import query_models, stored_state
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.authority_models import AttemptLeaseStatus
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ItemId


def _dependency_key(value: stored_state.ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: stored_state.ItemDependency) -> int:
    return value.position


def _item_key(value: stored_state.StoredWorkItem) -> tuple[int, str]:
    return value.queue_position or 0, str(value.item_id)


def _attempt_key(value: stored_state.StoredAttempt) -> str:
    return str(value.attempt_id)


def _parallel_item_key(value: query_models.ParallelItem) -> str:
    return value.item_id


def overview_from_state(state: stored_state.StoredWorkState) -> query_models.WorkOverview:
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
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
    live_items = tuple(
        (item, live_state)
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if (live_state := stored_state.live_work_state(item.state)) is not None
    )
    live_ids = frozenset(item.item_id for item, _live_state in live_items)

    def dependency_reason(item_id: ItemId, link: stored_state.ItemDependency) -> query_models.DependencyReason:
        proposal = proposals.get(item_id)
        if (
            proposal is not None
            and isinstance(proposal.relation, work_models.FollowUpProposalRelation)
            and proposal.relation.item == link.dependency_id
        ):
            reason = f"Follow-up to {link.dependency_id}: {proposal.why_it_matters}"
        else:
            prerequisite = next(
                (
                    value
                    for value in proposals.values()
                    if isinstance(value.relation, work_models.PrerequisiteProposalRelation)
                    and value.relation.item == item_id
                    and ItemId(value.proposal_id) == link.dependency_id
                ),
                None,
            )
            reason = (
                f"Inferred prerequisite {link.dependency_id}: {prerequisite.why_it_matters}"
                if prerequisite is not None
                else "Recorded dependency."
            )
        return query_models.DependencyReason(str(link.dependency_id), reason)

    def review_flags(item_id: ItemId) -> tuple[query_models.ReviewFlag, ...]:
        proposal = proposals.get(item_id)
        if proposal is None:
            return ()
        if isinstance(proposal.disposition, work_models.ReturnedProposalDisposition):
            return (
                query_models.ReviewFlag(
                    work_models.ProposalRelationKind.CLARIFICATION,
                    str(proposal.relation.item) if proposal.relation.item is not None else None,
                    proposal.disposition.reason,
                ),
            )
        if proposal.disposition is not None:
            return ()
        if proposal.relation.kind not in {
            work_models.ProposalRelationKind.DUPLICATE,
            work_models.ProposalRelationKind.CONTRADICTION,
            work_models.ProposalRelationKind.CLARIFICATION,
        }:
            return ()
        return (
            query_models.ReviewFlag(
                proposal.relation.kind,
                str(proposal.relation.item) if proposal.relation.item is not None else None,
                proposal.why_it_matters,
            ),
        )

    items = tuple(
        query_models.OverviewItem(
            str(item.item_id),
            item.user_label,
            live_state,
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
        for item, live_state in live_items
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (
            item.state in {work_models.WorkState.INTAKE, work_models.WorkState.READY, work_models.WorkState.DEFERRED}
            or item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}
        )
    )
    return query_models.WorkOverview(
        "pinboard-overview/v2",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(state.focus.item_id) if state.focus.item_id is not None else None,
        str(state.focus.attempt_id) if state.focus.attempt_id is not None else None,
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
    )


def item_status(store: WorkStore, item_id: ItemId) -> DecisionResult[query_models.ItemStatus]:
    state = store.snapshot()
    item = next((candidate for candidate in state.lifecycle.work_items if candidate.item_id == item_id), None)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' was not found.")
    attempts = tuple(
        query_models.ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in sorted(
            (candidate for candidate in state.lifecycle.attempts if candidate.item_id == item_id),
            key=_attempt_key,
        )
    )
    return query_models.ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v1",
        str(state.lifecycle.project.revision),
        str(item.item_id),
        item.user_label,
        item.state,
        item.timing,
        item.outcome_evidence,
        item.next_action,
        item.notes or "",
        attempts,
    )


def _preview_time(value: datetime | None) -> query_models.QueryResult[datetime]:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return query_models.QueryFailure(
            query_models.QueryRejectionCode.PARALLEL_TIME_INVALID,
            "Preview time must be timezone-aware.",
        )
    return current.astimezone(UTC)


def _parallel_reasons(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    live_items: frozenset[str],
    current: datetime,
) -> tuple[query_models.ParallelReason, ...]:
    item = next(value for value in state.lifecycle.work_items if value.item_id == item_id)
    if item.state not in {stored_state.StoredWorkItemState.READY, stored_state.StoredWorkItemState.ACTIVE}:
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.STATE_NOT_LAUNCHABLE,
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
            query_models.ParallelReason(
                query_models.ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    attempt = next(
        (
            candidate
            for candidate in state.lifecycle.attempts
            if candidate.item_id == item_id and candidate.state == work_models.AttemptState.ACTIVE
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
                query_models.ParallelReason(
                    query_models.ParallelReasonCode.ATTEMPT_OWNED,
                    f"Active attempt '{attempt.attempt_id}' is owned until {lease.expires_at.isoformat()}.",
                ),
            )
    return ()


def preview_parallel(
    store: WorkStore,
    *,
    selected: tuple[str, ...] = (),
    now: datetime | None = None,
) -> query_models.QueryResult[query_models.ParallelPreview]:
    state = store.snapshot()
    current = _preview_time(now)
    if isinstance(current, query_models.QueryFailure):
        return current
    live = tuple(
        (item, live_state)
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if (live_state := stored_state.live_work_state(item.state)) is not None
    )
    by_id = {str(item.item_id): (item, live_state) for item, live_state in live}
    if len(selected) != len(set(selected)) or any(item_id not in by_id for item_id in selected):
        return query_models.QueryFailure(
            query_models.QueryRejectionCode.PARALLEL_SELECTION_INVALID,
            "Selected item identities must be unique current items.",
        )
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else live
    live_ids = frozenset(by_id)
    items: list[query_models.ParallelItem] = []
    for item, live_state in candidates:
        reasons = _parallel_reasons(state, item.item_id, live_ids, current)
        common = (
            str(item.item_id),
            item.user_label,
            live_state,
            str(
                next(
                    (
                        attempt.attempt_id
                        for attempt in state.lifecycle.attempts
                        if attempt.item_id == item.item_id and attempt.state != work_models.AttemptState.DONE
                    ),
                    "",
                )
            )
            or None,
        )
        items.append(
            query_models.ExcludedParallelItem(*common, reasons)
            if reasons
            else query_models.LaunchableParallelItem(*common)
        )
    return query_models.ParallelPreview(
        "pinboard-parallel-preview/v1",
        str(state.lifecycle.project.revision),
        query_models.ParallelSelection.SELECTED if selected else query_models.ParallelSelection.ALL_SAFE,
        not selected or not any(isinstance(item, query_models.ExcludedParallelItem) for item in items),
        tuple(sorted(items, key=_parallel_item_key)),
    )
