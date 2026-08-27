from datetime import datetime
from typing import assert_never

from charlie_pinboard.application.stored_state import (
    ItemArtifactLink,
    ItemDependency,
    ProposalEvidence,
    ProposalFreshness,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.authority_models import (
    AttemptLeaseStatus,
    InactiveAttemptAuthority,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from charlie_pinboard.domain.identifiers import AttemptId, CandidateId, ItemId, ProposalId
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.work_models import (
    ArtifactRecord,
    AttemptAuthority,
    AttemptRecord,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    CoordinationLeaseAuthority,
    CoordinationLeaseStatus,
    ItemScope,
    ProposalRecord,
    ProposalRelationKind,
    ScopeAnchor,
    ScopeArtifact,
    ScopeDependency,
    SubjectRevision,
    WorkItem,
    WorkState,
)


def project_inactive_attempt_authority(
    state: StoredWorkState,
    attempt_id: AttemptId,
    now: datetime,
) -> DecisionResult[InactiveAttemptAuthority]:
    """Select exact retained inactive authority after ordinary interruption recovery."""

    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    if attempt is None or lease is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "No retained attempt authority exists.")
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            "The retained attempt generation has no exact identity anchor.",
        )
    if lease.state == AttemptLeaseStatus.ACTIVE:
        if lease.expires_at > now:
            return DecisionFailure(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Attempt authority remains live.")
        status = AttemptLeaseStatus.EXPIRED
    elif lease.state in {AttemptLeaseStatus.RELEASED, AttemptLeaseStatus.REVOKED}:
        status = lease.state
    else:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, "Attempt authority is not inactive.")
    return InactiveAttemptAuthority(
        state.lifecycle.project.host_epoch,
        attempt_id,
        attempt.item_id,
        anchor.task_id,
        anchor.host_id,
        anchor.lease_id,
        anchor.generation,
        lease.expires_at,
        status,
    )


def _dependency_order(value: ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _artifact_order(value: ItemArtifactLink) -> tuple[str, str, int]:
    return str(value.item_id), value.role.value, value.position


def _proposal_evidence_order(value: ProposalEvidence) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_freshness_order(value: ProposalFreshness) -> tuple[str, int]:
    return str(value.proposal_id), value.position


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
    stored_items_by_id = {item.item_id: item for item in state.lifecycle.work_items}
    dependency_groups: dict[ItemId, list[ScopeDependency]] = {item_id: [] for item_id in stored_items_by_id}
    for link in sorted(state.lifecycle.dependencies, key=_dependency_order):
        dependency_groups[link.item_id].append(ScopeDependency(link.position, link.dependency_id))
    dependencies_by_item = {item_id: tuple(values) for item_id, values in dependency_groups.items()}
    artifact_by_id = {artifact.artifact_ref_id: artifact for artifact in state.artifact_references}
    artifact_groups: dict[ItemId, list[ScopeArtifact]] = {item_id: [] for item_id in stored_items_by_id}
    for link in sorted(state.lifecycle.item_artifacts, key=_artifact_order):
        artifact = artifact_by_id[link.artifact_ref_id]
        artifact_groups[link.item_id].append(
            ScopeArtifact(
                link.role,
                link.position,
                artifact.kind.value,
                artifact.key,
                artifact.revision,
                artifact.selector,
                artifact.content_sha256,
            )
        )
    artifacts_by_item = {item_id: tuple(values) for item_id, values in artifact_groups.items()}
    scope_revisions_by_identity = {
        (anchor.item_id, anchor.revision, anchor.digest): anchor for anchor in state.lifecycle.scope_revisions
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
                artifacts_by_item[item.item_id],
            ),
        )
        for item in state.lifecycle.work_items
        if _live_state(item.state) is not None
        for anchor in (scope_revisions_by_identity.get((item.item_id, item.scope_revision, item.scope_digest)),)
        if anchor is not None
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

    attempt_by_id = {attempt.attempt_id: attempt for attempt in state.lifecycle.attempts}
    attempt_anchors = {(anchor.attempt_id, anchor.generation): anchor for anchor in state.authority.attempt_generations}
    attempt_authorities = tuple(
        AttemptAuthority(
            lease.attempt_id,
            attempt_by_id[lease.attempt_id].item_id,
            attempt_anchors[(lease.attempt_id, lease.generation)].lease_id
            if lease.state == AttemptLeaseStatus.ACTIVE
            else None,
            lease.generation,
        )
        for lease in state.authority.attempt_leases
    )
    command_attempt_authorities = tuple(
        CommandAttemptAuthority(
            state.lifecycle.project.host_epoch,
            attempt_by_id[lease.attempt_id].item_id,
            str(stored_items_by_id[attempt_by_id[lease.attempt_id].item_id].subject_revision),
            lease.attempt_id,
            str(attempt_by_id[lease.attempt_id].subject_revision),
            anchor.task_id,
            anchor.host_id,
            anchor.lease_id,
            lease.generation,
            lease.expires_at,
        )
        for lease in state.authority.attempt_leases
        if lease.state == AttemptLeaseStatus.ACTIVE
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
        and state.authority.coordination.state == CoordinationLeaseStatus.ACTIVE
        else None
    )
    proposal_ids = tuple(proposal.proposal_id for proposal in state.proposals.proposals)
    evidence_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for evidence in sorted(state.proposals.evidence, key=_proposal_evidence_order):
        evidence_groups[evidence.proposal_id].append(evidence.selector)
    evidence_by_proposal = {proposal_id: tuple(values) for proposal_id, values in evidence_groups.items()}
    freshness_groups: dict[ProposalId, list[str]] = {proposal_id: [] for proposal_id in proposal_ids}
    for freshness in sorted(state.proposals.freshness, key=_proposal_freshness_order):
        freshness_groups[freshness.proposal_id].append(freshness.assumption)
    freshness_by_proposal = {proposal_id: tuple(values) for proposal_id, values in freshness_groups.items()}

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
            ArtifactRecord(artifact.artifact_ref_id, artifact.kind.value) for artifact in state.artifact_references
        ),
        proposals=tuple(
            ProposalRecord(
                proposal.proposal_id,
                str(proposal.subject_revision),
                proposal.created_at,
                proposal.source_task_id,
                proposal.user_label,
                proposal.trigger,
                proposal.why_it_matters,
                ProposalRelationKind(proposal.relation.value),
                proposal.relation_item_id,
                proposal.effect,
                proposal.unlock,
                proposal.urgency_evidence,
                evidence_by_proposal.get(proposal.proposal_id, ()),
                freshness_by_proposal.get(proposal.proposal_id, ()),
            )
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
        host_epoch=state.lifecycle.project.host_epoch,
        focus_item=state.focus.item_id,
        focus_attempt=state.focus.attempt_id,
        can_transfer_coordinator=coordination_authority is not None,
        coordination_lease=(
            CoordinationLeaseAuthority(
                state.lifecycle.project.host_epoch,
                coordination.task_id,
                coordination.host_id,
                coordination.lease_id,
                coordination.generation,
                coordination.acquired_at,
                coordination.expires_at,
                CoordinationLeaseStatus(coordination.state.value),
            )
            if (coordination := state.authority.coordination) is not None
            else None
        ),
    )
