from dataclasses import replace
from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.errors import MutationContractError, MutationContractErrorCode
from charlie_pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
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
    stored_close_outcome,
    stored_live_work_state,
)
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.authority_models import (
    AttemptLeaseStatus,
)
from charlie_pinboard.domain.history import (
    HistoryOutcome,
    encode_transition_receipt_outcome,
)
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    ProposalId,
    TaskId,
)


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


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case TransitionMutation(decision=decision):
            checkpoint = None
            candidate = None
            match decision.change:
                case decision_models.CheckpointAcceptanceChange(checkpoint=value, candidate=accepted_candidate):
                    checkpoint = str(value)
                    candidate = str(accepted_candidate)
                case decision_models.ReviewAcceptanceChange(candidate=accepted_candidate):
                    candidate = str(accepted_candidate)
                case decision_models.ReviewSubmissionChange(protected_candidate_after=accepted_candidate):
                    candidate = str(accepted_candidate)
                case (
                    decision_models.AcceptedProposalChange()
                    | decision_models.ActivationChange()
                    | decision_models.AttemptStateChange()
                    | decision_models.BlockAttemptChange()
                    | decision_models.BlockItemChange()
                    | decision_models.AttemptClosureChange()
                    | decision_models.CompletionChange()
                    | decision_models.CoordinatorTransferChange()
                    | decision_models.ItemClosureChange()
                    | decision_models.ItemStateChange()
                    | decision_models.MergedProposalChange()
                    | decision_models.ReturnedProposalChange()
                    | decision_models.RejectedProposalChange()
                    | decision_models.ResumeAttemptChange()
                    | decision_models.ReviewReturnChange()
                ):
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
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
        work_models.CanonicalJson(outcome.payload),
        decided_at,
    )


def _history_action_kind(value: decision_models.ActionKind) -> TransitionHistoryActionKind:
    match value:
        case (
            decision_models.ActionKind.ACCEPT_CHECKPOINT
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_PROPOSAL
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.COMPLETE
            | decision_models.ActionKind.CLOSE
            | decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.MERGE_PROPOSAL
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.REPORT_BLOCKER
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
            | decision_models.ActionKind.SUBMIT_REVIEW
            | decision_models.ActionKind.TRANSFER_COORDINATOR
        ):
            return TransitionHistoryActionKind(value.value)
        case _ as unreachable:
            assert_never(unreachable)


def _history_authorization_kind(
    value: decision_models.AuthorizationKind,
) -> TransitionHistoryAuthorizationKind:
    match value:
        case (
            decision_models.AuthorizationKind.COORDINATOR
            | decision_models.AuthorizationKind.COORDINATION
            | decision_models.AuthorizationKind.ATTEMPT
        ):
            return TransitionHistoryAuthorizationKind(value.value)
        case decision_models.AuthorizationKind.OBSERVER:
            raise MutationContractError(
                MutationContractErrorCode.RECEIPT_MISMATCH,
                "An observer action cannot produce mutation history.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _common_after(mutation: StoredStateMutation) -> StoredWorkState:
    before = mutation.before
    project = before.lifecycle.project
    receipt = mutation.receipt
    next_history_id = 1 + max((int(value.history_id) for value in before.transition_receipts), default=0)
    if int(receipt.history_id) != next_history_id or receipt.project_revision != project.revision + 1:
        raise MutationContractError(
            MutationContractErrorCode.RECEIPT_MISMATCH,
            "The accepted stored receipt does not identify the mutation exactly.",
        )
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
        intake.effect,
        intake.unlock,
        intake.urgency_evidence,
        None,
        mutation.receipt.project_revision,
    )
    if any(value.proposal_id == intake.proposal_id for value in common.proposals.proposals):
        raise MutationContractError(
            MutationContractErrorCode.PROPOSAL_EXISTS, "Proposal creation identity already exists."
        )
    visible = decision.visible_item
    if any(value.item_id == visible.item_id for value in common.lifecycle.work_items):
        raise MutationContractError(
            MutationContractErrorCode.PROPOSAL_EXISTS, "Visible intake identity already exists."
        )
    now = mutation.receipt.transition.decided_at
    revision = mutation.receipt.project_revision
    items = [
        replace(value, queue_position=value.queue_position + 1)
        if value.queue_position is not None and value.queue_position >= visible.position
        else value
        for value in common.lifecycle.work_items
    ]
    items.append(
        StoredWorkItem(
            visible.item_id,
            intake.user_label,
            StoredWorkItemState.INTAKE,
            None,
            f"proposal:{intake.proposal_id}",
            intake.trigger,
            intake.why_it_matters,
            intake.effect,
            intake.unlock,
            None,
            intake.unlock,
            intake.urgency_evidence,
            1,
            visible.scope_digest,
            revision,
            now,
            now,
            visible.position,
        )
    )
    scope_revisions = [
        *common.lifecycle.scope_revisions,
        ItemScopeRevision(visible.item_id, 1, visible.scope_digest, revision, now),
    ]
    dependencies = [
        *common.lifecycle.dependencies,
        *(
            ItemDependency(visible.item_id, dependency, position)
            for position, dependency in enumerate(visible.dependencies)
        ),
    ]
    if (prerequisite := decision.prerequisite_change) is not None:
        target_index = next(
            (index for index, value in enumerate(items) if value.item_id == prerequisite.item_id),
            None,
        )
        if target_index is None:
            raise MutationContractError(
                MutationContractErrorCode.PROPOSAL_RELATION_TARGET_MISSING,
                "A prerequisite relation requires its current target item.",
            )
        target = items[target_index]
        if (
            target.scope_revision != prerequisite.scope_revision
            or target.scope_digest != prerequisite.scope_digest_before
        ):
            raise MutationContractError(
                MutationContractErrorCode.ITEM_CHANGE_STALE,
                "The prerequisite target scope changed before proposal creation.",
            )
        next_scope_revision = target.scope_revision + 1
        items[target_index] = replace(
            target,
            scope_revision=next_scope_revision,
            scope_digest=prerequisite.scope_digest_after,
            subject_revision=revision,
            updated_at=now,
        )
        dependencies.append(ItemDependency(prerequisite.item_id, prerequisite.dependency_id, prerequisite.position))
        scope_revisions.append(
            ItemScopeRevision(
                prerequisite.item_id,
                next_scope_revision,
                prerequisite.scope_digest_after,
                revision,
                now,
            )
        )
    lifecycle = replace(
        common.lifecycle,
        work_items=tuple(sorted(items, key=_work_item_key)),
        scope_revisions=tuple(sorted(scope_revisions, key=_scope_revision_key)),
        dependencies=tuple(sorted(dependencies, key=_dependency_key)),
    )
    return replace(
        common,
        lifecycle=lifecycle,
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


def _item_state_after(
    lifecycle: LifecycleRecords,
    item_id: ItemId,
    before: work_models.WorkState,
    after: StoredWorkItemState,
    revision: int,
    now: datetime,
    outcome_evidence: str | None = None,
) -> LifecycleRecords:
    items = list(lifecycle.work_items)
    index = next((position for position, item in enumerate(items) if item.item_id == item_id), None)
    if index is None or items[index].state.value != before.value:
        raise MutationContractError(MutationContractErrorCode.ITEM_CHANGE_STALE, "The transition item change is stale.")
    queue_position = items[index].queue_position
    terminal = after in {
        StoredWorkItemState.DONE,
        StoredWorkItemState.SUPERSEDED,
        StoredWorkItemState.DROPPED,
    }
    if terminal and queue_position is not None:
        items = [
            replace(value, queue_position=value.queue_position - 1)
            if value.item_id != item_id and value.queue_position is not None and value.queue_position > queue_position
            else value
            for value in items
        ]
    items[index] = replace(
        items[index],
        state=after,
        outcome_evidence=outcome_evidence,
        subject_revision=revision,
        updated_at=now,
        queue_position=None if terminal else queue_position,
    )
    return replace(lifecycle, work_items=tuple(items))


def _item_dependencies_after(
    lifecycle: LifecycleRecords,
    item: ItemId,
    dependencies_after: tuple[ItemId, ...],
) -> LifecycleRecords:
    dependencies = (
        *(value for value in lifecycle.dependencies if value.item_id != item),
        *(ItemDependency(item, dependency, position) for position, dependency in enumerate(dependencies_after)),
    )
    return replace(lifecycle, dependencies=tuple(sorted(dependencies, key=_dependency_key)))


def _accepted_proposal_after(
    change: decision_models.AcceptedProposalChange,
    lifecycle: LifecycleRecords,
    revision: int,
    now: datetime,
) -> LifecycleRecords:
    accepted = change.accepted_item
    items = list(lifecycle.work_items)
    item_index = next((index for index, item in enumerate(items) if item.item_id == accepted.item), None)
    if item_index is None or items[item_index].state != StoredWorkItemState.INTAKE:
        raise MutationContractError(
            MutationContractErrorCode.ITEM_CHANGE_STALE,
            "Proposal acceptance requires its existing visible intake item.",
        )
    current = items[item_index]
    scope_revision = current.scope_revision
    scope_revisions = lifecycle.scope_revisions
    if current.scope_digest != accepted.scope_digest:
        scope_revision += 1
        scope_revisions = tuple(
            sorted(
                (
                    *scope_revisions,
                    ItemScopeRevision(accepted.item, scope_revision, accepted.scope_digest, revision, now),
                ),
                key=_scope_revision_key,
            )
        )
    items[item_index] = replace(
        current,
        user_label=accepted.user_label,
        state=stored_live_work_state(accepted.state),
        timing=accepted.timing,
        source=accepted.source,
        trigger=accepted.trigger,
        why_it_matters=accepted.why_it_matters,
        effect=accepted.effect,
        unlock=accepted.unlock,
        next_action=accepted.next_action,
        notes=accepted.notes,
        scope_revision=scope_revision,
        scope_digest=accepted.scope_digest,
        subject_revision=revision,
        updated_at=now,
    )
    dependencies = tuple(value for value in lifecycle.dependencies if value.item_id != accepted.item)
    return replace(
        lifecycle,
        work_items=tuple(items),
        scope_revisions=scope_revisions,
        dependencies=tuple(
            sorted(
                (
                    *dependencies,
                    *(
                        ItemDependency(accepted.item, dependency, position)
                        for position, dependency in enumerate(accepted.dependencies)
                    ),
                ),
                key=_dependency_key,
            )
        ),
    )


def _activation_attempt_after(
    change: decision_models.ActivationChange,
    lifecycle: LifecycleRecords,
    revision: int,
    now: datetime,
) -> LifecycleRecords:
    item = next((value for value in lifecycle.work_items if value.item_id == change.item), None)
    if item is None:
        raise MutationContractError(
            MutationContractErrorCode.ATTEMPT_ITEM_MISSING, "Attempt creation requires its current item."
        )
    attempt = StoredAttempt(
        change.attempt,
        item.item_id,
        work_models.AttemptState.ACTIVE,
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
    return replace(lifecycle, attempts=tuple(sorted((*lifecycle.attempts, attempt), key=_attempt_key)))


def _attempt_state_after(
    lifecycle: LifecycleRecords,
    attempt_id: AttemptId,
    before: work_models.AttemptState,
    after: work_models.AttemptState,
    revision: int,
    now: datetime,
    *,
    brief_artifact_ref_id: ArtifactRefId | None = None,
    protected_candidate_after: CandidateId | None = None,
    candidate_observed_at: datetime | None = None,
) -> LifecycleRecords:
    attempts = list(lifecycle.attempts)
    index = next((position for position, attempt in enumerate(attempts) if attempt.attempt_id == attempt_id), None)
    if index is None or attempts[index].state != before:
        raise MutationContractError(
            MutationContractErrorCode.ATTEMPT_MISSING_OR_BEFORE_STATE_STALE,
            "The stored attempt is missing or its before state is stale.",
        )
    clears_candidate = after.value in {"active", "paused", "blocked"}
    records_candidate = after == work_models.AttemptState.REVIEW
    attempts[index] = replace(
        attempts[index],
        state=after,
        brief_artifact_ref_id=(
            brief_artifact_ref_id if brief_artifact_ref_id is not None else attempts[index].brief_artifact_ref_id
        ),
        candidate_revision=(
            None
            if clears_candidate
            else str(protected_candidate_after)
            if records_candidate
            else attempts[index].candidate_revision
        ),
        candidate_recorded_at=(
            None
            if clears_candidate
            else candidate_observed_at
            if records_candidate
            else attempts[index].candidate_recorded_at
        ),
        subject_revision=revision,
        updated_at=now,
    )
    return replace(lifecycle, attempts=tuple(attempts))


def _proposal_disposition_after(
    common: StoredWorkState,
    proposal_id: ProposalId,
    disposition: work_models.ProposalDisposition,
    revision: int,
) -> StoredWorkState:
    proposals = list(common.proposals.proposals)
    index = next((position for position, proposal in enumerate(proposals) if proposal.proposal_id == proposal_id), None)
    if index is None or proposals[index].disposition is not None:
        raise MutationContractError(
            MutationContractErrorCode.PROPOSAL_CHANGE_STALE, "The transition proposal change is stale."
        )
    proposals[index] = replace(
        proposals[index],
        disposition=disposition,
        subject_revision=revision,
    )
    return replace(common, proposals=replace(common.proposals, proposals=tuple(proposals)))


def _transition_attempt_authority_after(
    change: decision_models.AttemptAuthorityChange,
    common: StoredWorkState,
    decided_at: datetime,
) -> StoredWorkState:
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
        raise MutationContractError(
            MutationContractErrorCode.ATTEMPT_AUTHORITY_GENERATION_MISSING,
            "Attempt-authority fencing requires its exact retained generation.",
        )
    if change.after.lease_id is not None or change.after.generation != change.before.generation + 1:
        raise MutationContractError(
            MutationContractErrorCode.ATTEMPT_AUTHORITY_GENERATION_INVALID,
            "Attempt-authority fencing must allocate one revoked generation.",
        )
    counters[counter_index] = replace(counters[counter_index], generation_high_water=change.after.generation)
    leases[lease_index] = replace(
        leases[lease_index],
        generation=change.after.generation,
        expires_at=decided_at,
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


def _transition_coordinator_after(
    change: decision_models.CoordinatorAuthorityChange,
    common: StoredWorkState,
) -> StoredWorkState:
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
        raise MutationContractError(MutationContractErrorCode.TRANSFER_STALE, "The coordinator transfer is stale.")
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
            raise MutationContractError(
                MutationContractErrorCode.ATTEMPT_AUTHORITY_COUNTER_MISSING,
                "Attempt authority requires its retained counter.",
            )
        counters.append(counter)
    else:
        if counters[counter_index].generation_high_water != decision.counter_before:
            raise MutationContractError(
                MutationContractErrorCode.ATTEMPT_AUTHORITY_COUNTER_STALE, "The attempt-authority counter is stale."
            )
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
            raise MutationContractError(
                MutationContractErrorCode.ATTEMPT_AUTHORITY_LEASE_MISSING,
                "Attempt authority expected a retained lease row.",
            )
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


def _transition_focus_after(
    mutation: TransitionMutation,
    common: StoredWorkState,
    item: ItemId,
    attempt: AttemptId | None,
    *,
    terminal: bool = False,
) -> StoredWorkState:
    action = mutation.decision.action
    if terminal:
        next_action = "select"
    else:
        match action:
            case (
                decision_models.PauseAction() | decision_models.BlockAttemptAction() | decision_models.BlockItemAction()
            ):
                next_action = "resume"
            case decision_models.SubmitReviewAction():
                next_action = "review"
            case (
                decision_models.AcceptReviewAndContinueAction()
                | decision_models.ReturnForCorrectionAction()
                | decision_models.ResumeAction()
                | decision_models.ReopenAction()
                | decision_models.MarkReadyAction()
            ):
                next_action = "continue"
            case decision_models.DeferAction():
                next_action = "reopen"
            case (
                decision_models.AcceptCheckpointAction()
                | decision_models.AcceptProposalAction()
                | decision_models.ActivateAction()
                | decision_models.CompleteAction()
                | decision_models.CloseAction()
                | decision_models.MergeProposalAction()
                | decision_models.RejectProposalAction()
                | decision_models.ReturnProposalAction()
                | decision_models.TransferCoordinatorAction()
            ):
                next_action = action.kind.value
            case _ as unreachable:
                assert_never(unreachable)
    return replace(
        common,
        focus=StoredFocus(
            None if terminal else item,
            None if terminal else attempt,
            next_action,
            mutation.receipt.project_revision,
        ),
    )


def _transition_after(  # noqa: C901, PLR0912, PLR0915
    mutation: TransitionMutation, common: StoredWorkState
) -> StoredWorkState:
    decision = mutation.decision
    change = decision.change
    revision = mutation.receipt.project_revision
    now = decision.receipt.decided_at
    lifecycle = common.lifecycle
    result = common
    item: ItemId | None = None
    attempt: AttemptId | None = None
    terminal = False
    match change:
        case decision_models.ItemStateChange(item=item, before=before, after=after):
            lifecycle = _item_state_after(lifecycle, item, before, stored_live_work_state(after), revision, now)
        case decision_models.ActivationChange(item=item, item_before=before, attempt=attempt):
            lifecycle = _item_state_after(lifecycle, item, before, StoredWorkItemState.ACTIVE, revision, now)
            lifecycle = _activation_attempt_after(change, lifecycle, revision, now)
        case decision_models.AttemptStateChange(
            item=item,
            item_before=item_before,
            item_after=item_after,
            attempt=attempt,
            attempt_before=attempt_before,
            attempt_after=attempt_after,
        ):
            lifecycle = _item_state_after(
                lifecycle, item, item_before, stored_live_work_state(item_after), revision, now
            )
            lifecycle = _attempt_state_after(lifecycle, attempt, attempt_before, attempt_after, revision, now)
        case decision_models.BlockAttemptChange(
            item=item,
            item_before=item_before,
            attempt=attempt,
            attempt_before=attempt_before,
            dependencies_after=dependencies_after,
        ):
            lifecycle = _item_state_after(lifecycle, item, item_before, StoredWorkItemState.BLOCKED, revision, now)
            lifecycle = _attempt_state_after(
                lifecycle, attempt, attempt_before, work_models.AttemptState.BLOCKED, revision, now
            )
            lifecycle = _item_dependencies_after(lifecycle, item, dependencies_after)
        case decision_models.BlockItemChange(item=item, item_before=item_before, dependencies_after=dependencies_after):
            lifecycle = _item_state_after(lifecycle, item, item_before, StoredWorkItemState.BLOCKED, revision, now)
            lifecycle = _item_dependencies_after(lifecycle, item, dependencies_after)
        case decision_models.ResumeAttemptChange(
            item=item,
            item_before=item_before,
            attempt=attempt,
            attempt_before=attempt_before,
            brief_artifact_ref_id=brief,
        ):
            lifecycle = _item_state_after(lifecycle, item, item_before, StoredWorkItemState.ACTIVE, revision, now)
            lifecycle = _attempt_state_after(
                lifecycle,
                attempt,
                attempt_before,
                work_models.AttemptState.ACTIVE,
                revision,
                now,
                brief_artifact_ref_id=brief,
            )
        case decision_models.ReviewSubmissionChange(
            item=item,
            attempt=attempt,
            protected_candidate_after=candidate,
            candidate_observed_at=observed_at,
        ):
            lifecycle = _item_state_after(
                lifecycle, item, work_models.WorkState.ACTIVE, StoredWorkItemState.REVIEW, revision, now
            )
            lifecycle = _attempt_state_after(
                lifecycle,
                attempt,
                work_models.AttemptState.ACTIVE,
                work_models.AttemptState.REVIEW,
                revision,
                now,
                protected_candidate_after=candidate,
                candidate_observed_at=observed_at,
            )
        case (
            decision_models.ReviewAcceptanceChange(item=item, attempt=attempt, authority_change=authority_change)
            | decision_models.ReviewReturnChange(item=item, attempt=attempt, authority_change=authority_change)
        ):
            lifecycle = _item_state_after(
                lifecycle, item, work_models.WorkState.REVIEW, StoredWorkItemState.ACTIVE, revision, now
            )
            lifecycle = _attempt_state_after(
                lifecycle, attempt, work_models.AttemptState.REVIEW, work_models.AttemptState.ACTIVE, revision, now
            )
            result = _transition_attempt_authority_after(authority_change, result, now)
        case decision_models.CompletionChange(
            item=item,
            item_before=item_before,
            attempt=attempt,
            attempt_before=attempt_before,
            evidence=evidence,
            authority_change=authority_change,
        ):
            lifecycle = _item_state_after(
                lifecycle, item, item_before, StoredWorkItemState.DONE, revision, now, evidence
            )
            lifecycle = _attempt_state_after(
                lifecycle, attempt, attempt_before, work_models.AttemptState.DONE, revision, now
            )
            if authority_change is not None:
                result = _transition_attempt_authority_after(authority_change, result, now)
            terminal = True
        case decision_models.ItemClosureChange(
            item=item,
            item_before=item_before,
            terminal_state=terminal_state,
            evidence=evidence,
        ):
            lifecycle = _item_state_after(
                lifecycle, item, item_before, stored_close_outcome(terminal_state), revision, now, evidence
            )
            terminal = True
        case decision_models.AttemptClosureChange(
            item=item,
            item_before=item_before,
            terminal_state=terminal_state,
            evidence=evidence,
            attempt=attempt,
            attempt_before=attempt_before,
            authority_change=authority_change,
        ):
            lifecycle = _item_state_after(
                lifecycle, item, item_before, stored_close_outcome(terminal_state), revision, now, evidence
            )
            lifecycle = _attempt_state_after(
                lifecycle, attempt, attempt_before, work_models.AttemptState.DONE, revision, now
            )
            if authority_change is not None:
                result = _transition_attempt_authority_after(authority_change, result, now)
            terminal = True
        case decision_models.AcceptedProposalChange(accepted_item=accepted, proposal=proposal, disposed_at=disposed_at):
            item = accepted.item
            lifecycle = _accepted_proposal_after(change, lifecycle, revision, now)
            result = _proposal_disposition_after(
                result, proposal, work_models.AcceptedProposalDisposition(item, disposed_at), revision
            )
        case decision_models.MergedProposalChange(proposal=proposal, target_item=target, disposed_at=disposed_at):
            lifecycle = _item_state_after(
                lifecycle,
                ItemId(proposal),
                work_models.WorkState.INTAKE,
                StoredWorkItemState.SUPERSEDED,
                revision,
                now,
                f"Merged into {target}.",
            )
            result = _proposal_disposition_after(
                result, proposal, work_models.MergedProposalDisposition(target, disposed_at), revision
            )
        case decision_models.ReturnedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
            result = _proposal_disposition_after(
                result,
                proposal,
                work_models.ReturnedProposalDisposition(reason, disposed_at),
                revision,
            )
        case decision_models.RejectedProposalChange(proposal=proposal, reason=reason, disposed_at=disposed_at):
            lifecycle = _item_state_after(
                lifecycle,
                ItemId(proposal),
                work_models.WorkState.INTAKE,
                StoredWorkItemState.DROPPED,
                revision,
                now,
                reason,
            )
            result = _proposal_disposition_after(
                result,
                proposal,
                work_models.RejectedProposalDisposition(reason, disposed_at),
                revision,
            )
        case decision_models.CheckpointAcceptanceChange(item=item, attempt=attempt, authority_change=authority_change):
            lifecycle = _item_state_after(
                lifecycle, item, work_models.WorkState.REVIEW, StoredWorkItemState.PAUSED, revision, now
            )
            lifecycle = _attempt_state_after(
                lifecycle, attempt, work_models.AttemptState.REVIEW, work_models.AttemptState.PAUSED, revision, now
            )
            result = _transition_attempt_authority_after(authority_change, result, now)
        case decision_models.CoordinatorTransferChange(authority_change=authority_change):
            result = _transition_coordinator_after(authority_change, result)
        case _ as unreachable:
            assert_never(unreachable)
    result = replace(result, lifecycle=lifecycle)
    if item is None:
        return result
    return _transition_focus_after(mutation, result, item, attempt, terminal=terminal)


def _carrier_after(mutation: StoredStateMutation, common: StoredWorkState) -> StoredWorkState:
    supplied = mutation.after
    match mutation:
        case ProposalCreationMutation():
            return _proposal_creation_after(mutation, common)
        case TransitionMutation():
            return _transition_after(mutation, common)
        case CoordinationAuthorityMutation():
            if supplied.authority.coordination == common.authority.coordination:
                raise MutationContractError(
                    MutationContractErrorCode.COORDINATION_CARRIER_INVALID,
                    "A coordination-authority carrier must change the coordination lease.",
                )
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
                raise MutationContractError(
                    MutationContractErrorCode.COORDINATION_DECISION_MISMATCH,
                    "The coordination decision does not match its relational delta.",
                )
            if decision.before is None:
                if retained is not None:
                    raise MutationContractError(
                        MutationContractErrorCode.COORDINATION_EXPECTED_ABSENT,
                        "Coordination acquisition expected no retained authority.",
                    )
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
                raise MutationContractError(
                    MutationContractErrorCode.COORDINATION_STALE, "The coordination decision is stale."
                )
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


def project_transition_mutation(before: StoredWorkState, decision: decision_models.Decision) -> TransitionMutation:
    """Project one pure lifecycle decision into its exact flat accepted mutation."""

    action = decision.action
    capability = action.capability
    actor_task_id: TaskId | None = None
    actor_host_id: HostId | None = None
    if capability.authorization == decision_models.AuthorizationKind.ATTEMPT and capability.lease_id is not None:
        anchor = next(
            (
                value
                for value in before.authority.attempt_generations
                if value.lease_id == capability.lease_id and value.generation == capability.coordinator_generation
            ),
            None,
        )
        if anchor is not None:
            actor_task_id, actor_host_id = anchor.task_id, anchor.host_id
    elif capability.authorization == decision_models.AuthorizationKind.COORDINATION:
        coordination = before.authority.coordination
        if coordination is not None:
            actor_task_id, actor_host_id = coordination.task_id, coordination.host_id
    receipt = MutationReceipt(
        decision.receipt,
        HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
        before.lifecycle.project.revision + 1,
        _history_action_kind(action.kind),
        HistorySubjectId(capability.subject),
        None,
        _history_authorization_kind(capability.authorization),
        actor_task_id,
        actor_host_id,
        "decision/v1",
        work_models.CanonicalJson(b"{}"),
    )
    draft = TransitionMutation(decision, before, before, receipt)
    return replace(draft, after=expected_stored_state(draft))
