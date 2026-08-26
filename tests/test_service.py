import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.service import (
    change_attempt_authority,
    change_coordination_authority,
    create_proposal,
    execute,
)
from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.authority_decisions import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AttemptLeaseStatus,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
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
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AcceptProposalInput,
    EvidenceInput,
    MergeProposalInput,
    ProposalRelationKind,
    ReasonInput,
    SubmitReviewInput,
    Timing,
    TransferCoordinatorInput,
)
from charlie_pinboard.domain.proposal_decisions import CreateProposalOperation, ProposalIntake
from tests.support import SQLITE_NOW, complete_sqlite_state


class ServiceTest(unittest.TestCase):
    def _store(self) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        return store

    def _store_with_state(self, state: StoredWorkState) -> tuple[SQLiteWorkStore, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        return store, roots.database_path

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
        self.assertEqual(len(before.transition_receipts) + 1, len(after.transition_receipts))

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
        self.assertEqual(len(state.transition_receipts) + 2, len(after.transition_receipts))
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

    def test_attempt_authority_renewal_and_release_persist_exact_generation(self) -> None:
        state = complete_sqlite_state()
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
        self.assertEqual(AttemptLeaseStatus.RELEASED, after.authority.attempt_leases[0].state)

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
        self.assertEqual(AttemptLeaseStatus.REVOKED, normal_store.snapshot().authority.attempt_leases[0].state)

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

    def test_completion_fences_attempt_authority_atomically(self) -> None:
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
        self.assertEqual(AttemptLeaseStatus.REVOKED, current_attempt.state)

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
        self.assertEqual(recorded_at, after.transition_receipts[-1].committed_at)

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


if __name__ == "__main__":
    unittest.main()
