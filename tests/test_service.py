import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.service import (
    ABANDON_MUTATION_INTENT_INPUT_SCHEMA,
    AbandonMutationIntentHistoryInput,
    abandon_mutation_intent,
    advance_resource_observation,
    change_attempt_authority,
    change_coordination_authority,
    change_reservation,
    change_task_use_authority,
    claim_resource,
    create_proposal,
    edit_item_scope,
    edit_resource_definition,
    execute,
    preserve_resource_state,
    reconcile_interrupted_observation,
    record_planning_impact,
    register_mutation_intent,
    resolve_fenced_resource_intent,
    resolve_planning_obligation,
)
from charlie_pinboard.application.stored_state import (
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    PlanningRecords,
    ResourceInstanceState,
    ResourceMutationIntent,
    StoredAttemptLease,
    StoredResourceUseLease,
    StoredWorkState,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AcquireTaskUseAuthority,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    ReleaseTaskUseAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RenewTaskUseAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    RevokeTaskUseAuthority,
    TransferAttemptAuthority,
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
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AcceptProposalInput,
    CanonicalJson,
    EvidenceInput,
    MergeProposalInput,
    MutationIntentState,
    PlanningImpact,
    PlanningObligation,
    ProposalRelationKind,
    ReasonInput,
    ResourceIntentCapability,
    ResourceMutationCapability,
    ResourceRequirement,
    ScopeDependency,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
    UseLeaseGenerationKind,
    UseLeaseState,
)
from charlie_pinboard.domain.planning_decisions import (
    BlockedPlanningDisposition,
    InterruptedPlanningAttemptAuthority,
    LivePlanningAttemptAuthority,
    NoAttemptPlanningAuthority,
    RecordPlanningImpactOperation,
    ResolvePlanningObligationOperation,
    RevisedPlanningDisposition,
    SupersededPlanningDisposition,
)
from charlie_pinboard.domain.proposal_decisions import CreateProposalOperation, ProposalIntake
from charlie_pinboard.domain.resource_decisions import (
    AbandonmentForm,
    AbandonMutationIntentInput,
    AdvanceResourceObservationInput,
    AssignReservationOperation,
    ClaimResourceOperation,
    FencedIntentDisposition,
    ObservedResource,
    PreserveResourceStateInput,
    ReallocateReservationOperation,
    ReconcileInterruptedObservationInput,
    RegisterMutationIntentInput,
    ReleaseReservationOperation,
    ResolveFencedIntentInput,
    ResolverEvidenceDecision,
    RevokeReservationOperation,
)
from charlie_pinboard.domain.resource_definition_decisions import (
    PortableResourceDefinition,
    ResourceDefinitionEditOperation,
    ResourceDefinitionUnchanged,
)
from charlie_pinboard.domain.scope_decisions import ReplaceDependenciesOperation, ReplaceResourceRequirementsOperation
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


class ServiceTest(unittest.TestCase):
    def _store(self) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        store.initialize_state(replace(state, resources=replace(state.resources, mutation_intents=())))
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

    def test_execute_accepts_exact_live_worker_authority_for_review_submission(self) -> None:
        store = self._store()
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
        action = next(value for value in actions if value.kind == ActionKind.SUBMIT_REVIEW)
        command = bind_transition(action, SubmitReviewInput(CandidateId("candidate-review")))
        self.assertNotIsInstance(command, DecisionFailure)
        receipt = execute(store, command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(receipt, DecisionFailure)
        self.assertEqual("review", store.snapshot().lifecycle.attempts[0].state.value)

    def test_coordination_authority_operations_are_pure_fenced_and_transactional(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            authority=replace(state.authority, coordination=None),
            resources=replace(state.resources, mutation_intents=()),
        )
        store, _database_path = self._store_with_state(state)
        acquired_at = SQLITE_NOW + timedelta(seconds=1)
        acquired = change_coordination_authority(
            store,
            AcquireCoordinationAuthority(
                state.lifecycle.project.host_epoch,
                TaskId("coordinator-a"),
                HostId("host-a"),
                LeaseId("coordination-a"),
                acquired_at,
                acquired_at + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        current = project_decision_snapshot(store.snapshot()).coordination_authority
        assert current is not None

        renewed = change_coordination_authority(
            store,
            RenewCoordinationAuthority(
                current,
                acquired_at + timedelta(seconds=1),
                acquired_at + timedelta(minutes=3),
            ),
        )

        self.assertNotIsInstance(renewed, DecisionFailure)
        after = store.snapshot()
        assert after.authority.coordination is not None
        self.assertEqual(acquired_at + timedelta(minutes=3), after.authority.coordination.expires_at)
        self.assertEqual(state.lifecycle.project.revision + 2, after.lifecycle.project.revision)
        self.assertEqual(len(state.history.receipts) + 2, len(after.history.receipts))
        renewed_authority = project_decision_snapshot(after).coordination_authority
        assert renewed_authority is not None
        released = change_coordination_authority(
            store,
            ReleaseCoordinationAuthority(renewed_authority, acquired_at + timedelta(seconds=2)),
        )
        self.assertNotIsInstance(released, DecisionFailure)
        released_authority = store.snapshot().authority.coordination
        assert released_authority is not None
        self.assertEqual("released", released_authority.state.value)

        revoke_store, _database_path = self._store_with_state(state)
        acquired = change_coordination_authority(
            revoke_store,
            AcquireCoordinationAuthority(
                state.lifecycle.project.host_epoch,
                TaskId("coordinator-b"),
                HostId("host-a"),
                LeaseId("coordination-b"),
                acquired_at,
                acquired_at + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        revoke_authority = project_decision_snapshot(revoke_store.snapshot()).coordination_authority
        assert revoke_authority is not None
        revoked = change_coordination_authority(
            revoke_store,
            RevokeCoordinationAuthority(
                revoke_authority.lease_id,
                revoke_authority.generation,
                acquired_at + timedelta(seconds=2),
            ),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        revoked_authority = revoke_store.snapshot().authority.coordination
        assert revoked_authority is not None
        self.assertEqual("revoked", revoked_authority.state.value)

    def test_attempt_authority_renewal_and_release_fence_every_current_task_use(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        store, _database_path = self._store_with_state(state)
        current = project_decision_snapshot(store.snapshot()).command_attempt_authorities[0]
        renewed = change_attempt_authority(
            store,
            RenewAttemptAuthority(
                current,
                SQLITE_NOW + timedelta(seconds=1),
                current.expires_at + timedelta(minutes=1),
            ),
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        current = project_decision_snapshot(store.snapshot()).command_attempt_authorities[0]

        released = change_attempt_authority(
            store,
            ReleaseAttemptAuthority(current, SQLITE_NOW + timedelta(seconds=2)),
        )

        self.assertNotIsInstance(released, DecisionFailure)
        after = store.snapshot()
        self.assertEqual(4, after.authority.attempt_counters[0].generation_high_water)
        self.assertEqual(AttemptLeaseState.RELEASED, after.authority.attempt_leases[0].state)
        self.assertEqual(
            ((3, "grant", "revoked"), (4, "fence", "revoked")),
            tuple(
                (value.generation, value.generation_kind.value, value.state.value)
                for value in after.resources.use_leases[-2:]
            ),
        )
        self.assertEqual("active", after.resources.reservations[0].state.value)

    def test_attempt_authority_initial_acquire_transfer_and_revoke_persist_exact_generations(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(replace(state.authority.attempt_counters[0], generation_high_water=0),),
                attempt_generations=(),
                attempt_leases=(),
            ),
            resources=replace(state.resources, use_leases=(), mutation_intents=()),
        )
        store, _database_path = self._store_with_state(state)
        attempt = state.lifecycle.attempts[0]
        acquired = change_attempt_authority(
            store,
            AcquireInitialAttemptAuthority(
                state.lifecycle.project.host_epoch,
                attempt.attempt_id,
                attempt.item_id,
                TaskId("worker-initial"),
                HostId("host-a"),
                LeaseId("attempt-initial"),
                SQLITE_NOW + timedelta(seconds=1),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)

        normal_state = complete_sqlite_state()
        normal_state = replace(normal_state, resources=replace(normal_state.resources, mutation_intents=()))
        normal_store, _normal_database_path = self._store_with_state(normal_state)
        snapshot = project_decision_snapshot(normal_store.snapshot())
        current = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        assert coordination is not None
        released = change_attempt_authority(
            normal_store,
            ReleaseAttemptAuthority(current, SQLITE_NOW + timedelta(seconds=1)),
        )
        self.assertNotIsInstance(released, DecisionFailure)
        proof = project_inactive_attempt_authority(
            normal_store.snapshot(),
            current.attempt,
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertNotIsInstance(proof, DecisionFailure)
        assert not isinstance(proof, DecisionFailure)
        transferred = change_attempt_authority(
            normal_store,
            TransferAttemptAuthority(
                proof,
                coordination,
                TaskId("worker-next"),
                HostId("host-a"),
                LeaseId("attempt-next"),
                SQLITE_NOW + timedelta(seconds=2),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(transferred, DecisionFailure)
        next_authority = project_decision_snapshot(normal_store.snapshot()).command_attempt_authorities[0]
        revoked = change_attempt_authority(
            normal_store,
            RevokeAttemptAuthority(
                next_authority.attempt,
                next_authority.lease_id,
                next_authority.generation,
                coordination,
                SQLITE_NOW + timedelta(seconds=3),
            ),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        self.assertEqual(AttemptLeaseState.REVOKED, normal_store.snapshot().authority.attempt_leases[0].state)

    def test_task_use_renewal_and_release_change_only_the_exact_grant(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        store, _database_path = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        current = snapshot.mutation_use_leases[-1]
        attempt = snapshot.command_attempt_authorities[0]
        renewed = change_task_use_authority(
            store,
            RenewTaskUseAuthority(
                current,
                attempt,
                SQLITE_NOW + timedelta(seconds=1),
                current.expires_at + timedelta(minutes=1),
            ),
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        snapshot = project_decision_snapshot(store.snapshot())
        current = snapshot.mutation_use_leases[-1]

        released = change_task_use_authority(
            store,
            ReleaseTaskUseAuthority(
                current,
                snapshot.command_attempt_authorities[0],
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )

        self.assertNotIsInstance(released, DecisionFailure)
        after = store.snapshot()
        self.assertEqual("released", after.resources.use_leases[-1].state.value)
        self.assertEqual("active", after.resources.reservations[0].state.value)
        self.assertEqual(state.authority, after.authority)

    def test_task_use_initial_acquire_and_coordination_revocation_persist_the_fence(self) -> None:
        state = complete_sqlite_state()
        template = project_decision_snapshot(state).mutation_use_leases[-1]
        state = replace(state, resources=replace(state.resources, use_leases=(), mutation_intents=()))
        store, _database_path = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        attempt = snapshot.command_attempt_authorities[0]
        requested = replace(
            template,
            lease_id=LeaseId("use-initial"),
            generation=1,
            expires_at=SQLITE_NOW + timedelta(minutes=2),
            state=UseLeaseState.ACTIVE,
        )
        acquired = change_task_use_authority(
            store,
            AcquireTaskUseAuthority(requested, attempt, SQLITE_NOW + timedelta(seconds=1)),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        current = project_decision_snapshot(store.snapshot()).mutation_use_leases[-1]
        coordination = project_decision_snapshot(store.snapshot()).coordination_authority
        assert coordination is not None
        fence = replace(
            current,
            lease_id=LeaseId("use-revoked-fence"),
            generation=current.generation + 1,
            generation_kind=UseLeaseGenerationKind.FENCE,
            expires_at=SQLITE_NOW + timedelta(seconds=2),
            state=UseLeaseState.REVOKED,
        )
        revoked = change_task_use_authority(
            store,
            RevokeTaskUseAuthority(current, fence, coordination, SQLITE_NOW + timedelta(seconds=2)),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        self.assertEqual(
            ((1, "grant", "revoked"), (2, "fence", "revoked")),
            tuple(
                (value.generation, value.generation_kind.value, value.state.value)
                for value in store.snapshot().resources.use_leases
            ),
        )

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

    def test_execute_transfers_the_active_sqlite_coordination_lease_in_place(self) -> None:
        store = self._store()
        before = store.snapshot()
        prior = before.authority.coordination
        assert prior is not None
        action = self._coordinator_action(store, ActionKind.TRANSFER_COORDINATOR)
        command = bind_transition(action, TransferCoordinatorInput(TaskId("next-task"), HostId("next-host")))
        assert not isinstance(command, DecisionFailure)

        receipt = execute(store, command, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        current = store.snapshot().authority.coordination
        assert current is not None
        self.assertEqual(prior.lease_id, current.lease_id)
        self.assertEqual(prior.acquired_at, current.acquired_at)
        self.assertEqual(prior.expires_at, current.expires_at)
        self.assertEqual(prior.generation + 1, current.generation)
        self.assertEqual((TaskId("next-task"), HostId("next-host")), (current.task_id, current.host_id))

    def test_completion_fences_attempt_authority_and_releases_all_resource_authority_atomically(self) -> None:
        store = self._store()
        before = store.snapshot()
        prior_attempt = before.authority.attempt_leases[0]
        action = self._coordinator_action(store, ActionKind.COMPLETE)
        command = bind_transition(action, EvidenceInput("The accepted outcome is complete."))
        assert not isinstance(command, DecisionFailure)

        receipt = execute(store, command, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        current_attempt = after.authority.attempt_leases[0]
        self.assertEqual(prior_attempt.generation + 1, current_attempt.generation)
        self.assertEqual(AttemptLeaseState.REVOKED, current_attempt.state)
        self.assertTrue(all(value.state.value == "released" for value in after.resources.reservations))
        self.assertTrue(
            all(
                value.state.value != "active"
                for value in after.resources.use_leases
                if value.generation_kind == UseLeaseGenerationKind.GRANT
            )
        )

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

        receipt = record_planning_impact(
            store, RecordPlanningImpactOperation(impact, authority, SQLITE_NOW + timedelta(seconds=1))
        )

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
            RecordPlanningImpactOperation(
                replace(impact, impact_id=PlanningImpactId("stale-impact")),
                replace(authority, lease_id=LeaseId("wrong-lease")),
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before.lifecycle.project.revision + 1, store.snapshot().lifecycle.project.revision)

    def test_item_only_planning_impact_requires_exact_coordination_authority(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        coordination = snapshot.coordination_authority
        assert coordination is not None
        impact = PlanningImpact(
            PlanningImpactId("impact-item-only"),
            ItemId("work-c"),
            None,
            1,
            SQLITE_DIGEST,
            "The queued work changes the active item.",
            "The accepted scope identifies the exact target.",
            (PlanningObligation(ItemId("work-a"), 0, 1, SQLITE_DIGEST),),
        )

        receipt = record_planning_impact(
            store, RecordPlanningImpactOperation(impact, coordination, SQLITE_NOW + timedelta(seconds=1))
        )

        self.assertNotIsInstance(receipt, DecisionFailure)
        self.assertEqual(
            TransitionHistoryAuthorizationKind.COORDINATION,
            store.snapshot().history.receipts[-1].authorization,
        )

    def test_sqlite_proposal_intake_is_immutable_and_does_not_require_a_coordination_lease(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()),
            authority=replace(state.authority, coordination=None),
        )
        store, _database_path = self._store_with_state(state)
        created_at = SQLITE_NOW - timedelta(days=1)
        recorded_at = SQLITE_NOW + timedelta(seconds=1)
        intake = ProposalIntake(
            ProposalId("sqlite-proposal"),
            created_at,
            TaskId("discovering-task"),
            "SQLite proposal",
            "A local observation needs coordination.",
            "The finding should be preserved immutably.",
            "A proposal row becomes available for disposition.",
            "The coordinator can inspect it later.",
            ProposalRelationKind.INDEPENDENT,
            None,
            "The evidence is current.",
            ("source:local",),
            ("The current schema remains accepted.",),
        )

        receipt = create_proposal(store, CreateProposalOperation(intake), recorded_at)
        duplicate = create_proposal(store, CreateProposalOperation(intake), recorded_at + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        self.assertIsInstance(duplicate, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, duplicate.code)
        after = store.snapshot()
        self.assertEqual(
            (ProposalId("sqlite-proposal"),), tuple(value.proposal_id for value in after.proposals.proposals)
        )
        self.assertEqual(("source:local",), tuple(value.selector for value in after.proposals.evidence))
        proposal = after.proposals.proposals[0]
        self.assertEqual(created_at, proposal.created_at)
        self.assertEqual(recorded_at, proposal.recorded_at)
        self.assertEqual(recorded_at, after.lifecycle.project.updated_at)
        self.assertEqual(recorded_at, after.history.receipts[-1].committed_at)

    def test_sqlite_proposal_intake_rejects_a_missing_related_item_before_persistence(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()))
        store, _database_path = self._store_with_state(state)
        intake = ProposalIntake(
            ProposalId("missing-related-item"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Missing relation",
            "A proposal names an absent item.",
            "Storage must not be the first validator.",
            "The proposal is rejected without mutation.",
            "Return a typed item rejection.",
            ProposalRelationKind.FOLLOW_UP,
            ItemId("does-not-exist"),
            "The related item is absent.",
            (),
            (),
        )
        before = store.snapshot()

        result = create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertIsInstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, result.code)
        self.assertEqual(before, store.snapshot())

    def test_dependency_edit_advances_only_the_affected_item_scope_and_subject(self) -> None:
        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        authority = snapshot.coordination_authority
        current = next(value for value in snapshot.scopes if value.item == ItemId("work-c"))
        assert authority is not None
        dependencies = (ScopeDependency(0, ItemId("work-a")),)
        operation = ReplaceDependenciesOperation(
            authority,
            ItemId("work-c"),
            current,
            dependencies,
            replace(current.scope, dependencies=dependencies),
            SQLITE_NOW + timedelta(seconds=1),
        )

        receipt = edit_item_scope(store, operation)

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        before_items = {value.item_id: value for value in before.lifecycle.work_items}
        after_items = {value.item_id: value for value in after.lifecycle.work_items}
        self.assertEqual(
            before_items[ItemId("work-a")].subject_revision, after_items[ItemId("work-a")].subject_revision
        )
        self.assertEqual(2, after_items[ItemId("work-c")].scope_revision)
        self.assertEqual(before.lifecycle.project.revision + 1, after_items[ItemId("work-c")].subject_revision)
        self.assertEqual(
            (ItemId("work-a"),),
            tuple(value.dependency_id for value in after.lifecycle.dependencies if value.item_id == ItemId("work-c")),
        )

    def test_resource_requirement_edit_replaces_only_the_target_item_set(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.coordination_authority
        assert authority is not None
        current = next(value for value in snapshot.scopes if value.item == ItemId("work-c"))
        requirements = (ResourceRequirement(0, ResourceId("workspace")),)
        receipt = edit_item_scope(
            store,
            ReplaceResourceRequirementsOperation(
                authority,
                current.item,
                current,
                requirements,
                replace(current.scope, resource_requirements=requirements),
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertNotIsInstance(receipt, DecisionFailure)
        after = project_decision_snapshot(store.snapshot())
        changed = next(value for value in after.scopes if value.item == current.item)
        self.assertEqual(requirements, changed.scope.resource_requirements)

    def test_resource_definition_edit_stales_only_requiring_items_and_identical_repeat_is_noop(self) -> None:
        store = self._store()
        before = store.snapshot()
        definition = before.resources.definitions[0]
        requiring = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-a"))
        unrelated = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-b"))
        scopes = before.lifecycle.scope_revisions
        authority = project_decision_snapshot(before).coordination_authority
        assert authority is not None

        changed = edit_resource_definition(
            store,
            ResourceDefinitionEditOperation(
                authority,
                definition.subject_revision,
                PortableResourceDefinition(definition.resource_id, definition.kind, "Renamed exclusive workspace"),
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertNotIsInstance(changed, DecisionFailure)
        after = store.snapshot()
        updated = after.resources.definitions[0]
        self.assertEqual(definition.subject_revision + 1, updated.subject_revision)
        self.assertEqual("Renamed exclusive workspace", updated.description)
        self.assertEqual(
            requiring.subject_revision + 1,
            next(value for value in after.lifecycle.work_items if value.item_id == requiring.item_id).subject_revision,
        )
        self.assertEqual(
            unrelated.subject_revision,
            next(value for value in after.lifecycle.work_items if value.item_id == unrelated.item_id).subject_revision,
        )
        self.assertEqual(scopes, after.lifecycle.scope_revisions)
        revision = after.lifecycle.project.revision
        history = after.history.receipts
        current_authority = project_decision_snapshot(after).coordination_authority
        assert current_authority is not None

        repeated = edit_resource_definition(
            store,
            ResourceDefinitionEditOperation(
                current_authority,
                updated.subject_revision,
                PortableResourceDefinition(updated.resource_id, updated.kind, updated.description),
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )

        self.assertIsInstance(repeated, ResourceDefinitionUnchanged)
        final = store.snapshot()
        self.assertEqual(revision, final.lifecycle.project.revision)
        self.assertEqual(history, final.history.receipts)

    def test_resource_definition_creation_persists_once_and_rejects_stale_absence(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.coordination_authority
        assert authority is not None
        definition = PortableResourceDefinition(ResourceId("capture-rig"), "capture-rig", "One local capture rig")
        created = edit_resource_definition(
            store,
            ResourceDefinitionEditOperation(authority, None, definition, SQLITE_NOW + timedelta(seconds=1)),
        )
        self.assertNotIsInstance(created, DecisionFailure)
        self.assertEqual(
            definition.description,
            next(
                value.description
                for value in store.snapshot().resources.definitions
                if value.resource_id == definition.resource_id
            ),
        )
        stale = edit_resource_definition(
            store,
            ResourceDefinitionEditOperation(
                authority, None, replace(definition, description="Different"), SQLITE_NOW + timedelta(seconds=2)
            ),
        )
        self.assertIsInstance(stale, DecisionFailure)

    def test_resource_definition_rejects_nonportable_kind_before_sqlite(self) -> None:
        store = self._store()
        before = store.snapshot()
        authority = project_decision_snapshot(before).coordination_authority
        assert authority is not None

        rejected = edit_resource_definition(
            store,
            ResourceDefinitionEditOperation(
                authority,
                None,
                PortableResourceDefinition(ResourceId("bad-kind"), "Bad--Kind", "Invalid portable kind"),
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_DECLARATION_INVALID, rejected.code)
        self.assertEqual(before, store.snapshot())

    def test_direct_reservation_release_atomically_releases_current_task_use(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        store, _database_path = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.coordination_authority
        assert authority is not None
        reservation = next(value for value in snapshot.resource_reservations if value.state.value == "active")
        observation = snapshot.resource_observation(reservation.instance_id)
        assert observation is not None

        receipt = change_reservation(
            store,
            ReleaseReservationOperation(
                authority,
                reservation.reservation_id,
                reservation.generation,
                observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = project_decision_snapshot(store.snapshot())
        released = after.resource_reservation(reservation.reservation_id)
        assert released is not None
        self.assertEqual("released", released.state.value)
        self.assertFalse(
            any(
                value.reservation_id == reservation.reservation_id and value.state.value == "active"
                for value in after.resource_use_leases
            )
        )

    def test_direct_reservation_release_rejects_a_planned_intent_without_commit(self) -> None:
        store, _database_path = self._store_with_state(complete_sqlite_state())
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        authority = snapshot.coordination_authority
        assert authority is not None
        reservation = next(value for value in snapshot.resource_reservations if value.state.value == "active")
        observation = snapshot.resource_observation(reservation.instance_id)
        assert observation is not None

        rejected = change_reservation(
            store,
            ReleaseReservationOperation(
                authority,
                reservation.reservation_id,
                reservation.generation,
                observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED, rejected.code)
        self.assertEqual(before, store.snapshot())

    def test_reservation_release_rejects_cross_wired_locator_evidence(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            resources=replace(
                state.resources,
                instances=tuple(
                    replace(value, state=ResourceInstanceState.ACTIVE) for value in state.resources.instances
                ),
                mutation_intents=(),
            ),
        )
        store, _database_path = self._store_with_state(state)
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        authority = snapshot.coordination_authority
        assert authority is not None
        reservation = snapshot.resource_reservations[0]
        wrong_observation = next(
            value for value in snapshot.resource_observations if value.instance_id != reservation.instance_id
        )

        rejected = change_reservation(
            store,
            ReleaseReservationOperation(
                authority,
                reservation.reservation_id,
                reservation.generation,
                wrong_observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_RESERVATION_STALE, rejected.code)
        self.assertEqual(before, store.snapshot())

    def test_proposal_relationships_reject_missing_identities_before_sqlite(self) -> None:
        dependency_store = self._store()
        accept = self._coordinator_action(dependency_store, ActionKind.ACCEPT_PROPOSAL)
        dependency_command = bind_transition(
            accept,
            AcceptProposalInput(
                ItemId("accepted-proposal"),
                AcceptedProposalState.INTAKE,
                "review-intake",
                Timing.SAFE_TO_DEFER,
                (ItemId("missing-dependency"),),
            ),
        )
        assert not isinstance(dependency_command, DecisionFailure)
        before = dependency_store.snapshot()
        dependency_rejected = execute(
            dependency_store,
            dependency_command,
            SQLITE_NOW + timedelta(seconds=1),
        )
        self.assertIsInstance(dependency_rejected, DecisionFailure)
        assert isinstance(dependency_rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.DEPENDENCY_NOT_SATISFIED, dependency_rejected.code)
        self.assertEqual(before, dependency_store.snapshot())

        merge_store = self._store()
        merge = self._coordinator_action(merge_store, ActionKind.MERGE_PROPOSAL)
        merge_command = bind_transition(merge, MergeProposalInput(ItemId("missing-target")))
        assert not isinstance(merge_command, DecisionFailure)
        before = merge_store.snapshot()
        merge_rejected = execute(merge_store, merge_command, SQLITE_NOW + timedelta(seconds=1))
        self.assertIsInstance(merge_rejected, DecisionFailure)
        assert isinstance(merge_rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, merge_rejected.code)
        self.assertEqual(before, merge_store.snapshot())

    def test_resource_claim_atomically_assigns_reservation_and_first_task_use(self) -> None:
        state = complete_sqlite_state()
        initial = project_decision_snapshot(state)
        template = initial.mutation_use_leases[-1]
        state = replace(
            state,
            resources=replace(
                state.resources,
                reservation_counters=tuple(
                    replace(value, generation_high_water=0) for value in state.resources.reservation_counters
                ),
                reservations=(),
                use_leases=(),
                mutation_intents=(),
            ),
        )
        store, _database_path = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.command_attempt_authorities[0]
        definition = snapshot.resource_definitions[0]
        instance = snapshot.resource_instances[0]
        observation = snapshot.resource_observation(instance.instance_id)
        assert observation is not None
        requested = replace(
            template,
            reservation_id=ReservationId("reservation-claim"),
            instance_id=instance.instance_id,
            reservation_generation=1,
            instance_subject_revision=instance.subject_revision,
            observation_generation=observation.generation,
            observation_digest=observation.digest,
            lease_id=LeaseId("use-claim"),
            generation=1,
            expires_at=SQLITE_NOW + timedelta(minutes=4),
            state=UseLeaseState.ACTIVE,
        )

        receipt = claim_resource(
            store,
            ClaimResourceOperation(
                definition,
                instance,
                observation,
                authority,
                requested,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = project_decision_snapshot(store.snapshot())
        reservation = after.resource_reservation(ReservationId("reservation-claim"))
        self.assertIsNotNone(reservation)
        self.assertTrue(
            any(
                value.reservation_id == ReservationId("reservation-claim")
                and value.generation == 1
                and value.state == UseLeaseState.ACTIVE
                for value in after.resource_use_leases
            )
        )

    def test_reservation_service_assigns_reallocates_and_revokes_with_exact_observations(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            resources=replace(
                state.resources,
                instances=tuple(
                    replace(value, state=ResourceInstanceState.ACTIVE) for value in state.resources.instances
                ),
                reservation_counters=tuple(
                    replace(value, generation_high_water=0) for value in state.resources.reservation_counters
                ),
                reservations=(),
                use_leases=(),
                mutation_intents=(),
            ),
        )
        store, _database_path = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.command_attempt_authorities[0]
        coordination = snapshot.coordination_authority
        assert coordination is not None
        first, second = snapshot.resource_instances
        first_observation = snapshot.resource_observation(first.instance_id)
        second_observation = snapshot.resource_observation(second.instance_id)
        assert first_observation is not None and second_observation is not None
        unchanged = store.snapshot()
        cross_wired_assignment = change_reservation(
            store,
            AssignReservationOperation(
                authority,
                ReservationId("cross-wired-assignment"),
                first.resource_id,
                first.instance_id,
                authority.attempt,
                1,
                second_observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertIsInstance(cross_wired_assignment, DecisionFailure)
        self.assertEqual(unchanged, store.snapshot())
        assigned = change_reservation(
            store,
            AssignReservationOperation(
                authority,
                ReservationId("reservation-first"),
                first.resource_id,
                first.instance_id,
                snapshot.command_attempt_authorities[0].attempt,
                1,
                first_observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertNotIsInstance(assigned, DecisionFailure)
        after_assignment = store.snapshot()
        cross_wired_reallocation = change_reservation(
            store,
            ReallocateReservationOperation(
                authority,
                ReservationId("reservation-first"),
                1,
                ReservationId("cross-wired-reallocation"),
                second.instance_id,
                1,
                first_observation,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertIsInstance(cross_wired_reallocation, DecisionFailure)
        self.assertEqual(after_assignment, store.snapshot())
        reallocated = change_reservation(
            store,
            ReallocateReservationOperation(
                authority,
                ReservationId("reservation-first"),
                1,
                ReservationId("reservation-second"),
                second.instance_id,
                1,
                second_observation,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertNotIsInstance(reallocated, DecisionFailure)
        after_reallocation = store.snapshot()
        cross_wired_revocation = change_reservation(
            store,
            RevokeReservationOperation(
                coordination,
                ReservationId("reservation-second"),
                1,
                1,
                first_observation,
                SQLITE_NOW + timedelta(seconds=3),
            ),
        )
        self.assertIsInstance(cross_wired_revocation, DecisionFailure)
        self.assertEqual(after_reallocation, store.snapshot())
        revoked = change_reservation(
            store,
            RevokeReservationOperation(
                coordination,
                ReservationId("reservation-second"),
                1,
                1,
                second_observation,
                SQLITE_NOW + timedelta(seconds=3),
            ),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        final = project_decision_snapshot(store.snapshot())
        self.assertEqual(
            ("released", "revoked"),
            tuple(value.state.value for value in final.resource_reservations),
        )

    def test_assignment_and_atomic_claim_require_the_attempt_item_resource_requirement(self) -> None:
        state = complete_sqlite_state()
        template = project_decision_snapshot(state).mutation_use_leases[-1]
        state = replace(
            state,
            resources=replace(
                state.resources,
                requirements=(),
                instances=tuple(
                    replace(value, state=ResourceInstanceState.ACTIVE) for value in state.resources.instances
                ),
                reservation_counters=tuple(
                    replace(value, generation_high_water=0) for value in state.resources.reservation_counters
                ),
                reservations=(),
                use_leases=(),
                mutation_intents=(),
            ),
        )
        store, _database_path = self._store_with_state(state)
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        authority = snapshot.command_attempt_authorities[0]
        definition = snapshot.resource_definitions[0]
        instance = next(value for value in snapshot.resource_instances if value.resource_id == definition.resource_id)
        observation = snapshot.resource_observation(instance.instance_id)
        assert observation is not None

        assignment = change_reservation(
            store,
            AssignReservationOperation(
                authority,
                ReservationId("unrequired-assignment"),
                definition.resource_id,
                instance.instance_id,
                authority.attempt,
                1,
                observation,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertIsInstance(assignment, DecisionFailure)
        assert isinstance(assignment, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_REQUIREMENT_INVALID, assignment.code)
        self.assertEqual(before, store.snapshot())

        requested = replace(
            template,
            reservation_id=ReservationId("unrequired-claim"),
            instance_id=instance.instance_id,
            reservation_generation=1,
            instance_subject_revision=instance.subject_revision,
            observation_generation=observation.generation,
            observation_digest=observation.digest,
            lease_id=LeaseId("unrequired-use"),
            generation=1,
            expires_at=SQLITE_NOW + timedelta(minutes=4),
            state=UseLeaseState.ACTIVE,
        )
        claimed = claim_resource(
            store,
            ClaimResourceOperation(
                definition,
                instance,
                observation,
                authority,
                requested,
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertIsInstance(claimed, DecisionFailure)
        assert isinstance(claimed, DecisionFailure)
        self.assertEqual(DecisionFailureCode.RESOURCE_REQUIREMENT_INVALID, claimed.code)
        self.assertEqual(before, store.snapshot())

    def test_attempt_transfer_rejects_terminal_work_without_mutation(self) -> None:
        store = self._store()
        complete_action = self._coordinator_action(store, ActionKind.COMPLETE)
        complete_command = bind_transition(complete_action, EvidenceInput("Terminal transfer must stay fenced."))
        assert not isinstance(complete_command, DecisionFailure)
        completed = execute(store, complete_command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(completed, DecisionFailure)
        terminal = store.snapshot()
        snapshot = project_decision_snapshot(terminal)
        proof = project_inactive_attempt_authority(
            terminal,
            AttemptId("work-a-1"),
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertNotIsInstance(proof, DecisionFailure)
        assert not isinstance(proof, DecisionFailure)
        coordination = snapshot.coordination_authority
        assert coordination is not None

        rejected = change_attempt_authority(
            store,
            TransferAttemptAuthority(
                proof,
                coordination,
                TaskId("terminal-worker"),
                HostId("host-a"),
                LeaseId("terminal-attempt"),
                SQLITE_NOW + timedelta(seconds=3),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, rejected.code)
        self.assertEqual(terminal, store.snapshot())

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
        recorded = record_planning_impact(
            store, RecordPlanningImpactOperation(impact, impact_authority, SQLITE_NOW + timedelta(seconds=1))
        )
        self.assertNotIsInstance(recorded, DecisionFailure)
        before_resolution = store.snapshot()
        coordination = project_decision_snapshot(before_resolution).coordination_authority
        assert coordination is not None

        receipt = resolve_planning_obligation(
            store,
            ResolvePlanningObligationOperation(
                impact.impact_id,
                ItemId("work-c"),
                SupersededPlanningDisposition(
                    "The replacement now owns the accepted outcome.",
                    "The replacement retains the accepted outcome.",
                    (ItemId("legacy-work"),),
                ),
                coordination,
                NoAttemptPlanningAuthority(ItemId("work-c")),
                SQLITE_NOW + timedelta(seconds=2),
            ),
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
            ResolvePlanningObligationOperation(
                impact.impact_id,
                ItemId("work-c"),
                SupersededPlanningDisposition(
                    "Try the same resolution again.",
                    "The replacement retains the accepted outcome.",
                    (ItemId("legacy-work"),),
                ),
                replace(coordination, generation=coordination.generation + 1),
                NoAttemptPlanningAuthority(ItemId("work-c")),
                SQLITE_NOW + timedelta(seconds=3),
            ),
        )
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(unchanged, store.snapshot())

    def test_live_planning_revision_installs_scope_but_leaves_attempt_acceptance_stale(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        coordination = snapshot.coordination_authority
        assert coordination is not None
        source = next(value for value in snapshot.scopes if value.item == ItemId("work-c"))
        target = next(value for value in snapshot.scopes if value.item == ItemId("work-a"))
        impact = PlanningImpact(
            PlanningImpactId("impact-live-revised"),
            source.item,
            None,
            source.revision,
            source.digest,
            "The queued item changes active work.",
            "The target scope must be revised explicitly.",
            (PlanningObligation(target.item, 0, target.revision, target.digest),),
        )
        recorded = record_planning_impact(
            store,
            RecordPlanningImpactOperation(impact, coordination, SQLITE_NOW + timedelta(seconds=1)),
        )
        self.assertNotIsInstance(recorded, DecisionFailure)
        current = project_decision_snapshot(store.snapshot())
        live = current.command_attempt_authorities[0]
        current_coordination = current.coordination_authority
        assert current_coordination is not None

        resolved = resolve_planning_obligation(
            store,
            ResolvePlanningObligationOperation(
                impact.impact_id,
                target.item,
                RevisedPlanningDisposition(
                    "The target accepts a new semantic scope.",
                    replace(target.scope, user_label="Revised active work"),
                ),
                current_coordination,
                LivePlanningAttemptAuthority(live),
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )

        self.assertNotIsInstance(resolved, DecisionFailure)
        after = project_decision_snapshot(store.snapshot())
        revised = next(value for value in after.scopes if value.item == target.item)
        attempt = after.attempt(live.attempt)
        assert attempt is not None
        self.assertEqual(target.revision + 1, revised.revision)
        self.assertEqual(target.revision, attempt.accepted_scope_revision)

    def test_live_planning_block_and_terminal_resolution_apply_complete_resource_effects(self) -> None:
        for disposition, terminal in (
            (BlockedPlanningDisposition("The target must pause for reconciliation."), False),
            (
                SupersededPlanningDisposition(
                    "The replacement owns the outcome.",
                    "The replacement preserves the accepted outcome.",
                    (ItemId("work-c"),),
                ),
                True,
            ),
        ):
            with self.subTest(disposition=type(disposition).__name__):
                store = self._store()
                snapshot = project_decision_snapshot(store.snapshot())
                coordination = snapshot.coordination_authority
                assert coordination is not None
                source = next(value for value in snapshot.scopes if value.item == ItemId("work-c"))
                target = next(value for value in snapshot.scopes if value.item == ItemId("work-a"))
                impact = PlanningImpact(
                    PlanningImpactId(f"impact-live-{terminal}"),
                    source.item,
                    None,
                    source.revision,
                    source.digest,
                    "The queued item changes active work.",
                    "The target requires an explicit lifecycle disposition.",
                    (PlanningObligation(target.item, 0, target.revision, target.digest),),
                )
                recorded = record_planning_impact(
                    store,
                    RecordPlanningImpactOperation(impact, coordination, SQLITE_NOW + timedelta(seconds=1)),
                )
                self.assertNotIsInstance(recorded, DecisionFailure)
                current = project_decision_snapshot(store.snapshot())
                live = current.command_attempt_authorities[0]
                current_coordination = current.coordination_authority
                assert current_coordination is not None
                if terminal:
                    target_authority = LivePlanningAttemptAuthority(live)
                    resolved_at = SQLITE_NOW + timedelta(seconds=2)
                else:
                    released = change_attempt_authority(
                        store,
                        ReleaseAttemptAuthority(live, SQLITE_NOW + timedelta(seconds=2)),
                    )
                    self.assertNotIsInstance(released, DecisionFailure)
                    proof = project_inactive_attempt_authority(
                        store.snapshot(),
                        live.attempt,
                        SQLITE_NOW + timedelta(seconds=3),
                    )
                    self.assertNotIsInstance(proof, DecisionFailure)
                    assert not isinstance(proof, DecisionFailure)
                    target_authority = InterruptedPlanningAttemptAuthority(proof)
                    current_coordination = project_decision_snapshot(store.snapshot()).coordination_authority
                    assert current_coordination is not None
                    resolved_at = SQLITE_NOW + timedelta(seconds=3)
                resolved = resolve_planning_obligation(
                    store,
                    ResolvePlanningObligationOperation(
                        impact.impact_id,
                        target.item,
                        disposition,
                        current_coordination,
                        target_authority,
                        resolved_at,
                    ),
                )
                self.assertNotIsInstance(resolved, DecisionFailure)
                after = store.snapshot()
                if terminal:
                    self.assertEqual("closed", after.lifecycle.attempts[0].state.value)
                    self.assertTrue(all(value.state.value == "released" for value in after.resources.reservations))
                    self.assertEqual("revoked", after.authority.attempt_leases[0].state.value)
                else:
                    self.assertEqual("blocked", after.lifecycle.attempts[0].state.value)
                    self.assertEqual("revoked", after.authority.attempt_leases[0].state.value)
                    self.assertTrue(all(value.state.value == "active" for value in after.resources.reservations))
                    self.assertFalse(any(value.state.value == "active" for value in after.resources.use_leases))

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
            RecordPlanningImpactOperation(impact, authority, SQLITE_NOW + timedelta(seconds=2)),
        )
        self.assertIsInstance(rejected_impact, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PLANNING_IMPACT_SOURCE_TERMINAL, rejected_impact.code)
        self.assertFalse(any(value.impact_id == impact.impact_id for value in impact_store.snapshot().planning.impacts))

        impact_store, completion_store = self._store_pair()
        action = self._coordinator_action(completion_store, ActionKind.COMPLETE)
        authority = project_decision_snapshot(completion_store.snapshot()).command_attempt_authorities[0]
        completion = bind_transition(action, EvidenceInput("The accepted outcome is complete."))
        self.assertNotIsInstance(completion, DecisionFailure)
        recorded = record_planning_impact(
            impact_store,
            RecordPlanningImpactOperation(impact, authority, SQLITE_NOW + timedelta(seconds=1)),
        )
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

    def test_fenced_intent_resolution_uses_coordination_only_and_never_recreates_authority(self) -> None:
        interrupted, capability, intent, prior_use = self._interrupted_resource_state()
        counter = next(
            value for value in interrupted.resources.reservation_counters if value.instance_id == intent.instance_id
        )
        next_generation = counter.generation_high_water + 1
        fenced_state = replace(
            interrupted,
            resources=replace(
                interrupted.resources,
                reservation_counters=tuple(
                    replace(value, generation_high_water=next_generation)
                    if value.instance_id == intent.instance_id
                    else value
                    for value in interrupted.resources.reservation_counters
                ),
                reservations=tuple(
                    replace(value, state=value.state.REVOKED_PENDING_RECOVERY)
                    if value.reservation_id == intent.reservation_id
                    else value
                    for value in interrupted.resources.reservations
                ),
                use_leases=(
                    *(
                        replace(value, state=UseLeaseState.REVOKED) if value.lease_id == prior_use.lease_id else value
                        for value in interrupted.resources.use_leases
                    ),
                    replace(
                        prior_use,
                        lease_id=LeaseId("use-fence-service"),
                        generation=prior_use.generation + 1,
                        generation_kind=UseLeaseGenerationKind.FENCE,
                        state=UseLeaseState.REVOKED,
                    ),
                ),
            ),
        )
        store, _database_path = self._store_with_state(fenced_state)
        snapshot = project_decision_snapshot(store.snapshot())
        coordination = snapshot.coordination_authority
        instance = snapshot.resource_instance(intent.instance_id)
        observation = snapshot.resource_observation(intent.instance_id)
        definition = snapshot.resource_definition(capability.resource_id)
        assert coordination is not None
        assert instance is not None
        assert observation is not None
        assert definition is not None
        before_authority = store.snapshot().authority
        value = ResolveFencedIntentInput(
            ResourceIntentCapability(capability, intent.intent_id, intent.policy_digest, intent.state),
            coordination,
            ObservedResource(
                instance.instance_id,
                instance.host_id,
                definition.kind,
                instance.discovery_fingerprint,
                observation.locator_schema,
                observation.locator,
                observation.digest,
                SQLITE_NOW + timedelta(seconds=3),
            ),
            next_generation,
            FencedIntentDisposition.UNCHANGED,
            "The fenced mutation did not run.",
            None,
            None,
            None,
            None,
            SQLITE_NOW + timedelta(seconds=3),
        )

        receipt = resolve_fenced_resource_intent(store, value)

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        self.assertEqual(before_authority, after.authority)
        self.assertEqual(MutationIntentState.ABANDONED, after.resources.mutation_intents[-1].state)
        self.assertEqual("revoked", after.resources.reservations[0].state.value)
        self.assertFalse(any(value.state == UseLeaseState.ACTIVE for value in after.resources.use_leases))

    def test_interrupted_reconciliation_and_human_preservation_commit_complete_evidence(self) -> None:
        for preserve in (False, True):
            with self.subTest(preserve=preserve):
                interrupted, capability, stored_intent, _prior_use = self._interrupted_resource_state()
                store, _database_path = self._store_with_state(interrupted)
                snapshot = project_decision_snapshot(store.snapshot())
                authority = snapshot.command_attempt_authorities[0]
                coordination = snapshot.coordination_authority
                intent = next(
                    value for value in snapshot.mutation_intents if value.intent_id == stored_intent.intent_id
                )
                instance = snapshot.resource_instance(intent.instance_id)
                definition = snapshot.resource_definition(capability.resource_id)
                observation = snapshot.resource_observation(intent.instance_id)
                assert coordination is not None
                assert instance is not None
                assert definition is not None
                assert observation is not None
                intent_capability = ResourceIntentCapability(
                    capability,
                    intent.intent_id,
                    intent.policy_digest,
                    intent.state,
                )
                observed = ObservedResource(
                    instance.instance_id,
                    instance.host_id,
                    definition.kind,
                    instance.discovery_fingerprint,
                    observation.locator_schema,
                    observation.locator,
                    "f" * 64,
                    SQLITE_NOW + timedelta(seconds=3),
                )
                if preserve:
                    result = preserve_resource_state(
                        store,
                        PreserveResourceStateInput(
                            intent_capability,
                            coordination,
                            authority,
                            observed,
                            LeaseId("preserve-fence"),
                            "Keep the inspected resource state.",
                            None,
                            None,
                            None,
                            SQLITE_NOW + timedelta(seconds=3),
                        ),
                    )
                    expected = MutationIntentState.HUMAN_PRESERVED
                else:
                    result = reconcile_interrupted_observation(
                        store,
                        ReconcileInterruptedObservationInput(
                            intent_capability,
                            authority,
                            observed,
                            "change-evidence/v1",
                            CanonicalJson(b"{}"),
                            "e" * 64,
                            ResolverEvidenceDecision.ACCEPTED,
                            SQLITE_NOW + timedelta(seconds=3),
                        ),
                    )
                    expected = MutationIntentState.RECONCILED

                self.assertNotIsInstance(result, DecisionFailure)
                final = store.snapshot()
                self.assertEqual(expected, final.resources.mutation_intents[-1].state)
                self.assertEqual("f" * 64, final.resources.mutation_intents[-1].result_observation_digest)

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
