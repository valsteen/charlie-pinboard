from typing import assert_never

from charlie_pinboard.application.stored_state import (
    AttemptLeaseState,
    CoordinationLeaseState,
    ItemArtifactLink,
    ItemDependency,
    ItemResourceRequirement,
    ResourceInstanceState,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.identifiers import AttemptId, CandidateId, ItemId
from charlie_pinboard.domain.model import (
    ArtifactRecord,
    AttemptAuthority,
    AttemptRecord,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    ItemScope,
    LedgerSnapshot,
    MutationIntent,
    MutationReservation,
    MutationUseLease,
    PlanningImpact,
    PlanningObligation,
    ProposalRecord,
    ResourceAuthority,
    ResourceDefinition,
    ResourceInstance,
    ResourceObservation,
    ResourceRequirement,
    ResourceReservation,
    ResourceReservationCounter,
    ResourceUseLease,
    ScopeAnchor,
    ScopeArtifact,
    ScopeDependency,
    SubjectRevision,
    WorkItem,
    WorkState,
)
from charlie_pinboard.domain.resource_decisions import current_authorizing_grant


def _dependency_position(value: ItemDependency) -> int:
    return value.position


def _requirement_position(value: ItemResourceRequirement) -> int:
    return value.position


def _artifact_position(value: ItemArtifactLink) -> tuple[str, int]:
    return value.role.value, value.position


def _replacement_position(value: StoredPlanningReplacement) -> int:
    return value.position


def _obligation_position(value: StoredPlanningObligation) -> int:
    return value.position


def _live_state(state: StoredWorkItemState) -> WorkState | None:
    match state:
        case StoredWorkItemState.INTAKE:
            return WorkState.INTAKE
        case StoredWorkItemState.READY:
            return WorkState.READY
        case StoredWorkItemState.ACTIVE:
            return WorkState.ACTIVE
        case StoredWorkItemState.PAUSED:
            return WorkState.PAUSED
        case StoredWorkItemState.BLOCKED:
            return WorkState.BLOCKED
        case StoredWorkItemState.DEFERRED:
            return WorkState.DEFERRED
        case StoredWorkItemState.REVIEW:
            return WorkState.REVIEW
        case StoredWorkItemState.DONE | StoredWorkItemState.SUPERSEDED | StoredWorkItemState.DROPPED:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _work_item(value: StoredWorkItem, state: WorkState, attempt_by_item: dict[ItemId, AttemptId]) -> WorkItem:
    return WorkItem(
        value.item_id,
        state,
        value.timing.value if value.timing is not None else None,
        (),
        attempt_by_item.get(value.item_id),
        value.source or "",
        value.next_action,
        value.notes or "",
        value.outcome_evidence,
    )


def project_decision_snapshot(state: StoredWorkState) -> LedgerSnapshot:
    """Project complete persisted state into the narrower facts consumed by pure decisions."""

    attempts_by_item = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state.value not in {"done", "closed"}
    }
    live_items = tuple(
        _work_item(item, live_state, attempts_by_item)
        for item in state.lifecycle.work_items
        if (live_state := _live_state(item.state)) is not None
    )
    dependencies_by_item = {
        item.item_id: tuple(
            ScopeDependency(link.position, link.dependency_id)
            for link in sorted(
                (candidate for candidate in state.lifecycle.dependencies if candidate.item_id == item.item_id),
                key=_dependency_position,
            )
        )
        for item in state.lifecycle.work_items
    }
    requirements_by_item = {
        item.item_id: tuple(
            ResourceRequirement(link.position, link.resource_id)
            for link in sorted(
                (candidate for candidate in state.resources.requirements if candidate.item_id == item.item_id),
                key=_requirement_position,
            )
        )
        for item in state.lifecycle.work_items
    }
    artifact_by_id = {artifact.artifact_ref_id: artifact for artifact in state.artifacts.references}
    artifacts_by_item = {
        item.item_id: tuple(
            ScopeArtifact(
                link.role,
                link.position,
                artifact.kind.value,
                artifact.key,
                artifact.revision,
                artifact.selector,
                artifact.content_sha256,
            )
            for link in sorted(
                (candidate for candidate in state.lifecycle.item_artifacts if candidate.item_id == item.item_id),
                key=_artifact_position,
            )
            for artifact in (artifact_by_id[link.artifact_ref_id],)
        )
        for item in state.lifecycle.work_items
    }
    scopes = tuple(
        ScopeAnchor(
            item.item_id,
            anchor.revision,
            anchor.digest,
            ItemScope(
                item.item_id,
                item.user_label,
                item.trigger,
                item.why_it_matters,
                item.effect,
                item.unlock,
                dependencies_by_item[item.item_id],
                requirements_by_item[item.item_id],
                artifacts_by_item[item.item_id],
            ),
        )
        for item in state.lifecycle.work_items
        if _live_state(item.state) is not None
        for anchor in state.lifecycle.scope_revisions
        if (anchor.item_id, anchor.revision, anchor.digest) == (item.item_id, item.scope_revision, item.scope_digest)
    )
    work_items = tuple(
        WorkItem(
            item.item,
            item.state,
            item.timing,
            tuple(link.dependency_id for link in dependencies_by_item[item.item]),
            item.attempt,
            item.source,
            item.next_action,
            item.notes,
            item.outcome_evidence,
        )
        for item in live_items
    )

    planning_impacts = tuple(
        PlanningImpact(
            impact.impact_id,
            impact.source_item_id,
            impact.source_attempt_id,
            impact.source_scope_revision,
            impact.source_scope_digest,
            impact.summary,
            impact.evidence,
            tuple(
                PlanningObligation(
                    obligation.target_item_id,
                    obligation.position,
                    obligation.observed_scope_revision,
                    obligation.observed_scope_digest,
                    obligation.disposition,
                    obligation.evaluated_scope_revision,
                    obligation.evaluated_scope_digest,
                    obligation.resulting_scope_revision,
                    obligation.resulting_scope_digest,
                    tuple(
                        replacement.replacement_item_id
                        for replacement in sorted(
                            (
                                candidate
                                for candidate in state.planning.replacements
                                if candidate.impact_id == obligation.impact_id
                                and candidate.target_item_id == obligation.target_item_id
                            ),
                            key=_replacement_position,
                        )
                    ),
                    obligation.outcome_evidence,
                    obligation.reason,
                )
                for obligation in sorted(
                    (candidate for candidate in state.planning.obligations if candidate.impact_id == impact.impact_id),
                    key=_obligation_position,
                )
            ),
        )
        for impact in state.planning.impacts
    )

    attempt_by_id = {attempt.attempt_id: attempt for attempt in state.lifecycle.attempts}
    reservation_by_id = {reservation.reservation_id: reservation for reservation in state.resources.reservations}
    projected_use_leases = tuple(
        ResourceUseLease(
            use_lease.lease_id,
            use_lease.reservation_id,
            use_lease.attempt_lease_id,
            use_lease.attempt_lease_generation,
            use_lease.generation,
            use_lease.state,
            use_lease.generation_kind,
        )
        for use_lease in state.resources.use_leases
    )
    active_use_leases = tuple(
        grant
        for reservation in state.resources.reservations
        if (grant := current_authorizing_grant(projected_use_leases, reservation.reservation_id)) is not None
    )
    active_instance_ids = {
        instance.instance_id for instance in state.resources.instances if instance.state == ResourceInstanceState.ACTIVE
    }
    attempt_anchors = {(anchor.attempt_id, anchor.generation): anchor for anchor in state.authority.attempt_generations}
    attempt_authorities = tuple(
        AttemptAuthority(
            lease.attempt_id,
            attempt_by_id[lease.attempt_id].item_id,
            attempt_anchors[(lease.attempt_id, lease.generation)].lease_id
            if lease.state == AttemptLeaseState.ACTIVE
            else None,
            lease.generation,
            tuple(
                ResourceAuthority(
                    reservation_by_id[use_lease.reservation_id].resource_id,
                    reservation_by_id[use_lease.reservation_id].host_id,
                    use_lease.lease_id,
                    use_lease.generation,
                )
                for use_lease in active_use_leases
                if reservation_by_id[use_lease.reservation_id].attempt_id == lease.attempt_id
                and lease.state == AttemptLeaseState.ACTIVE
            ),
        )
        for lease in state.authority.attempt_leases
    )
    command_attempt_authorities = tuple(
        CommandAttemptAuthority(
            state.lifecycle.project.host_epoch,
            attempt_by_id[lease.attempt_id].item_id,
            str(
                next(
                    item.subject_revision
                    for item in state.lifecycle.work_items
                    if item.item_id == attempt_by_id[lease.attempt_id].item_id
                )
            ),
            lease.attempt_id,
            str(attempt_by_id[lease.attempt_id].subject_revision),
            anchor.task_id,
            anchor.host_id,
            anchor.lease_id,
            lease.generation,
            lease.expires_at,
        )
        for lease in state.authority.attempt_leases
        if lease.state == AttemptLeaseState.ACTIVE
        for anchor in (attempt_anchors[(lease.attempt_id, lease.generation)],)
    )
    coordination_authority = (
        CoordinationCommandAuthority(
            state.lifecycle.project.host_epoch,
            state.authority.coordination.task_id,
            state.authority.coordination.host_id,
            state.authority.coordination.lease_id,
            state.authority.coordination.generation,
            state.authority.coordination.expires_at,
        )
        if state.authority.coordination is not None
        and state.authority.coordination.state == CoordinationLeaseState.ACTIVE
        else None
    )

    return LedgerSnapshot(
        revision=str(state.lifecycle.project.revision),
        generation=state.authority.coordination.generation if state.authority.coordination is not None else 0,
        items=work_items,
        attempts=tuple(
            AttemptRecord(
                attempt.attempt_id,
                attempt.item_id,
                attempt.state,
                attempt.accepted_scope_revision,
                attempt.accepted_scope_digest,
                CandidateId(attempt.candidate_revision) if attempt.candidate_revision is not None else None,
                attempt.brief_artifact_ref_id,
            )
            for attempt in state.lifecycle.attempts
        ),
        artifacts=tuple(
            ArtifactRecord(artifact.artifact_ref_id, artifact.kind.value) for artifact in state.artifacts.references
        ),
        proposals=tuple(
            ProposalRecord(proposal.proposal_id, str(proposal.subject_revision))
            for proposal in state.proposals.proposals
            if proposal.disposition is None
        ),
        subject_revisions=tuple(
            SubjectRevision(item.item_id, str(item.subject_revision)) for item in state.lifecycle.work_items
        )
        + tuple(
            SubjectRevision(attempt.attempt_id, str(attempt.subject_revision)) for attempt in state.lifecycle.attempts
        )
        + tuple(
            SubjectRevision(proposal.proposal_id, str(proposal.subject_revision))
            for proposal in state.proposals.proposals
        ),
        attempt_authorities=attempt_authorities,
        command_attempt_authorities=command_attempt_authorities,
        coordination_authority=coordination_authority,
        history_items=tuple(item.item_id for item in state.lifecycle.work_items if _live_state(item.state) is None),
        scopes=scopes,
        planning_impacts=planning_impacts,
        resource_definitions=tuple(
            ResourceDefinition(definition.resource_id, definition.kind) for definition in state.resources.definitions
        ),
        resource_instances=tuple(
            ResourceInstance(
                instance.instance_id,
                instance.resource_id,
                instance.host_id,
                instance.subject_revision,
                instance.discovery_kind,
                instance.discovery_fingerprint,
            )
            for instance in state.resources.instances
            if instance.instance_id in active_instance_ids
        ),
        resource_reservation_counters=tuple(
            ResourceReservationCounter(counter.instance_id, counter.generation_high_water)
            for counter in state.resources.reservation_counters
            if counter.instance_id in active_instance_ids
        ),
        resource_reservations=tuple(
            ResourceReservation(
                reservation.reservation_id,
                reservation.resource_id,
                reservation.instance_id,
                reservation.attempt_id,
                reservation.acquisition_generation,
                reservation.state,
            )
            for reservation in state.resources.reservations
        ),
        resource_use_leases=projected_use_leases,
        resource_observations=tuple(
            ResourceObservation(
                locator.instance_id,
                locator.host_id,
                locator.locator_schema,
                locator.locator,
                locator.observation_generation,
                locator.observation_digest,
                locator.observed_at,
            )
            for locator in state.resources.locators
        ),
        mutation_reservations=tuple(
            MutationReservation(
                reservation.reservation_id,
                reservation.instance_id,
                reservation.resource_id,
                reservation.host_id,
                reservation.acquisition_generation,
                reservation.attempt_id,
                reservation.item_id,
                reservation.state,
                reservation.subject_revision,
            )
            for reservation in state.resources.reservations
        ),
        mutation_use_leases=tuple(
            MutationUseLease(
                use_lease.reservation_id,
                use_lease.instance_id,
                use_lease.reservation_generation,
                use_lease.attempt_id,
                use_lease.host_id,
                use_lease.instance_subject_revision,
                use_lease.observation_generation,
                use_lease.observation_digest,
                use_lease.task_id,
                use_lease.attempt_lease_id,
                use_lease.attempt_lease_generation,
                use_lease.lease_id,
                use_lease.generation,
                use_lease.generation_kind,
                use_lease.host_epoch,
                use_lease.expires_at,
                use_lease.state,
            )
            for use_lease in state.resources.use_leases
        ),
        mutation_intents=tuple(
            MutationIntent(
                intent.intent_id,
                intent.reservation_id,
                intent.reservation_generation,
                intent.instance_id,
                intent.attempt_id,
                intent.host_id,
                intent.resource_use_generation,
                intent.resource_use_lease_id,
                intent.task_id,
                intent.attempt_lease_id,
                intent.attempt_lease_generation,
                intent.start_instance_subject_revision,
                intent.start_observation_generation,
                intent.start_observation_digest,
                intent.policy_schema,
                intent.policy,
                intent.policy_digest,
                intent.state,
                intent.recorded_at,
                intent.resolved_at,
                intent.result_observation_generation,
                intent.result_observation_digest,
                intent.evidence_schema,
                intent.evidence,
                intent.evidence_digest,
                intent.disposition_task_id,
                intent.disposition_reason,
            )
            for intent in state.resources.mutation_intents
        ),
        host_epoch=state.lifecycle.project.host_epoch,
        focus_item=state.focus.item_id,
        focus_attempt=state.focus.attempt_id,
        can_transfer_coordinator=False,
    )
