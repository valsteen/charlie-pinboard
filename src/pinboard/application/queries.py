from datetime import datetime
from typing import assert_never

from pinboard.application import query_models, stored_state
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.authority_models import AttemptLeaseStatus, PreparationLeaseStatus
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


def _preparation_status(
    state: stored_state.StoredWorkState, item_id: ItemId, now: datetime
) -> query_models.PreparationStatusView | None:
    lease = next((value for value in state.authority.preparation_leases if value.item_id == item_id), None)
    if lease is None:
        return None
    anchor = next(
        (
            value
            for value in state.authority.preparation_generations
            if value.item_id == item_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        return None
    match lease.state:
        case PreparationLeaseStatus.ACTIVE:
            status: query_models.PreparationStatus = "expired" if lease.expires_at <= now else "active"
        case PreparationLeaseStatus.EXPIRED:
            status = "expired"
        case PreparationLeaseStatus.RELEASED:
            status = "released"
        case PreparationLeaseStatus.REVOKED:
            status = "revoked"
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.PreparationStatusView(
        lease.definition_revision,
        lease.definition_digest,
        str(anchor.task_id),
        str(anchor.host_id),
        str(anchor.lease_id),
        lease.generation,
        lease.expires_at.isoformat(),
        status,
    )


def overview_from_state(state: stored_state.StoredWorkState, now: datetime) -> query_models.WorkOverview:
    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
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
            definitions[item.item_id].title,
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
            _preparation_status(state, item.item_id, now),
        )
        for item, live_state in live_items
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (item.preparation is None or item.preparation.status != "active")
        and (
            item.state in {work_models.WorkState.INTAKE, work_models.WorkState.READY, work_models.WorkState.DEFERRED}
            or item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}
        )
    )
    return query_models.WorkOverview(
        "pinboard-overview/v2",
        "sqlite-v3",
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


def item_status(store: WorkStore, item_id: ItemId, now: datetime) -> DecisionResult[query_models.ItemStatus]:
    state = store.snapshot()
    item = next((candidate for candidate in state.lifecycle.work_items if candidate.item_id == item_id), None)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' was not found.")
    definition = next(
        (value.definition for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item_id),
        None,
    )
    if definition is None:
        return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Item '{item_id}' has no definition.")
    attempts = tuple(
        query_models.ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in sorted(
            (candidate for candidate in state.lifecycle.attempts if candidate.item_id == item_id),
            key=_attempt_key,
        )
    )
    return query_models.ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v3",
        str(state.lifecycle.project.revision),
        str(item.item_id),
        definition.title,
        item.state,
        item.timing,
        item.outcome_evidence,
        item.next_action,
        item.notes or "",
        attempts,
        _preparation_status(state, item_id, now),
    )


def _definition_view(definition: work_models.WorkItemDefinition) -> query_models.WorkItemDefinitionView:
    return query_models.WorkItemDefinitionView(
        "pinboard-work-item-definition/v1",
        definition.title,
        definition.objective,
        definition.hypothesis,
        definition.evidence,
        definition.scope,
        definition.non_scope,
        definition.acceptance_criteria,
        tuple(definition.dependencies),
        definition.effect,
        definition.unlock,
    )


def item_definition(store: WorkStore, item_id: ItemId) -> DecisionResult[query_models.ItemDefinition]:
    state = store.snapshot()
    if not any(item.item_id == item_id for item in state.lifecycle.work_items):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    current = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item_id),
        None,
    )
    if current is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Item '{item_id}' has no accepted definition.",
        )
    return query_models.ItemDefinition(
        "pinboard-item-definition/v1",
        "sqlite-v3",
        state.lifecycle.project.revision,
        item_id,
        current.revision,
        current.digest,
        _definition_view(current.definition),
    )


def item_definition_history(
    store: WorkStore,
    item_id: ItemId,
    *,
    limit: int = 20,
    before_revision: int | None = None,
) -> DecisionResult[query_models.ItemDefinitionHistory]:
    state = store.snapshot()
    if not any(item.item_id == item_id for item in state.lifecycle.work_items):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    available = tuple(
        value
        for value in reversed(state.lifecycle.definition_revisions)
        if value.item_id == item_id and (before_revision is None or value.revision < before_revision)
    )
    selected = available[:limit]
    rows = tuple(
        query_models.ItemDefinitionHistoryRow(
            value.revision,
            value.digest,
            _definition_view(value.definition),
            value.reason,
            value.source_task_id,
            value.accepted_at.isoformat(),
            value.before_digest,
            value.after_digest,
            value.accepted_project_revision,
        )
        for value in selected
    )
    return query_models.ItemDefinitionHistory(
        "pinboard-item-definition-history/v1",
        "sqlite-v3",
        state.lifecycle.project.revision,
        item_id,
        rows,
        rows[-1].revision if len(available) > limit else None,
    )


def _preview_time(value: datetime) -> query_models.QueryResult[datetime]:
    if value.tzinfo is None:
        return query_models.QueryFailure(
            query_models.QueryRejectionCode.PARALLEL_TIME_INVALID,
            "Preview time must be timezone-aware.",
        )
    return value


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
    preparation = next(
        (candidate for candidate in state.authority.preparation_leases if candidate.item_id == item_id),
        None,
    )
    if (
        preparation is not None
        and preparation.state == PreparationLeaseStatus.ACTIVE
        and preparation.expires_at > current
    ):
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.PREPARATION_OWNED,
                f"Item '{item_id}' is being prepared until {preparation.expires_at.isoformat()}.",
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
    now: datetime,
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
    if any(item_id not in by_id for item_id in selected):
        return query_models.QueryFailure(
            query_models.QueryRejectionCode.PARALLEL_SELECTION_INVALID,
            "Selected item identities must be current items.",
        )
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else live
    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
    live_ids = frozenset(by_id)
    items: list[query_models.ParallelItem] = []
    for item, live_state in candidates:
        reasons = _parallel_reasons(state, item.item_id, live_ids, current)
        common = (
            str(item.item_id),
            definitions[item.item_id].title,
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
