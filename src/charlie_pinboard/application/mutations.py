from dataclasses import dataclass, replace
from typing import assert_never

from charlie_pinboard.application.stored_state import (
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    ItemDependency,
    ItemScopeRevision,
    LifecycleRecords,
    ProposalEvidence,
    ProposalFreshness,
    StoredAttempt,
    StoredAttemptLease,
    StoredFocus,
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import (
    AttemptAuthorityDecision,
    AttemptLeaseStatus,
    CoordinationAuthorityDecision,
)
from charlie_pinboard.domain.decisions import ActionKind, AuthorizationKind, Decision, TransitionReceipt
from charlie_pinboard.domain.history import (
    HistoryOutcome,
    encode_transition_receipt_outcome,
)
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    TaskId,
)
from charlie_pinboard.domain.model import CanonicalJson
from charlie_pinboard.domain.proposal_decisions import ProposalCreationDecision


class MutationContractError(ValueError):
    pass


def _work_item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _scope_revision_key(value: ItemScopeRevision) -> tuple[str, int]:
    return str(value.item_id), value.revision


def _dependency_key(value: ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _attempt_generation_key(value: AttemptLeaseGeneration) -> tuple[str, int]:
    return str(value.attempt_id), value.generation


def _attempt_counter_key(value: AttemptLeaseCounter) -> str:
    return str(value.attempt_id)


def _stored_attempt_lease_key(value: StoredAttemptLease) -> str:
    return str(value.attempt_id)


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Stored-history identity for an ordinary transition receipt."""

    transition: TransitionReceipt
    history_id: HistoryId
    project_revision: int
    action_kind: TransitionHistoryActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: TransitionHistoryAuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_payload: CanonicalJson


@dataclass(frozen=True, slots=True)
class ProposalCreationMutation:
    """Persists an authorized proposal intake without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: ProposalCreationDecision


@dataclass(frozen=True, slots=True)
class TransitionMutation:
    """Persists one accepted closed lifecycle decision as an exact relational delta."""

    decision: Decision
    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityMutation:
    """Persists an authorized coordination change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: CoordinationAuthorityDecision


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutation:
    """Persists an authorized attempt-authority change without deciding its legality."""

    before: StoredWorkState
    after: StoredWorkState
    receipt: MutationReceipt
    decision: AttemptAuthorityDecision


type TransitionReceiptMutation = (
    TransitionMutation | ProposalCreationMutation | CoordinationAuthorityMutation | AttemptAuthorityMutation
)

type StoredStateMutation = TransitionReceiptMutation


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case TransitionMutation(decision=decision):
            checkpoint = None
            candidate = None
            if decision.checkpoint_acceptance_change is not None:
                checkpoint = str(decision.checkpoint_acceptance_change.checkpoint)
                candidate = str(decision.checkpoint_acceptance_change.candidate)
            if decision.attempt_change is not None and decision.attempt_change.protected_candidate_after is not None:
                candidate = str(decision.attempt_change.protected_candidate_after)
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(
                    evidence=decision.receipt.evidence,
                    outcome=decision.receipt.outcome,
                    candidate=candidate,
                    checkpoint=checkpoint,
                ),
            )
        case (
            TransitionMutation()
            | ProposalCreationMutation()
            | CoordinationAuthorityMutation()
            | AttemptAuthorityMutation()
        ):
            transition = mutation.receipt.transition
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(evidence=transition.evidence, outcome=transition.outcome),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _stored_receipt(mutation: StoredStateMutation) -> StoredTransitionReceipt:
    outcome = _history_outcome(mutation)
    match mutation:
        case (
            TransitionMutation(receipt=receipt)
            | ProposalCreationMutation(receipt=receipt)
            | CoordinationAuthorityMutation(receipt=receipt)
            | AttemptAuthorityMutation(receipt=receipt)
        ):
            action_id = receipt.transition.action_id
            decided_at = receipt.transition.decided_at
        case _ as unreachable:
            assert_never(unreachable)
    return StoredTransitionReceipt(
        receipt.history_id,
        receipt.project_revision,
        action_id,
        receipt.action_kind,
        receipt.subject_id,
        receipt.artifact_ref_id,
        receipt.authorization,
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.input_schema,
        receipt.input_payload,
        outcome.outcome_schema,
        CanonicalJson(outcome.payload),
        decided_at,
    )


def _common_after(mutation: StoredStateMutation) -> StoredWorkState:
    before = mutation.before
    project = before.lifecycle.project
    receipt = mutation.receipt
    next_history_id = 1 + max((int(value.history_id) for value in before.transition_receipts), default=0)
    if int(receipt.history_id) != next_history_id or receipt.project_revision != project.revision + 1:
        raise MutationContractError("The accepted stored receipt does not identify the mutation exactly.")
    stored_receipt = _stored_receipt(mutation)
    return replace(
        before,
        lifecycle=replace(
            before.lifecycle,
            project=replace(project, revision=project.revision + 1, updated_at=stored_receipt.committed_at),
        ),
        transition_receipts=(*before.transition_receipts, stored_receipt),
    )


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _proposal_evidence_key(value: ProposalEvidence) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_freshness_key(value: ProposalFreshness) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_creation_after(
    mutation: ProposalCreationMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    intake = decision.proposal
    proposal = StoredProposal(
        intake.proposal_id,
        intake.created_at,
        mutation.receipt.transition.decided_at,
        intake.source_task_id,
        intake.user_label,
        intake.trigger,
        intake.why_it_matters,
        intake.relation,
        intake.relation_item,
        intake.effect,
        intake.unlock,
        intake.urgency_evidence,
        None,
        None,
        None,
        mutation.receipt.project_revision,
        None,
    )
    if any(value.proposal_id == intake.proposal_id for value in common.proposals.proposals):
        raise MutationContractError("Proposal creation identity already exists.")
    return replace(
        common,
        proposals=replace(
            common.proposals,
            proposals=tuple(
                sorted(
                    (*common.proposals.proposals, proposal),
                    key=_proposal_key,
                )
            ),
            evidence=tuple(
                sorted(
                    (
                        *common.proposals.evidence,
                        *(
                            ProposalEvidence(intake.proposal_id, position, value)
                            for position, value in enumerate(decision.evidence)
                        ),
                    ),
                    key=_proposal_evidence_key,
                )
            ),
            freshness=tuple(
                sorted(
                    (
                        *common.proposals.freshness,
                        *(
                            ProposalFreshness(intake.proposal_id, position, value)
                            for position, value in enumerate(decision.freshness)
                        ),
                    ),
                    key=_proposal_freshness_key,
                )
            ),
        ),
    )


def _transition_item_state(decision: Decision) -> StoredWorkItemState:
    if decision.action.kind == ActionKind.COMPLETE:
        return StoredWorkItemState.DONE
    try:
        state = StoredWorkItemState(decision.receipt.outcome)
    except ValueError as error:
        raise MutationContractError("A terminal transition must name its exact stored item state.") from error
    if state not in {StoredWorkItemState.DONE, StoredWorkItemState.SUPERSEDED, StoredWorkItemState.DROPPED}:
        raise MutationContractError("A terminal transition must name a terminal stored item state.")
    return state


def _transition_item_after(
    mutation: TransitionMutation,
    lifecycle: LifecycleRecords,
) -> LifecycleRecords:
    decision = mutation.decision
    change = decision.item_change
    if change is None:
        return lifecycle
    revision = mutation.receipt.project_revision
    now = decision.receipt.decided_at
    items = list(lifecycle.work_items)
    if change.before is None:
        proposal_change = decision.proposal_change
        accepted = None if proposal_change is None else proposal_change.accepted_item
        if accepted is None or change.after != accepted.state:
            raise MutationContractError("Item creation requires one complete accepted-proposal decision.")
        items.append(
            StoredWorkItem(
                accepted.item,
                accepted.user_label,
                StoredWorkItemState(accepted.state.value),
                accepted.timing,
                accepted.source,
                accepted.trigger,
                accepted.why_it_matters,
                accepted.effect,
                accepted.unlock,
                None,
                accepted.next_action,
                accepted.notes,
                1,
                accepted.scope_digest,
                revision,
                now,
                now,
            )
        )
        return replace(
            lifecycle,
            work_items=tuple(sorted(items, key=_work_item_key)),
            scope_revisions=tuple(
                sorted(
                    (
                        *lifecycle.scope_revisions,
                        ItemScopeRevision(accepted.item, 1, accepted.scope_digest, revision, now),
                    ),
                    key=_scope_revision_key,
                )
            ),
            dependencies=tuple(
                sorted(
                    (
                        *lifecycle.dependencies,
                        *(
                            ItemDependency(accepted.item, dependency, position)
                            for position, dependency in enumerate(accepted.dependencies)
                        ),
                    ),
                    key=_dependency_key,
                )
            ),
        )
    index = next((position for position, item in enumerate(items) if item.item_id == change.item), None)
    if index is None or items[index].state.value != change.before.value:
        raise MutationContractError("The transition item change is stale.")
    terminal = change.after is None
    items[index] = replace(
        items[index],
        state=_transition_item_state(decision) if change.after is None else StoredWorkItemState(change.after.value),
        outcome_evidence=change.outcome_evidence if terminal else None,
        subject_revision=revision,
        updated_at=now,
    )
    return replace(lifecycle, work_items=tuple(items))


def _transition_attempt_after(
    mutation: TransitionMutation,
    lifecycle: LifecycleRecords,
) -> LifecycleRecords:
    change = mutation.decision.attempt_change
    if change is None:
        return lifecycle
    revision = mutation.receipt.project_revision
    now = mutation.decision.receipt.decided_at
    attempts = list(lifecycle.attempts)
    if change.before is None:
        if (
            change.after is None
            or change.brief_artifact_ref_id is None
            or change.branch is None
            or change.base_revision is None
            or change.owner is None
            or mutation.decision.item_change is None
        ):
            raise MutationContractError("Attempt creation facts are incomplete.")
        item = next(
            (value for value in lifecycle.work_items if value.item_id == mutation.decision.item_change.item),
            None,
        )
        if item is None:
            raise MutationContractError("Attempt creation requires its current item.")
        attempts.append(
            StoredAttempt(
                change.attempt,
                item.item_id,
                change.after,
                change.branch,
                change.base_revision,
                change.owner,
                change.brief_artifact_ref_id,
                None,
                None,
                None,
                None,
                item.scope_revision,
                item.scope_digest,
                revision,
                now,
                now,
            )
        )
        return replace(lifecycle, attempts=tuple(sorted(attempts, key=_attempt_key)))
    index = next((position for position, attempt in enumerate(attempts) if attempt.attempt_id == change.attempt), None)
    if index is None or attempts[index].state != change.before or change.after is None:
        raise MutationContractError("The transition attempt change is stale or incomplete.")
    clears_candidate = change.after.value in {"active", "paused", "blocked"}
    records_candidate = change.after.value == "review"
    if records_candidate and (change.protected_candidate_after is None or change.candidate_observed_at is None):
        raise MutationContractError("Review submission requires exact protected candidate provenance.")
    attempts[index] = replace(
        attempts[index],
        state=change.after,
        brief_artifact_ref_id=(
            change.brief_artifact_ref_id
            if change.brief_artifact_ref_id is not None
            else attempts[index].brief_artifact_ref_id
        ),
        candidate_revision=(
            None
            if clears_candidate
            else str(change.protected_candidate_after)
            if records_candidate
            else attempts[index].candidate_revision
        ),
        candidate_recorded_at=(
            None
            if clears_candidate
            else change.candidate_observed_at
            if records_candidate
            else attempts[index].candidate_recorded_at
        ),
        subject_revision=revision,
        updated_at=now,
    )
    return replace(lifecycle, attempts=tuple(attempts))


def _transition_proposals_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.proposal_change
    if change is None:
        return common
    disposition = change.disposition
    proposals = list(common.proposals.proposals)
    index = next(
        (position for position, proposal in enumerate(proposals) if proposal.proposal_id == change.proposal), None
    )
    if index is None or proposals[index].disposition is not None:
        raise MutationContractError("The transition proposal change is stale.")
    proposals[index] = replace(
        proposals[index],
        disposition=disposition,
        disposition_target_item_id=change.target_item,
        disposition_reason=change.reason,
        subject_revision=mutation.receipt.project_revision,
        disposition_recorded_at=change.disposed_at,
    )
    return replace(common, proposals=replace(common.proposals, proposals=tuple(proposals)))


def _transition_attempt_authority_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.attempt_authority_change
    if change is None:
        return common
    authority = common.authority
    counters = list(authority.attempt_counters)
    counter_index = next(
        (index for index, value in enumerate(counters) if value.attempt_id == change.before.attempt), None
    )
    leases = list(authority.attempt_leases)
    lease_index = next((index for index, value in enumerate(leases) if value.attempt_id == change.before.attempt), None)
    anchor = next(
        (
            value
            for value in authority.attempt_generations
            if value.attempt_id == change.before.attempt and value.generation == change.before.generation
        ),
        None,
    )
    if counter_index is None or lease_index is None or anchor is None:
        raise MutationContractError("Attempt-authority fencing requires its exact retained generation.")
    if change.after.lease_id is not None or change.after.generation != change.before.generation + 1:
        raise MutationContractError("Attempt-authority fencing must allocate one revoked generation.")
    counters[counter_index] = replace(counters[counter_index], generation_high_water=change.after.generation)
    leases[lease_index] = replace(
        leases[lease_index],
        generation=change.after.generation,
        expires_at=mutation.decision.receipt.decided_at,
        state=AttemptLeaseStatus.REVOKED,
    )
    generations = (
        *authority.attempt_generations,
        AttemptLeaseGeneration(
            change.before.attempt, change.after.generation, anchor.lease_id, anchor.task_id, anchor.host_id
        ),
    )
    return replace(
        common,
        authority=replace(
            authority,
            attempt_counters=tuple(counters),
            attempt_generations=tuple(sorted(generations, key=_attempt_generation_key)),
            attempt_leases=tuple(leases),
        ),
    )


def _transition_coordinator_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.coordinator_authority_change
    if change is None:
        return common
    retained = common.authority.coordination
    if retained is None or (
        retained.lease_id,
        retained.task_id,
        retained.host_id,
        retained.generation,
        retained.acquired_at,
        retained.expires_at,
        retained.state.value,
    ) != (
        change.before.lease_id,
        change.before.task_id,
        change.before.host_id,
        change.before.generation,
        change.before.acquired_at,
        change.before.expires_at,
        change.before.state.value,
    ):
        raise MutationContractError("The coordinator transfer is stale.")
    after = change.after
    coordination = replace(
        retained,
        lease_id=after.lease_id,
        task_id=after.task_id,
        host_id=after.host_id,
        generation=after.generation,
        acquired_at=after.acquired_at,
        expires_at=after.expires_at,
        state=after.state,
    )
    return replace(common, authority=replace(common.authority, coordination=coordination))


def _attempt_authority_carrier_after(
    mutation: AttemptAuthorityMutation,
    common: StoredWorkState,
) -> StoredWorkState:
    decision = mutation.decision
    authority = common.authority
    counters = list(authority.attempt_counters)
    counter_index = next(
        (index for index, value in enumerate(counters) if value.attempt_id == decision.attempt),
        None,
    )
    counter = AttemptLeaseCounter(decision.attempt, decision.counter_after)
    if counter_index is None:
        if decision.counter_before != 0:
            raise MutationContractError("Attempt authority requires its retained counter.")
        counters.append(counter)
    else:
        if counters[counter_index].generation_high_water != decision.counter_before:
            raise MutationContractError("The attempt-authority counter is stale.")
        counters[counter_index] = counter
    leases = list(authority.attempt_leases)
    lease_index = next(
        (index for index, value in enumerate(leases) if value.attempt_id == decision.attempt),
        None,
    )
    after = decision.current_after
    stored_lease = StoredAttemptLease(
        after.attempt,
        after.generation,
        after.acquired_at,
        after.expires_at,
        after.state,
    )
    if lease_index is None:
        if decision.current_before is not None:
            raise MutationContractError("Attempt authority expected a retained lease row.")
        leases.append(stored_lease)
    else:
        leases[lease_index] = stored_lease
    generations = authority.attempt_generations
    if not any(value.attempt_id == after.attempt and value.generation == after.generation for value in generations):
        generations = (
            *generations,
            AttemptLeaseGeneration(after.attempt, after.generation, after.lease_id, after.task_id, after.host_id),
        )
    return replace(
        common,
        authority=replace(
            authority,
            attempt_counters=tuple(sorted(counters, key=_attempt_counter_key)),
            attempt_generations=tuple(sorted(generations, key=_attempt_generation_key)),
            attempt_leases=tuple(sorted(leases, key=_stored_attempt_lease_key)),
        ),
    )


def _transition_focus_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    change = mutation.decision.item_change
    if change is None:
        return common
    terminal = change.after is None
    kind = mutation.decision.action.kind
    if terminal:
        next_action = "select"
    elif kind in {ActionKind.PAUSE, ActionKind.BLOCK, ActionKind.BLOCK_ITEM}:
        next_action = "resume"
    elif kind == ActionKind.SUBMIT_REVIEW:
        next_action = "review"
    elif kind in {ActionKind.RETURN_FOR_CORRECTION, ActionKind.RESUME, ActionKind.REOPEN, ActionKind.MARK_READY}:
        next_action = "continue"
    elif kind == ActionKind.DEFER:
        next_action = "reopen"
    else:
        next_action = kind.value
    return replace(
        common,
        focus=StoredFocus(
            None if terminal else change.item,
            None if terminal else change.attempt,
            next_action,
            mutation.receipt.project_revision,
        ),
    )


def _transition_after(mutation: TransitionMutation, common: StoredWorkState) -> StoredWorkState:
    lifecycle = _transition_item_after(mutation, common.lifecycle)
    lifecycle = _transition_attempt_after(mutation, lifecycle)
    result = replace(common, lifecycle=lifecycle)
    result = _transition_proposals_after(mutation, result)
    result = _transition_coordinator_after(mutation, result)
    result = _transition_attempt_authority_after(mutation, result)
    return _transition_focus_after(mutation, result)


def _carrier_after(mutation: StoredStateMutation, common: StoredWorkState) -> StoredWorkState:
    supplied = mutation.after
    match mutation:
        case ProposalCreationMutation():
            return _proposal_creation_after(mutation, common)
        case TransitionMutation():
            return _transition_after(mutation, common)
        case CoordinationAuthorityMutation():
            if supplied.authority.coordination == common.authority.coordination:
                raise MutationContractError("A coordination-authority carrier must change the coordination lease.")
            decision = mutation.decision
            retained = mutation.before.authority.coordination
            changed = supplied.authority.coordination
            if changed is None or (
                changed.lease_id,
                changed.task_id,
                changed.host_id,
                changed.generation,
                changed.acquired_at,
                changed.expires_at,
                changed.state.value,
            ) != (
                decision.after.lease_id,
                decision.after.task_id,
                decision.after.host_id,
                decision.after.generation,
                decision.after.acquired_at,
                decision.after.expires_at,
                decision.after.state.value,
            ):
                raise MutationContractError("The coordination decision does not match its relational delta.")
            if decision.before is None:
                if retained is not None:
                    raise MutationContractError("Coordination acquisition expected no retained authority.")
            elif retained is None or (
                retained.lease_id,
                retained.task_id,
                retained.host_id,
                retained.generation,
                retained.acquired_at,
                retained.expires_at,
                retained.state.value,
            ) != (
                decision.before.lease_id,
                decision.before.task_id,
                decision.before.host_id,
                decision.before.generation,
                decision.before.acquired_at,
                decision.before.expires_at,
                decision.before.state.value,
            ):
                raise MutationContractError("The coordination decision is stale.")
            return replace(common, authority=replace(common.authority, coordination=supplied.authority.coordination))
        case AttemptAuthorityMutation():
            return _attempt_authority_carrier_after(mutation, common)
        case _ as unreachable:
            assert_never(unreachable)


def expected_stored_state(mutation: StoredStateMutation) -> StoredWorkState:
    common = _common_after(mutation)
    match mutation:
        case (
            TransitionMutation()
            | ProposalCreationMutation()
            | CoordinationAuthorityMutation()
            | AttemptAuthorityMutation()
        ):
            expected = _carrier_after(mutation, common)
        case _ as unreachable:
            assert_never(unreachable)
    return expected


def project_transition_mutation(before: StoredWorkState, decision: Decision) -> TransitionMutation:
    """Project one pure lifecycle decision into its exact flat accepted mutation."""

    action = decision.action
    actor_task_id: TaskId | None = None
    actor_host_id: HostId | None = None
    if action.authorization == AuthorizationKind.ATTEMPT and action.lease_id is not None:
        anchor = next(
            (
                value
                for value in before.authority.attempt_generations
                if value.lease_id == action.lease_id and value.generation == action.coordinator_generation
            ),
            None,
        )
        if anchor is not None:
            actor_task_id, actor_host_id = anchor.task_id, anchor.host_id
    elif action.authorization == AuthorizationKind.COORDINATION:
        coordination = before.authority.coordination
        if coordination is not None:
            actor_task_id, actor_host_id = coordination.task_id, coordination.host_id
    receipt = MutationReceipt(
        decision.receipt,
        HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
        before.lifecycle.project.revision + 1,
        TransitionHistoryActionKind(action.kind.value),
        HistorySubjectId(action.subject),
        None,
        TransitionHistoryAuthorizationKind(action.authorization.value),
        actor_task_id,
        actor_host_id,
        "decision/v1",
        CanonicalJson(b"{}"),
    )
    draft = TransitionMutation(decision, before, before, receipt)
    return replace(draft, after=expected_stored_state(draft))
