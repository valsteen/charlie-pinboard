import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import StorageError, initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.mutations import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    DependencyEditMutation,
    PlanningImpactMutation,
    PlanningResolutionMutation,
    ProposalCreationMutation,
    ReservationTaskUseMutation,
    ResourceIntentMutation,
    ResourceMutation,
    ResourceRequirementEditMutation,
)
from charlie_pinboard.application.ports import WorkStore, WorkTransaction
from charlie_pinboard.application.stored_state import (
    CanonicalJson,
    HistoryRecords,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    OriginKind,
    ProposalDisposition,
    ProposalRecords,
    ProposalRelation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredResourceDefinition,
    StoredTransitionReceipt,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.decisions import (
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Role,
    TransitionReceipt,
    available_actions,
    decide,
)
from charlie_pinboard.domain.errors import DecisionError
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HistoryId,
    HistorySubjectId,
    ItemId,
    LeaseId,
    ProposalId,
    ResourceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AcceptProposalInput,
    ActivateInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    MergeProposalInput,
    ReasonInput,
    SubmitReviewInput,
    Timing,
)
from charlie_pinboard.domain.planning_decisions import PlanningResolutionDecision
from charlie_pinboard.domain.resource_decisions import (
    IntentDecisionKind,
    MutationIntentChange,
    ResourceDecision,
    ResourceDecisionKind,
    ResourceIntentDecision,
)
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


class MutationPersistenceTest(unittest.TestCase):
    def _store(self) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        return store

    def _store_pair(self) -> tuple[SQLiteWorkStore, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        first = SQLiteWorkStore(roots.database_path)
        first.initialize_state(complete_sqlite_state())
        return first, SQLiteWorkStore(roots.database_path)

    def _receipt_state(self, before: StoredWorkState, action: str) -> tuple[TransitionReceipt, StoredWorkState]:
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        receipt = TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
        stored_receipt = StoredTransitionReceipt(
            HistoryId(2),
            13,
            receipt.action_id,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId("work-a"),
            None,
            TransitionHistoryAuthorizationKind.COORDINATOR,
            None,
            None,
            "mutation/v1",
            CanonicalJson(b"{}"),
            "mutation-receipt/v1",
            CanonicalJson(
                json.dumps(
                    {"evidence": receipt.evidence, "outcome": receipt.outcome},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            decided_at,
        )
        after = replace(
            before,
            lifecycle=replace(
                before.lifecycle,
                project=replace(before.lifecycle.project, revision=13, updated_at=decided_at),
            ),
            history=HistoryRecords((*before.history.receipts, stored_receipt)),
        )
        return receipt, after

    def test_activation_decision_retains_and_persists_creation_facts(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACTIVATE)
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        decision = decide(
            snapshot,
            action,
            ActivateInput(
                AttemptId("work-c-1"),
                "codex/work-c",
                "base-c",
                "worker-c",
                ArtifactRefId(1),
            ),
            decided_at,
        )

        assert decision.attempt_change is not None
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (
                decision.attempt_change.branch,
                decision.attempt_change.base_revision,
                decision.attempt_change.owner,
                decision.attempt_change.brief_artifact_ref_id,
            ),
        )
        with store.write() as transaction:
            transaction.commit(decision)

        reopened = store.snapshot()
        attempt = next(value for value in reopened.lifecycle.attempts if value.attempt_id == AttemptId("work-c-1"))
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (attempt.branch, attempt.base_revision, attempt.provenance, attempt.brief_artifact_ref_id),
        )
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.history.receipts))

    def test_proposal_acceptance_round_trips_semantics_and_ordered_dependencies(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACCEPT_PROPOSAL)
        decision = decide(
            snapshot,
            action,
            AcceptProposalInput(
                ItemId("accepted-proposal"),
                AcceptedProposalState.READY,
                "activate",
                timing=None,
                depends_on=(ItemId("work-c"), ItemId("legacy-work")),
                resource_requirements=(ResourceId("workspace"),),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )

        self.assertIsNotNone(decision.proposal_change)
        with store.write() as transaction:
            transaction.commit(decision)

        reopened = store.snapshot()
        item = next(value for value in reopened.lifecycle.work_items if value.item_id == ItemId("accepted-proposal"))
        proposal = reopened.proposals.proposals[0]
        self.assertEqual(
            ("Proposal A", "A related observation", "Record the follow-up."),
            (item.user_label, item.trigger, item.effect),
        )
        self.assertEqual(StoredWorkItemState.READY, item.state)
        self.assertEqual(
            (ItemId("work-c"), ItemId("legacy-work")),
            tuple(value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == item.item_id),
        )
        self.assertEqual(
            (ResourceId("workspace"),),
            tuple(value.resource_id for value in reopened.resources.requirements if value.item_id == item.item_id),
        )
        self.assertEqual(ProposalDisposition.ACCEPTED, proposal.disposition)
        self.assertEqual(ItemId("accepted-proposal"), proposal.disposition_target_item_id)
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.history.receipts))

    def test_closed_mutation_contract_routes_every_accepted_family_and_rejects_stale_replay(self) -> None:
        constructors = (
            ProposalCreationMutation,
            DependencyEditMutation,
            ResourceRequirementEditMutation,
            CoordinationAuthorityMutation,
            AttemptAuthorityMutation,
            ReservationTaskUseMutation,
        )
        for constructor in constructors:
            with self.subTest(family=constructor.__name__):
                store = self._store()
                before = store.snapshot()
                receipt, after = self._receipt_state(before, f"inspect:{constructor.__name__}")
                mutation = constructor(before, after, receipt)
                with store.write() as transaction:
                    self.assertEqual(receipt, transaction.commit(mutation))
                self.assertEqual(after, store.snapshot())
                with self.assertRaises(DecisionError), store.write() as transaction:
                    transaction.commit(mutation)
                self.assertEqual(after, store.snapshot())

        for family in ("planning-impact", "planning-resolution", "resource", "resource-intent"):
            with self.subTest(family=family):
                store = self._store()
                before = store.snapshot()
                receipt, after = self._receipt_state(before, f"inspect:{family}")
                snapshot = project_decision_snapshot(before)
                impact = snapshot.planning_impacts[0]
                intent = snapshot.mutation_intents[0]
                if family == "planning-impact":
                    mutation = PlanningImpactMutation(impact, before, after, receipt)
                elif family == "planning-resolution":
                    mutation = PlanningResolutionMutation(
                        PlanningResolutionDecision(impact, None, None), before, after, receipt
                    )
                elif family == "resource":
                    mutation = ResourceMutation(
                        ResourceDecision(ResourceDecisionKind.RELEASE, ()), before, after, receipt
                    )
                else:
                    mutation = ResourceIntentMutation(
                        ResourceIntentDecision(
                            IntentDecisionKind.ABANDON,
                            MutationIntentChange(intent, intent),
                        ),
                        before,
                        after,
                        receipt,
                    )
                with store.write() as transaction:
                    transaction.commit(mutation)
                self.assertEqual(after, store.snapshot())
                with self.assertRaises(DecisionError), store.write() as transaction:
                    transaction.commit(mutation)
                self.assertEqual(after, store.snapshot())

    def test_stored_mutation_rejects_invalid_revision_and_rolls_back(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:invalid")
        invalid = replace(
            after,
            lifecycle=replace(
                after.lifecycle,
                project=replace(after.lifecycle.project, revision=14),
            ),
        )
        mutation = DependencyEditMutation(before, invalid, receipt)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(before, store.snapshot())

        receipt, after = self._receipt_state(before, "inspect:constraint-failure")
        items = list(after.lifecycle.work_items)
        items[0] = replace(items[0], scope_digest="invalid")
        constrained = replace(after, lifecycle=replace(after.lifecycle, work_items=tuple(items)))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(DependencyEditMutation(before, constrained, receipt))
        self.assertEqual(before, store.snapshot())

    def test_two_writers_reject_the_stale_typed_mutation_without_partial_state(self) -> None:
        first, second = self._store_pair()
        before = first.snapshot()
        self.assertEqual(before, second.snapshot())
        receipt, after = self._receipt_state(before, "inspect:first-writer")
        first_mutation = CoordinationAuthorityMutation(before, after, receipt)
        with first.write() as transaction:
            transaction.commit(first_mutation)

        stale_receipt, stale_after = self._receipt_state(before, "inspect:stale-writer")
        stale_mutation = CoordinationAuthorityMutation(before, stale_after, stale_receipt)
        with self.assertRaises(DecisionError), second.write() as transaction:
            transaction.commit(stale_mutation)
        self.assertEqual(after, second.snapshot())

    def test_carriers_round_trip_creation_and_ordered_relational_facts(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:proposal-create")
        proposal = StoredProposal(
            ProposalId("proposal-b"),
            OriginKind.NATIVE,
            receipt.decided_at,
            receipt.decided_at,
            TaskId("source-b"),
            "Proposal B",
            "A new observation",
            "The queue needs the observation.",
            ProposalRelation.INDEPENDENT,
            None,
            "Preserve the proposal.",
            "A coordinator can assess it.",
            "Scheduling remains unchanged.",
            None,
            None,
            None,
            13,
            None,
            None,
        )
        after = replace(after, proposals=ProposalRecords((*before.proposals.proposals, proposal)))
        with store.write() as transaction:
            transaction.commit(ProposalCreationMutation(before, after, receipt))
        self.assertEqual(proposal, store.snapshot().proposals.proposals[-1])

        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:dependency-edit")
        digest = "b" * 64
        items = tuple(
            replace(
                item,
                scope_revision=2,
                scope_digest=digest,
                subject_revision=13,
                origin_updated_at=receipt.decided_at,
                updated_at=receipt.decided_at,
            )
            if item.item_id == ItemId("work-c")
            else item
            for item in before.lifecycle.work_items
        )
        dependencies = (
            *before.lifecycle.dependencies,
            ItemDependency(ItemId("work-c"), ItemId("legacy-work"), 0),
            ItemDependency(ItemId("work-c"), ItemId("work-b"), 1),
        )
        after = replace(
            after,
            lifecycle=replace(
                after.lifecycle,
                work_items=items,
                scope_revisions=(
                    *before.lifecycle.scope_revisions,
                    ItemScopeRevision(ItemId("work-c"), 2, digest, 13, receipt.decided_at),
                ),
                dependencies=dependencies,
            ),
        )
        with store.write() as transaction:
            transaction.commit(DependencyEditMutation(before, after, receipt))
        reopened = store.snapshot()
        self.assertEqual(
            (ItemId("legacy-work"), ItemId("work-b")),
            tuple(
                value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == ItemId("work-c")
            ),
        )

        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:requirement-edit")
        digest = "c" * 64
        items = tuple(
            replace(
                item,
                scope_revision=2,
                scope_digest=digest,
                subject_revision=13,
                origin_updated_at=receipt.decided_at,
                updated_at=receipt.decided_at,
            )
            if item.item_id == ItemId("work-c")
            else item
            for item in before.lifecycle.work_items
        )
        gpu = StoredResourceDefinition(
            ResourceId("gpu"),
            OriginKind.NATIVE,
            "gpu",
            "One exclusive GPU",
            13,
            receipt.decided_at,
            receipt.decided_at,
            receipt.decided_at,
            receipt.decided_at,
        )
        after = replace(
            after,
            lifecycle=replace(
                after.lifecycle,
                work_items=items,
                scope_revisions=(
                    *before.lifecycle.scope_revisions,
                    ItemScopeRevision(ItemId("work-c"), 2, digest, 13, receipt.decided_at),
                ),
            ),
            resources=replace(
                before.resources,
                definitions=(gpu, *before.resources.definitions),
                requirements=(
                    *before.resources.requirements,
                    ItemResourceRequirement(ItemId("work-c"), ResourceId("workspace"), 0),
                    ItemResourceRequirement(ItemId("work-c"), ResourceId("gpu"), 1),
                ),
            ),
        )
        with store.write() as transaction:
            transaction.commit(ResourceRequirementEditMutation(before, after, receipt))
        reopened = store.snapshot()
        self.assertEqual(
            (ResourceId("workspace"), ResourceId("gpu")),
            tuple(value.resource_id for value in reopened.resources.requirements if value.item_id == ItemId("work-c")),
        )

    def test_planning_replacements_and_resource_intent_evidence_persist_exactly(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:planning-resolution")
        replacement = StoredPlanningReplacement(
            before.planning.impacts[0].impact_id,
            ItemId("work-b"),
            ItemId("legacy-work"),
            1,
        )
        after = replace(
            after, planning=replace(before.planning, replacements=(*before.planning.replacements, replacement))
        )
        impact = project_decision_snapshot(before).planning_impacts[0]
        obligation = replace(impact.obligations[0], replacements=(ItemId("work-c"), ItemId("legacy-work")))
        decision = PlanningResolutionDecision(replace(impact, obligations=(obligation,)), None, None)
        with store.write() as transaction:
            transaction.commit(PlanningResolutionMutation(decision, before, after, receipt))
        self.assertEqual(
            (ItemId("work-c"), ItemId("legacy-work")),
            tuple(value.replacement_item_id for value in store.snapshot().planning.replacements),
        )

        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:resource-intent")
        stored_intent = before.resources.mutation_intents[0]
        abandoned = replace(
            stored_intent,
            state=stored_intent.state.ABANDONED,
            resolved_at=receipt.decided_at,
            disposition_task_id=TaskId("worker"),
            disposition_reason="The observed resource remained unchanged.",
        )
        after = replace(after, resources=replace(before.resources, mutation_intents=(abandoned,)))
        intent = project_decision_snapshot(before).mutation_intents[0]
        intent_after = replace(
            intent,
            state=intent.state.ABANDONED,
            resolved_at=receipt.decided_at,
            disposition_task_id=TaskId("worker"),
            disposition_reason="The observed resource remained unchanged.",
        )
        decision = ResourceIntentDecision(IntentDecisionKind.ABANDON, MutationIntentChange(intent, intent_after))
        with store.write() as transaction:
            transaction.commit(ResourceIntentMutation(decision, before, after, receipt))
        reopened = store.snapshot().resources.mutation_intents[0]
        self.assertEqual(("abandoned", TaskId("worker")), (reopened.state.value, reopened.disposition_task_id))
        self.assertEqual(SQLITE_DIGEST, reopened.start_observation_digest)

    def test_proposal_disposition_matrix_persists_each_closed_value(self) -> None:
        for kind, payload, expected, target, reason in (
            (ActionKind.MERGE_PROPOSAL, MergeProposalInput(ItemId("work-c")), "merged", ItemId("work-c"), None),
            (
                ActionKind.RETURN_PROPOSAL,
                ReasonInput("Clarify the evidence."),
                "returned",
                None,
                "Clarify the evidence.",
            ),
            (
                ActionKind.REJECT_PROPOSAL,
                ReasonInput("The finding is obsolete."),
                "rejected",
                None,
                "The finding is obsolete.",
            ),
        ):
            with self.subTest(kind=kind):
                store = self._store()
                snapshot = project_decision_snapshot(store.snapshot())
                actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
                action = next(value for value in available_actions(snapshot, actor) if value.kind == kind)
                decision = decide(snapshot, action, payload, SQLITE_NOW + timedelta(seconds=1))
                with store.write() as transaction:
                    transaction.commit(decision)
                proposal = store.snapshot().proposals.proposals[0]
                assert proposal.disposition is not None
                self.assertEqual(
                    (expected, target, reason),
                    (proposal.disposition.value, proposal.disposition_target_item_id, proposal.disposition_reason),
                )

    def test_lifecycle_writer_rejects_incomplete_or_invalid_typed_facts_atomically(self) -> None:
        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        close_action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == ActionKind.CLOSE and value.subject == ItemId("legacy-work")
        )
        close = decide(
            snapshot,
            close_action,
            CloseInput(CloseOutcome.DROPPED, "The legacy intake is no longer needed."),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(close)
        self.assertEqual(StoredWorkItemState.DROPPED, store.snapshot().lifecycle.work_items[0].state)

        for outcome in ("active", "not-a-state"):
            store = self._store()
            before = store.snapshot()
            snapshot = project_decision_snapshot(before)
            actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
            close_action = next(
                value
                for value in available_actions(snapshot, actor)
                if value.kind == ActionKind.CLOSE and value.subject == ItemId("legacy-work")
            )
            close = decide(
                snapshot,
                close_action,
                CloseInput(CloseOutcome.DROPPED, "The legacy intake is no longer needed."),
                SQLITE_NOW + timedelta(seconds=1),
            )
            invalid = replace(close, receipt=replace(close.receipt, outcome=outcome))
            with self.subTest(outcome=outcome), self.assertRaises(StorageError), store.write() as transaction:
                transaction.commit(invalid)
            self.assertEqual(before, store.snapshot())

        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        activation_action = next(
            value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACTIVATE
        )
        activation = decide(
            snapshot,
            activation_action,
            ActivateInput(AttemptId("work-c-1"), "branch", "base", "owner", ArtifactRefId(1)),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert activation.attempt_change is not None
        incomplete = replace(activation, attempt_change=replace(activation.attempt_change, branch=None))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(incomplete)
        self.assertEqual(before, store.snapshot())

        pause_action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.PAUSE)
        pause = decide(
            snapshot,
            pause_action,
            ReasonInput("Pause for an invalid-attempt-shape probe."),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert pause.attempt_change is not None
        missing_attempt_state = replace(pause, attempt_change=replace(pause.attempt_change, after=None))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(missing_attempt_state)
        self.assertEqual(before, store.snapshot())

        worker = ActorAuthority(
            Role.WORKER,
            AuthorizationKind.ATTEMPT,
            3,
            LeaseId("attempt-lease-a"),
            (AttemptId("work-a-1"),),
            False,
        )
        review_action = next(
            value for value in available_actions(snapshot, worker) if value.kind == ActionKind.SUBMIT_REVIEW
        )
        review = decide(
            snapshot,
            review_action,
            SubmitReviewInput(CandidateId("candidate")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert review.attempt_change is not None
        missing_candidate = replace(
            review,
            attempt_change=replace(review.attempt_change, protected_candidate_after=None),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(missing_candidate)
        self.assertEqual(before, store.snapshot())

    def test_application_port_contract_is_importable_at_the_composition_boundary(self) -> None:
        self.assertTrue(hasattr(WorkTransaction, "commit"))
        self.assertTrue(hasattr(WorkStore, "write"))

    def test_mutation_envelope_and_creation_fact_rejections_are_atomic(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:missing-history")
        missing_history = replace(after, history=before.history)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(DependencyEditMutation(before, missing_history, receipt))
        self.assertEqual(before, store.snapshot())

        wrong_receipt = replace(after.history.receipts[-1], history_id=HistoryId(9))
        mismatched_history = replace(after, history=HistoryRecords((*before.history.receipts, wrong_receipt)))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(DependencyEditMutation(before, mismatched_history, receipt))
        self.assertEqual(before, store.snapshot())

        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACCEPT_PROPOSAL)
        accepted = decide(
            snapshot,
            action,
            AcceptProposalInput(ItemId("incomplete-item"), AcceptedProposalState.READY, "activate"),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert accepted.proposal_change is not None
        incomplete = replace(
            accepted,
            proposal_change=replace(accepted.proposal_change, accepted_item=None),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(incomplete)
        self.assertEqual(before, store.snapshot())

    def test_defer_decision_persists_reopen_focus(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == ActionKind.DEFER and value.subject == ItemId("legacy-work")
        )
        decision = decide(
            snapshot,
            action,
            DeferInput(Timing.SAFE_TO_DEFER, "Reopen when the prerequisite is accepted."),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(decision)
        reopened = store.snapshot()
        self.assertEqual(StoredWorkItemState.DEFERRED, reopened.lifecycle.work_items[0].state)
        self.assertEqual("reopen", reopened.focus.next_action)

    def test_row_level_staleness_rejects_after_revision_tokens_are_refreshed(self) -> None:
        for kind, payload in (
            (ActionKind.PAUSE, ReasonInput("Pause once.")),
            (ActionKind.MERGE_PROPOSAL, MergeProposalInput(ItemId("work-c"))),
        ):
            with self.subTest(kind=kind):
                store = self._store()
                snapshot = project_decision_snapshot(store.snapshot())
                actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
                action = next(value for value in available_actions(snapshot, actor) if value.kind == kind)
                decision = decide(snapshot, action, payload, SQLITE_NOW + timedelta(seconds=1))
                with store.write() as transaction:
                    transaction.commit(decision)
                committed = store.snapshot()
                refreshed = replace(
                    decision,
                    action=replace(decision.action, expected_revision="13", subject_revision="13"),
                )
                with self.assertRaises(DecisionError), store.write() as transaction:
                    transaction.commit(refreshed)
                self.assertEqual(committed, store.snapshot())


if __name__ == "__main__":
    unittest.main()
