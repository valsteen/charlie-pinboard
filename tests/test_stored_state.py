import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    ArtifactRecords,
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    AuthorityRecords,
    CanonicalJson,
    CoordinationLeaseState,
    HistoryRecords,
    ItemArtifactLink,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    LifecycleRecords,
    MutationIntentState,
    OriginKind,
    PlanningObligationState,
    PlanningRecords,
    ProjectRecord,
    ProposalDisposition,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    ProposalRelation,
    ResourceInstanceLocator,
    ResourceInstanceState,
    ResourceMutationIntent,
    ResourceRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredReservationCounter,
    StoredResourceDefinition,
    StoredResourceInstance,
    StoredResourceReservation,
    StoredResourceUseLease,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
    UseLeaseGenerationKind,
)
from charlie_pinboard.domain.decisions import ActionKind, AuthorizationKind
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    PlanningDisposition,
    ReservationState,
    Timing,
    UseLeaseState,
)

NOW = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _work_item(item_id: ItemId, state: StoredWorkItemState, *, attempt: bool) -> StoredWorkItem:
    return StoredWorkItem(
        item_id=item_id,
        origin=OriginKind.NATIVE,
        user_label=f"Work {item_id}",
        state=state,
        timing=Timing.MUST_NOW,
        source="accepted requirement",
        trigger="A verified observation",
        why_it_matters="The workflow needs this fact.",
        effect="The state becomes explicit.",
        unlock="The next decision can run.",
        outcome_evidence=None,
        next_action="continue" if attempt else "activate",
        notes="Current work remains bounded.",
        scope_revision=1,
        scope_digest=DIGEST,
        subject_revision=7,
        origin_created_at=NOW,
        origin_updated_at=NOW,
        recorded_at=NOW,
        updated_at=NOW,
    )


class StoredWorkStateTest(unittest.TestCase):
    def test_complete_persistence_aggregate_projects_only_workflow_decision_facts(self) -> None:
        item_a = ItemId("work-a")
        item_b = ItemId("work-b")
        attempt_id = AttemptId("work-a-1")
        resource_id = ResourceId("workspace")
        instance_id = ResourceInstanceId("workspace-on-host")
        retired_instance_id = ResourceInstanceId("retired-workspace-on-host")
        reservation_id = ReservationId("reservation-a")
        attempt_lease_id = LeaseId("attempt-lease-a")
        use_lease_id = LeaseId("use-lease-a")
        artifact_brief = ArtifactReference(
            ArtifactRefId(1), "work-a-brief", 1, ArtifactKind.BRIEF, "artifacts/brief.md", DIGEST, 100, 3, NOW
        )
        artifact_design = ArtifactReference(
            ArtifactRefId(2), "work-a-design", 1, ArtifactKind.DESIGN, "artifacts/design.md", DIGEST, 200, 3, NOW
        )
        artifact_evidence = ArtifactReference(
            ArtifactRefId(3), "work-a-evidence", 1, ArtifactKind.EVIDENCE, "artifacts/evidence.md", DIGEST, 50, 4, NOW
        )
        lifecycle = LifecycleRecords(
            project=ProjectRecord("charlie-pinboard", 1, 12, 2, NOW, NOW),
            work_items=(
                _work_item(item_a, StoredWorkItemState.ACTIVE, attempt=True),
                _work_item(item_b, StoredWorkItemState.READY, attempt=False),
            ),
            scope_revisions=(
                ItemScopeRevision(item_a, 1, DIGEST, 3, NOW),
                ItemScopeRevision(item_b, 1, DIGEST, 3, NOW),
            ),
            dependencies=(ItemDependency(item_a, item_b, 0),),
            item_artifacts=(ItemArtifactLink(item_a, artifact_design.artifact_ref_id, ArtifactRole.DESIGN, 0),),
            attempts=(
                StoredAttempt(
                    attempt_id,
                    item_a,
                    OriginKind.NATIVE,
                    AttemptState.ACTIVE,
                    "codex/work-a",
                    "base-revision",
                    "source-task",
                    artifact_brief.artifact_ref_id,
                    None,
                    None,
                    None,
                    None,
                    1,
                    DIGEST,
                    8,
                    NOW,
                    NOW,
                    NOW,
                    NOW,
                ),
            ),
        )
        proposals = ProposalRecords(
            proposals=(
                StoredProposal(
                    ProposalId("proposal-a"),
                    OriginKind.NATIVE,
                    NOW,
                    NOW,
                    TaskId("source-task"),
                    "Proposal A",
                    "A related observation",
                    "It may affect work B.",
                    ProposalRelation.FOLLOW_UP,
                    item_b,
                    "Record the follow-up.",
                    "A later coordinator can assess it.",
                    "No immediate scheduling impact.",
                    None,
                    None,
                    None,
                    4,
                    None,
                    None,
                ),
                StoredProposal(
                    ProposalId("proposal-done"),
                    OriginKind.NATIVE,
                    NOW,
                    NOW,
                    TaskId("source-task"),
                    "Disposed proposal",
                    "Already handled",
                    "It is retained as evidence.",
                    ProposalRelation.DUPLICATE,
                    item_a,
                    "No new work.",
                    "The duplicate is explicit.",
                    "Already decided.",
                    ProposalDisposition.MERGED,
                    item_a,
                    "Same accepted work.",
                    5,
                    NOW,
                    NOW,
                ),
            ),
            evidence=(ProposalEvidence(ProposalId("proposal-a"), 0, "evidence:observation"),),
            freshness=(ProposalFreshness(ProposalId("proposal-a"), 0, "Work B remains live."),),
        )
        impact_id = PlanningImpactId("impact-a")
        planning = PlanningRecords(
            impacts=(
                StoredPlanningImpact(impact_id, item_a, attempt_id, 1, DIGEST, item_b, "Impact", "Evidence", 6, NOW),
            ),
            obligations=(
                StoredPlanningObligation(
                    impact_id,
                    item_b,
                    0,
                    1,
                    DIGEST,
                    PlanningObligationState.RESOLVED,
                    PlanningDisposition.SUPERSEDED,
                    1,
                    DIGEST,
                    None,
                    None,
                    item_a,
                    "work-b was superseded",
                    "The accepted work absorbs it.",
                    7,
                    NOW,
                    NOW,
                ),
            ),
            replacements=(StoredPlanningReplacement(impact_id, item_b, item_a, 0),),
        )
        coordination = StoredCoordinationLease(
            LeaseId("coordination-a"),
            TaskId("coordinator"),
            HostId("host-a"),
            9,
            NOW,
            NOW + timedelta(minutes=5),
            CoordinationLeaseState.ACTIVE,
        )
        authority = AuthorityRecords(
            coordination=coordination,
            attempt_counters=(AttemptLeaseCounter(attempt_id, 3),),
            attempt_generations=(
                AttemptLeaseGeneration(attempt_id, 3, attempt_lease_id, TaskId("worker"), HostId("host-a")),
            ),
            attempt_leases=(
                StoredAttemptLease(attempt_id, 3, NOW, NOW + timedelta(minutes=5), AttemptLeaseState.ACTIVE),
            ),
        )
        grant = StoredResourceUseLease(
            reservation_id,
            instance_id,
            1,
            attempt_id,
            HostId("host-a"),
            4,
            2,
            DIGEST,
            TaskId("worker"),
            attempt_lease_id,
            3,
            use_lease_id,
            1,
            UseLeaseGenerationKind.GRANT,
            2,
            NOW,
            NOW + timedelta(minutes=5),
            UseLeaseState.REVOKED,
        )
        fence = replace(
            grant,
            lease_id=LeaseId("use-fence"),
            generation=2,
            generation_kind=UseLeaseGenerationKind.FENCE,
            state=UseLeaseState.REVOKED,
        )
        successor_grant = replace(
            grant,
            lease_id=LeaseId("use-successor"),
            generation=3,
            state=UseLeaseState.ACTIVE,
        )
        resources = ResourceRecords(
            definitions=(
                StoredResourceDefinition(
                    resource_id, OriginKind.NATIVE, "git-checkout", "One workspace", 3, NOW, NOW, NOW, NOW
                ),
            ),
            requirements=(ItemResourceRequirement(item_a, resource_id, 0),),
            instances=(
                StoredResourceInstance(
                    instance_id,
                    resource_id,
                    HostId("host-a"),
                    "git-worktree",
                    "fingerprint",
                    ResourceInstanceState.ACTIVE,
                    4,
                    NOW,
                    NOW,
                ),
                StoredResourceInstance(
                    retired_instance_id,
                    resource_id,
                    HostId("host-a"),
                    "git-worktree",
                    "retired-fingerprint",
                    ResourceInstanceState.RETIRED,
                    3,
                    NOW,
                    NOW,
                ),
            ),
            locators=(
                ResourceInstanceLocator(
                    instance_id, HostId("host-a"), "git-worktree/v1", CanonicalJson(b"{}"), 2, DIGEST, NOW
                ),
                ResourceInstanceLocator(
                    retired_instance_id,
                    HostId("host-a"),
                    "git-worktree/v1",
                    CanonicalJson(b"{}"),
                    1,
                    DIGEST,
                    NOW,
                ),
            ),
            reservation_counters=(
                StoredReservationCounter(instance_id, 1),
                StoredReservationCounter(retired_instance_id, 0),
            ),
            reservations=(
                StoredResourceReservation(
                    reservation_id,
                    instance_id,
                    resource_id,
                    HostId("host-a"),
                    1,
                    attempt_id,
                    item_a,
                    ReservationState.ACTIVE,
                    5,
                    NOW,
                    None,
                ),
            ),
            use_leases=(grant, fence, successor_grant),
            mutation_intents=(
                ResourceMutationIntent(
                    MutationIntentId("intent-a"),
                    reservation_id,
                    1,
                    instance_id,
                    attempt_id,
                    HostId("host-a"),
                    1,
                    use_lease_id,
                    TaskId("worker"),
                    attempt_lease_id,
                    3,
                    4,
                    2,
                    DIGEST,
                    "mutation-policy/v1",
                    CanonicalJson(b"{}"),
                    DIGEST,
                    MutationIntentState.PLANNED,
                    NOW,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ),
        )
        history = HistoryRecords(
            receipts=(
                StoredTransitionReceipt(
                    HistoryId(1),
                    11,
                    ActionId("continue:work-a-1"),
                    TransitionHistoryActionKind.CONTINUE,
                    HistorySubjectId("work-a-1"),
                    artifact_evidence.artifact_ref_id,
                    TransitionHistoryAuthorizationKind.ATTEMPT,
                    TaskId("worker"),
                    HostId("host-a"),
                    "empty/v1",
                    CanonicalJson(b"{}"),
                    "continued/v1",
                    CanonicalJson(b"{}"),
                    NOW,
                ),
            ),
        )
        state = StoredWorkState(
            lifecycle,
            proposals,
            planning,
            ArtifactRecords((artifact_brief, artifact_design, artifact_evidence)),
            authority,
            resources,
            history,
            StoredFocus(item_a, attempt_id, "continue", 6),
        )

        snapshot = project_decision_snapshot(state)

        self.assertEqual((item_b,), snapshot.items_by_id()[item_a].depends_on)
        self.assertEqual(ArtifactRole.DESIGN, snapshot.scopes[0].scope.artifacts[0].role)
        self.assertEqual((ProposalId("proposal-a"),), tuple(value.proposal for value in snapshot.proposals))
        self.assertEqual((item_a,), snapshot.planning_impacts[0].obligations[0].replacements)
        self.assertEqual(1, snapshot.resource_reservations[0].generation)
        self.assertEqual(1, snapshot.resource_reservation_counters[0].generation_high_water)
        self.assertEqual(LeaseId("use-successor"), snapshot.attempt_authorities[0].resources[0].lease_id)
        self.assertEqual((instance_id,), tuple(value.instance_id for value in snapshot.resource_instances))
        self.assertEqual((instance_id,), tuple(value.instance_id for value in snapshot.resource_reservation_counters))
        self.assertEqual(
            (UseLeaseGenerationKind.GRANT, UseLeaseGenerationKind.FENCE, UseLeaseGenerationKind.GRANT),
            tuple(value.generation_kind for value in snapshot.resource_use_leases),
        )
        self.assertEqual((item_a, attempt_id), (snapshot.focus_item, snapshot.focus_attempt))
        self.assertEqual(
            (), tuple(value for value in snapshot.proposals if value.proposal == ProposalId("proposal-done"))
        )
        self.assertEqual(MutationIntentState.PLANNED, state.resources.mutation_intents[0].state)
        self.assertEqual(
            {value.value for value in ActionKind} | {"legacy-import", "legacy-cleanup"},
            {value.value for value in TransitionHistoryActionKind},
        )
        self.assertEqual(
            {value.value for value in AuthorizationKind} | {"migration"},
            {value.value for value in TransitionHistoryAuthorizationKind},
        )

    def test_terminal_work_remains_history_without_entering_live_decisions(self) -> None:
        item_id = ItemId("finished")
        finished = _work_item(item_id, StoredWorkItemState.DONE, attempt=False)
        state = StoredWorkState(
            LifecycleRecords(ProjectRecord("charlie-pinboard", 1, 2, 1, NOW, NOW), (finished,)),
            ProposalRecords(),
            PlanningRecords(),
            ArtifactRecords(),
            AuthorityRecords(),
            ResourceRecords(),
            HistoryRecords(),
            StoredFocus(None, None, "select", 1),
        )

        snapshot = project_decision_snapshot(state)

        self.assertEqual((), snapshot.items)
        self.assertEqual((item_id,), snapshot.history_items)


if __name__ == "__main__":
    unittest.main()
