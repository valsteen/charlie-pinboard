import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.service import (
    ABANDON_MUTATION_INTENT_INPUT_SCHEMA,
    AbandonMutationIntentHistoryInput,
    abandon_mutation_intent,
    advance_resource_observation,
    execute,
    record_planning_impact,
    register_mutation_intent,
    resolve_planning_obligation,
)
from charlie_pinboard.application.stored_state import (
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    PlanningRecords,
    ResourceMutationIntent,
    StoredAttemptLease,
    StoredResourceUseLease,
    StoredWorkState,
)
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
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
    PlanningImpactId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    CanonicalJson,
    EvidenceInput,
    MutationIntentState,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    ReasonInput,
    ResourceIntentCapability,
    ResourceMutationCapability,
    UseLeaseGenerationKind,
    UseLeaseState,
)
from charlie_pinboard.domain.resource_decisions import (
    AbandonmentForm,
    AbandonMutationIntentInput,
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

    def _store_with_state(self, state: StoredWorkState) -> tuple[SQLiteWorkStore, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        return store, roots.database_path

    def _registered_resource_store(self) -> tuple[SQLiteWorkStore, Path, ResourceMutationCapability]:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        store, database_path = self._store_with_state(state)
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
        capability = next(
            action for action in actions if action.kind == ActionKind.SUBMIT_REVIEW
        ).resource_capabilities[0]
        registered = register_mutation_intent(
            store,
            RegisterMutationIntentInput(
                capability,
                MutationIntentId("intent-abandon-service"),
                "mutation-policy/v1",
                CanonicalJson(b'{"paths":["src"]}'),
                "c" * 64,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertNotIsInstance(registered, DecisionFailure)
        return store, database_path, capability

    def _abandonment_value(
        self,
        store: SQLiteWorkStore,
        capability: ResourceMutationCapability,
        form: AbandonmentForm,
    ) -> AbandonMutationIntentInput:
        snapshot = project_decision_snapshot(store.snapshot())
        intent = snapshot.mutation_intents[-1]
        authority = snapshot.command_attempt_authorities[0]
        instance = snapshot.resource_instance(intent.instance_id)
        observation = snapshot.resource_observation(intent.instance_id)
        definition = snapshot.resource_definition(capability.resource_id)
        assert instance is not None
        assert observation is not None
        assert definition is not None
        return AbandonMutationIntentInput(
            ResourceIntentCapability(capability, intent.intent_id, intent.policy_digest, intent.state),
            authority,
            ObservedResource(
                instance.instance_id,
                instance.host_id,
                definition.kind,
                instance.discovery_fingerprint,
                observation.locator_schema,
                observation.locator,
                observation.digest,
                SQLITE_NOW + timedelta(seconds=2),
            ),
            form,
            "The observed resource remained unchanged.",
            SQLITE_NOW + timedelta(seconds=2),
        )

    def _interrupted_resource_state(
        self,
    ) -> tuple[StoredWorkState, ResourceMutationCapability, ResourceMutationIntent, StoredResourceUseLease]:
        source, _, capability = self._registered_resource_store()
        registered = source.snapshot()
        intent = registered.resources.mutation_intents[-1]
        prior_use = next(
            value for value in registered.resources.use_leases if value.lease_id == intent.resource_use_lease_id
        )
        recovery_generation = intent.attempt_lease_generation + 1
        interrupted = replace(
            registered,
            authority=replace(
                registered.authority,
                attempt_counters=(AttemptLeaseCounter(intent.attempt_id, recovery_generation),),
                attempt_generations=(
                    *registered.authority.attempt_generations,
                    AttemptLeaseGeneration(
                        intent.attempt_id,
                        recovery_generation,
                        LeaseId("attempt-lease-recovery"),
                        TaskId("recovery-worker"),
                        HostId("host-a"),
                    ),
                ),
                attempt_leases=(
                    StoredAttemptLease(
                        intent.attempt_id,
                        recovery_generation,
                        SQLITE_NOW + timedelta(seconds=2),
                        SQLITE_NOW + timedelta(minutes=5),
                        AttemptLeaseState.ACTIVE,
                    ),
                ),
            ),
            resources=replace(
                registered.resources,
                use_leases=tuple(
                    replace(value, state=UseLeaseState.EXPIRED) if value.lease_id == prior_use.lease_id else value
                    for value in registered.resources.use_leases
                ),
            ),
        )
        return interrupted, capability, intent, prior_use

    def _expected_abandonment_history(
        self,
        value: AbandonMutationIntentInput,
        prior_intent: ResourceMutationIntent,
    ) -> AbandonMutationIntentHistoryInput:
        capability = value.intent.resource
        observation = value.observation
        return AbandonMutationIntentHistoryInput(
            capability.locator_observation_digest,
            capability.locator_observation_generation,
            str(value.attempt_authority.task_id),
            observation.discovery_fingerprint,
            value.form,
            str(value.intent.intent_id),
            msgspec.Raw(observation.locator),
            observation.locator_schema,
            observation.digest,
            str(observation.host_id),
            observation.observed_at,
            prior_intent.attempt_lease_generation,
            str(prior_intent.attempt_lease_id),
            str(prior_intent.task_id),
            prior_intent.resource_use_generation,
            str(prior_intent.resource_use_lease_id),
            value.reason,
            str(observation.instance_id),
            observation.resource_kind,
            prior_intent.start_instance_subject_revision,
            prior_intent.start_observation_digest,
            prior_intent.start_observation_generation,
        )

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

    def test_abandonment_commits_both_authority_forms_with_complete_typed_provenance(self) -> None:
        live_store, live_database, live_capability = self._registered_resource_store()
        live_before = live_store.snapshot()
        live_value = self._abandonment_value(live_store, live_capability, AbandonmentForm.LIVE_OWNER)
        live_receipt = abandon_mutation_intent(live_store, live_value)
        self.assertNotIsInstance(live_receipt, DecisionFailure)
        live_reopened = SQLiteWorkStore(live_database).snapshot()
        live_intent = live_reopened.resources.mutation_intents[-1]
        self.assertEqual(live_before.lifecycle.project.revision + 1, live_reopened.lifecycle.project.revision)
        self.assertEqual(len(live_before.history.receipts) + 1, len(live_reopened.history.receipts))
        self.assertEqual(
            (None, None, None),
            (
                live_intent.result_observation_generation,
                live_intent.result_observation_digest,
                live_intent.evidence_digest,
            ),
        )
        live_history = live_reopened.history.receipts[-1]
        self.assertEqual(ABANDON_MUTATION_INTENT_INPUT_SCHEMA, live_history.input_schema)
        self.assertEqual(live_value.attempt_authority.task_id, live_history.actor_task_id)
        decoded_live = msgspec.json.decode(
            live_history.input_payload,
            type=AbandonMutationIntentHistoryInput,
        )
        self.assertEqual(
            self._expected_abandonment_history(live_value, live_before.resources.mutation_intents[-1]),
            decoded_live,
        )
        self.assertEqual(live_history.input_payload, msgspec.json.encode(decoded_live, order="sorted"))

        interrupted_state, clean_capability, prior_intent, _ = self._interrupted_resource_state()
        clean_store, clean_database = self._store_with_state(interrupted_state)
        clean_before = clean_store.snapshot()
        clean_value = self._abandonment_value(clean_store, clean_capability, AbandonmentForm.CLEAN_INTERRUPTION)
        clean_receipt = abandon_mutation_intent(clean_store, clean_value)
        self.assertNotIsInstance(clean_receipt, DecisionFailure)
        clean_reopened = SQLiteWorkStore(clean_database).snapshot()
        clean_intent = clean_reopened.resources.mutation_intents[-1]
        clean_history = clean_reopened.history.receipts[-1]
        decoded_clean = msgspec.json.decode(
            clean_history.input_payload,
            type=AbandonMutationIntentHistoryInput,
        )
        self.assertEqual(clean_before.lifecycle.project.revision + 1, clean_reopened.lifecycle.project.revision)
        self.assertEqual(len(clean_before.history.receipts) + 1, len(clean_reopened.history.receipts))
        self.assertEqual(
            (None, None, None),
            (
                clean_intent.result_observation_generation,
                clean_intent.result_observation_digest,
                clean_intent.evidence_digest,
            ),
        )
        self.assertEqual(TaskId("recovery-worker"), clean_history.actor_task_id)
        self.assertEqual(self._expected_abandonment_history(clean_value, prior_intent), decoded_clean)
        self.assertEqual(clean_history.input_payload, msgspec.json.encode(decoded_clean, order="sorted"))

    def test_abandonment_history_codec_rejects_malformed_constrained_or_unknown_input(self) -> None:
        interrupted, capability, _, _ = self._interrupted_resource_state()
        store, _ = self._store_with_state(interrupted)
        value = self._abandonment_value(store, capability, AbandonmentForm.CLEAN_INTERRUPTION)
        receipt = abandon_mutation_intent(store, value)
        self.assertNotIsInstance(receipt, DecisionFailure)
        payload = store.snapshot().history.receipts[-1].input_payload
        decoded = msgspec.json.decode(payload, type=AbandonMutationIntentHistoryInput)
        revision_zero = msgspec.structs.replace(decoded, start_instance_subject_revision=0)
        revision_zero_payload = msgspec.json.encode(revision_zero, order="sorted")
        decoded_revision_zero = msgspec.json.decode(
            revision_zero_payload,
            type=AbandonMutationIntentHistoryInput,
        )
        self.assertEqual(revision_zero, decoded_revision_zero)
        self.assertEqual(revision_zero_payload, msgspec.json.encode(decoded_revision_zero, order="sorted"))

        with self.assertRaises(msgspec.DecodeError):
            msgspec.json.decode(b"{", type=AbandonMutationIntentHistoryInput)
        invalid_payloads = (
            msgspec.json.encode(msgspec.structs.replace(decoded, reason=""), order="sorted"),
            payload[:-1] + b',"unknown":true}',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(msgspec.ValidationError):
                msgspec.json.decode(payload, type=AbandonMutationIntentHistoryInput)

    def test_abandonment_rejects_reuse_stale_authority_later_use_or_later_intent_without_commit(self) -> None:
        interrupted_state, capability, prior_intent, prior_use = self._interrupted_resource_state()
        clean_store, _ = self._store_with_state(interrupted_state)
        clean_value = self._abandonment_value(clean_store, capability, AbandonmentForm.CLEAN_INTERRUPTION)
        accepted = abandon_mutation_intent(clean_store, clean_value)
        self.assertNotIsInstance(accepted, DecisionFailure)
        committed = clean_store.snapshot()
        replay = abandon_mutation_intent(clean_store, clean_value)
        self.assertIsInstance(replay, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_USE_LEASE_STALE, replay.code)
        self.assertEqual(committed, clean_store.snapshot())

        later_use = replace(
            prior_use,
            task_id=TaskId("recovery-worker"),
            attempt_lease_id=LeaseId("attempt-lease-recovery"),
            attempt_lease_generation=prior_intent.attempt_lease_generation + 1,
            lease_id=LeaseId("use-later"),
            generation=prior_use.generation + 1,
            generation_kind=UseLeaseGenerationKind.GRANT,
            acquired_at=SQLITE_NOW + timedelta(seconds=2),
            state=UseLeaseState.ACTIVE,
        )
        later_intent = replace(
            prior_intent,
            intent_id=MutationIntentId("intent-later"),
            state=MutationIntentState.ABANDONED,
            recorded_at=SQLITE_NOW + timedelta(seconds=3),
            resolved_at=SQLITE_NOW + timedelta(seconds=3),
            disposition_task_id=TaskId("recovery-worker"),
            disposition_reason="A later operation already resolved.",
        )
        rejection_states = (
            (
                "stale-attempt",
                interrupted_state,
                replace(clean_value.attempt_authority, lease_id=LeaseId("stale")),
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            ),
            (
                "later-use",
                replace(
                    interrupted_state,
                    resources=replace(
                        interrupted_state.resources,
                        use_leases=(*interrupted_state.resources.use_leases, later_use),
                    ),
                ),
                clean_value.attempt_authority,
                DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            ),
            (
                "later-intent",
                replace(
                    interrupted_state,
                    resources=replace(
                        interrupted_state.resources,
                        mutation_intents=(*interrupted_state.resources.mutation_intents, later_intent),
                    ),
                ),
                clean_value.attempt_authority,
                DecisionFailureCode.RESOURCE_USE_LEASE_STALE,
            ),
        )
        for name, state, authority, expected_code in rejection_states:
            with self.subTest(name=name):
                store, _ = self._store_with_state(state)
                before = store.snapshot()
                candidate = replace(
                    self._abandonment_value(store, capability, AbandonmentForm.CLEAN_INTERRUPTION),
                    attempt_authority=authority,
                )
                rejected = abandon_mutation_intent(store, candidate)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(expected_code, rejected.code)
                self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
