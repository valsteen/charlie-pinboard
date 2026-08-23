import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.service import (
    advance_resource_observation,
    execute,
    record_planning_impact,
    register_mutation_intent,
    resolve_planning_obligation,
)
from charlie_pinboard.application.stored_state import PlanningRecords
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Role,
    available_actions,
    bind_transition,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import planning_impact_outcome, planning_resolution_outcome
from charlie_pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, MutationIntentId, PlanningImpactId
from charlie_pinboard.domain.model import (
    CanonicalJson,
    EvidenceInput,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    ReasonInput,
    ResourceIntentCapability,
)
from charlie_pinboard.domain.resource_decisions import (
    AdvanceResourceObservationInput,
    ObservedResource,
    RegisterMutationIntentInput,
    ResolverEvidenceDecision,
)
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


class ServiceTest(unittest.TestCase):
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
        state = complete_sqlite_state()
        first.initialize_state(
            replace(
                state,
                planning=PlanningRecords(),
                resources=replace(state.resources, mutation_intents=()),
            )
        )
        return first, SQLiteWorkStore(roots.database_path)

    def _coordinator_action(self, store: SQLiteWorkStore, kind: ActionKind) -> Action:
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.coordination_authority
        assert authority is not None
        actor = ActorAuthority(
            Role.COORDINATOR,
            AuthorizationKind.COORDINATION,
            authority.generation,
            LeaseId(authority.lease_id),
        )
        result = available_actions(snapshot, actor)
        self.assertIsInstance(result, tuple)
        return next(action for action in result if action.kind == kind)

    def test_execute_rediscovers_and_commits_one_transition_from_the_locked_snapshot(self) -> None:
        store = self._store()
        before = store.snapshot()
        action = self._coordinator_action(store, ActionKind.PAUSE)
        result = bind_transition(action, ReasonInput("Pause at a stable checkpoint."))
        self.assertNotIsInstance(result, DecisionFailure)

        outcome = execute(store, result, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(outcome, DecisionFailure)
        after = store.snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.history.receipts) + 1, len(after.history.receipts))

    def test_execute_rejects_a_stale_action_before_decision_or_commit(self) -> None:
        store = self._store()
        action = self._coordinator_action(store, ActionKind.PAUSE)
        result = bind_transition(action, ReasonInput("Pause at a stable checkpoint."))
        self.assertNotIsInstance(result, DecisionFailure)
        command = result

        first = execute(store, command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(first, DecisionFailure)
        committed = store.snapshot()

        rejected = execute(store, command, SQLITE_NOW + timedelta(seconds=2))

        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)
        self.assertEqual(committed, store.snapshot())

    def test_record_planning_impact_revalidates_authority_and_commits_canonical_history(self) -> None:
        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        authority = snapshot.command_attempt_authorities[0]
        impact = PlanningImpact(
            PlanningImpactId("impact-service"),
            ItemId("work-a"),
            AttemptId("work-a-1"),
            1,
            SQLITE_DIGEST,
            "The active work changes the queued item.",
            "The accepted dependency now has a different outcome.",
            (PlanningObligation(ItemId("work-c"), 0, 1, SQLITE_DIGEST),),
        )

        receipt = record_planning_impact(store, authority, impact, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        expected = planning_impact_outcome(impact)
        self.assertNotIsInstance(expected, DecisionFailure)
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.history.receipts) + 1, len(after.history.receipts))
        self.assertEqual("planning-impact/v1", after.history.receipts[-1].outcome_schema)
        self.assertEqual(expected.payload, after.history.receipts[-1].outcome_payload)

        stale = record_planning_impact(
            store,
            replace(authority, lease_id=LeaseId("wrong-lease")),
            replace(impact, impact_id=PlanningImpactId("stale-impact")),
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before.lifecycle.project.revision + 1, store.snapshot().lifecycle.project.revision)

    def test_resolve_planning_obligation_reselects_target_and_commits_canonical_history(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        impact_authority = snapshot.command_attempt_authorities[0]
        impact = PlanningImpact(
            PlanningImpactId("impact-resolution-service"),
            ItemId("work-a"),
            AttemptId("work-a-1"),
            1,
            SQLITE_DIGEST,
            "The active work changes the queued item.",
            "The target must move to its accepted replacement.",
            (PlanningObligation(ItemId("work-c"), 0, 1, SQLITE_DIGEST),),
        )
        recorded = record_planning_impact(store, impact_authority, impact, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(recorded, DecisionFailure)
        before_resolution = store.snapshot()
        coordination = project_decision_snapshot(before_resolution).coordination_authority
        assert coordination is not None

        receipt = resolve_planning_obligation(
            store,
            coordination,
            impact.impact_id,
            ItemId("work-c"),
            PlanningDisposition.SUPERSEDED,
            reason="The replacement now owns the accepted outcome.",
            replacements=(ItemId("legacy-work"),),
            outcome_evidence="The replacement retains the accepted outcome.",
            now=SQLITE_NOW + timedelta(seconds=2),
        )

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        resolved = next(
            value for value in project_decision_snapshot(after).planning_impacts if value.impact_id == impact.impact_id
        )
        expected = planning_resolution_outcome(resolved, ItemId("work-c"))
        self.assertNotIsInstance(expected, DecisionFailure)
        self.assertEqual(before_resolution.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual("planning-impact-resolution/v1", after.history.receipts[-1].outcome_schema)
        self.assertEqual(expected.payload, after.history.receipts[-1].outcome_payload)

        unchanged = after
        rejected = resolve_planning_obligation(
            store,
            replace(coordination, generation=coordination.generation + 1),
            impact.impact_id,
            ItemId("work-c"),
            PlanningDisposition.SUPERSEDED,
            reason="Try the same resolution again.",
            replacements=(ItemId("legacy-work"),),
            outcome_evidence="The replacement retains the accepted outcome.",
            now=SQLITE_NOW + timedelta(seconds=3),
        )
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(unchanged, store.snapshot())

    def test_completion_and_impact_writers_serialize_to_one_domain_outcome(self) -> None:
        completion_store, impact_store = self._store_pair()
        action = self._coordinator_action(completion_store, ActionKind.COMPLETE)
        authority = project_decision_snapshot(completion_store.snapshot()).command_attempt_authorities[0]
        completion = bind_transition(action, EvidenceInput("The accepted outcome is complete."))
        self.assertNotIsInstance(completion, DecisionFailure)
        impact = PlanningImpact(
            PlanningImpactId("impact-race"),
            ItemId("work-a"),
            AttemptId("work-a-1"),
            1,
            SQLITE_DIGEST,
            "The active work changes the queued item.",
            "The target must be reconciled before completion.",
            (PlanningObligation(ItemId("work-c"), 0, 1, SQLITE_DIGEST),),
        )

        completed = execute(completion_store, completion, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(completed, DecisionFailure)
        rejected_impact = record_planning_impact(
            impact_store,
            authority,
            impact,
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertIsInstance(rejected_impact, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PLANNING_IMPACT_INVALID, rejected_impact.code)
        self.assertFalse(any(value.impact_id == impact.impact_id for value in impact_store.snapshot().planning.impacts))

        impact_store, completion_store = self._store_pair()
        action = self._coordinator_action(completion_store, ActionKind.COMPLETE)
        authority = project_decision_snapshot(completion_store.snapshot()).command_attempt_authorities[0]
        completion = bind_transition(action, EvidenceInput("The accepted outcome is complete."))
        self.assertNotIsInstance(completion, DecisionFailure)
        recorded = record_planning_impact(impact_store, authority, impact, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(recorded, DecisionFailure)
        rejected_completion = execute(completion_store, completion, SQLITE_NOW + timedelta(seconds=2))
        self.assertIsInstance(rejected_completion, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PLANNING_IMPACT_UNRESOLVED, rejected_completion.code)
        final = completion_store.snapshot()
        self.assertTrue(any(value.impact_id == impact.impact_id for value in final.planning.impacts))
        self.assertEqual(13, final.lifecycle.project.revision)

    def test_resource_intent_registration_and_observation_advance_share_the_store_transaction(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.command_attempt_authorities[0]
        actor = ActorAuthority(
            Role.WORKER,
            AuthorizationKind.ATTEMPT,
            authority.generation,
            authority.lease_id,
            (authority.attempt,),
            False,
        )
        actions = available_actions(snapshot, actor)
        self.assertIsInstance(actions, tuple)
        capability = actions[0].resource_capabilities[0]
        registration = RegisterMutationIntentInput(
            capability,
            MutationIntentId("intent-service"),
            "mutation-policy/v1",
            CanonicalJson(b'{"paths":["src"]}'),
            "c" * 64,
            SQLITE_NOW + timedelta(seconds=1),
        )

        registered = register_mutation_intent(store, registration)

        self.assertNotIsInstance(registered, DecisionFailure)
        registered_state = store.snapshot()
        self.assertEqual("planned", registered_state.resources.mutation_intents[-1].state.value)
        self.assertEqual(13, registered_state.lifecycle.project.revision)

        snapshot = project_decision_snapshot(registered_state)
        actions = available_actions(snapshot, actor)
        self.assertIsInstance(actions, tuple)
        capability = actions[0].resource_capabilities[0]
        intent = snapshot.mutation_intents[-1]
        instance = snapshot.resource_instances[0]
        definition = snapshot.resource_definitions[0]
        observation = snapshot.resource_observations[0]
        advanced = advance_resource_observation(
            store,
            AdvanceResourceObservationInput(
                ResourceIntentCapability(capability, intent.intent_id, intent.policy_digest, intent.state),
                ObservedResource(
                    instance.instance_id,
                    instance.host_id,
                    definition.kind,
                    instance.discovery_fingerprint,
                    observation.locator_schema,
                    observation.locator,
                    "b" * 64,
                    SQLITE_NOW + timedelta(seconds=2),
                ),
                "change-evidence/v1",
                CanonicalJson(b'{"accepted":true}'),
                "d" * 64,
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )

        self.assertNotIsInstance(advanced, DecisionFailure)
        final = store.snapshot()
        self.assertEqual("accepted", final.resources.mutation_intents[-1].state.value)
        locator = next(value for value in final.resources.locators if value.instance_id == instance.instance_id)
        self.assertEqual("b" * 64, locator.observation_digest)
        self.assertEqual(14, final.lifecycle.project.revision)


if __name__ == "__main__":
    unittest.main()
