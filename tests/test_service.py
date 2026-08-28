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
from charlie_pinboard.application.stored_state import (
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
)
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.authority_models import (
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
    available_actions,
    bind_transition,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.proposal_models import (
    CreateProposalOperation,
    ProposalIntake,
)
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

    def _coordinator_action(
        self, store: SQLiteWorkStore, kind: decision_models.ActionKind, subject: str | None = None
    ) -> decision_models.Action:
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.coordination_authority
        assert authority is not None
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR,
            decision_models.AuthorizationKind.COORDINATION,
            authority.generation,
            LeaseId(authority.lease_id),
        )
        result = available_actions(snapshot, actor)
        self.assertIsInstance(result, tuple)
        return next(
            action
            for action in result
            if action.kind == kind and (subject is None or str(action.capability.subject) == subject)
        )

    def _worker_action(self, store: SQLiteWorkStore, kind: decision_models.ActionKind) -> decision_models.Action:
        snapshot = project_decision_snapshot(store.snapshot())
        authority = snapshot.command_attempt_authorities[0]
        actor = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            authority.generation,
            authority.lease_id,
            (authority.attempt,),
            False,
        )
        result = available_actions(snapshot, actor)
        self.assertIsInstance(result, tuple)
        return next(action for action in result if action.kind == kind)

    def test_execute_rediscovers_and_commits_one_transition_from_the_locked_snapshot(self) -> None:
        store = self._store()
        before = store.snapshot()
        action = self._coordinator_action(store, decision_models.ActionKind.PAUSE)
        result = bind_transition(action, work_models.ReasonInput("Pause at a stable checkpoint."))
        self.assertNotIsInstance(result, DecisionFailure)

        outcome = execute(store, result, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(outcome, DecisionFailure)
        after = store.snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.transition_receipts) + 1, len(after.transition_receipts))

    def test_execute_accepts_exact_live_worker_authority_for_review_submission(self) -> None:
        store = self._store()
        action = self._worker_action(store, decision_models.ActionKind.SUBMIT_REVIEW)
        command = bind_transition(action, work_models.SubmitReviewInput(CandidateId("candidate-review")))
        self.assertNotIsInstance(command, DecisionFailure)
        receipt = execute(store, command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(receipt, DecisionFailure)
        self.assertEqual("review", store.snapshot().lifecycle.attempts[0].state.value)

    def test_positive_item_state_variants_reload_from_fresh_stores(self) -> None:
        for kind, initial, payload, expected in (
            (
                decision_models.ActionKind.MARK_READY,
                StoredWorkItemState.INTAKE,
                work_models.ReasonInput("The intake is ready."),
                StoredWorkItemState.READY,
            ),
            (
                decision_models.ActionKind.BLOCK_ITEM,
                StoredWorkItemState.INTAKE,
                work_models.BlockInput("The intake awaits a dependency."),
                StoredWorkItemState.BLOCKED,
            ),
            (
                decision_models.ActionKind.REOPEN,
                StoredWorkItemState.DEFERRED,
                work_models.EvidenceInput("The prerequisite is now available."),
                StoredWorkItemState.INTAKE,
            ),
        ):
            with self.subTest(kind=kind):
                state = complete_sqlite_state()
                lifecycle = state.lifecycle
                state = replace(
                    state,
                    lifecycle=replace(
                        lifecycle,
                        work_items=tuple(
                            replace(value, state=initial) if value.item_id == ItemId("intake-work") else value
                            for value in lifecycle.work_items
                        ),
                    ),
                )
                store, database_path = self._store_with_state(state)
                command = bind_transition(
                    self._coordinator_action(store, kind, "intake-work"),
                    payload,
                )
                assert not isinstance(command, DecisionFailure)

                result = execute(store, command, SQLITE_NOW + timedelta(seconds=1))

                self.assertNotIsInstance(result, DecisionFailure)
                reloaded = SQLiteWorkStore(database_path).snapshot()
                item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("intake-work"))
                self.assertEqual(expected, item.state)

    def test_positive_attempt_state_variants_reload_from_fresh_stores(self) -> None:
        for kind, payload, expected in (
            (
                decision_models.ActionKind.PAUSE,
                work_models.ReasonInput("Pause at a stable point."),
                work_models.AttemptState.PAUSED,
            ),
            (
                decision_models.ActionKind.BLOCK,
                work_models.BlockInput("The attempt awaits a dependency."),
                work_models.AttemptState.BLOCKED,
            ),
        ):
            with self.subTest(kind=kind):
                store, database_path = self._store_with_state(complete_sqlite_state())
                command = bind_transition(self._coordinator_action(store, kind), payload)
                assert not isinstance(command, DecisionFailure)

                result = execute(store, command, SQLITE_NOW + timedelta(seconds=1))

                self.assertNotIsInstance(result, DecisionFailure)
                reloaded = SQLiteWorkStore(database_path).snapshot()
                item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
                attempt = next(
                    value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
                )
                self.assertEqual(StoredWorkItemState(expected.value), item.state)
                self.assertEqual(expected, attempt.state)

    def test_checkpoint_acceptance_preserves_supplied_candidate_and_reloads_from_fresh_store(self) -> None:
        store, database_path = self._store_with_state(complete_sqlite_state())
        submit = bind_transition(
            self._worker_action(store, decision_models.ActionKind.SUBMIT_REVIEW),
            work_models.SubmitReviewInput(CandidateId("protected-candidate")),
        )
        assert not isinstance(submit, DecisionFailure)
        submitted = execute(store, submit, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(submitted, DecisionFailure)
        submitted_store = SQLiteWorkStore(database_path)
        submitted_attempt = submitted_store.snapshot().lifecycle.attempts[0]
        self.assertEqual("protected-candidate", submitted_attempt.candidate_revision)
        accept = bind_transition(
            self._coordinator_action(submitted_store, decision_models.ActionKind.ACCEPT_CHECKPOINT),
            work_models.AcceptCheckpointInput(
                CheckpointId("checkpoint-a"),
                CandidateId("supplied-different-candidate"),
                "Checkpoint evidence is accepted.",
            ),
        )
        assert not isinstance(accept, DecisionFailure)

        accepted = execute(submitted_store, accept, SQLITE_NOW + timedelta(seconds=2))

        self.assertNotIsInstance(accepted, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(StoredWorkItemState.PAUSED, item.state)
        self.assertEqual(work_models.AttemptState.PAUSED, attempt.state)
        self.assertIsNone(attempt.candidate_revision)
        self.assertEqual(AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)
        outcome = reloaded.transition_receipts[-1].outcome_payload
        self.assertIn(b'"candidate":"supplied-different-candidate"', outcome)
        self.assertIn(b'"checkpoint":"checkpoint-a"', outcome)

    def test_review_acceptance_continues_the_attempt_and_reloads_every_fact(self) -> None:
        store, database_path = self._store_with_state(complete_sqlite_state())
        submit = bind_transition(
            self._worker_action(store, decision_models.ActionKind.SUBMIT_REVIEW),
            work_models.SubmitReviewInput(CandidateId("protected-candidate")),
        )
        assert not isinstance(submit, DecisionFailure)
        submitted = execute(store, submit, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(submitted, DecisionFailure)
        submitted_store = SQLiteWorkStore(database_path)
        mismatch = bind_transition(
            self._coordinator_action(submitted_store, decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE),
            work_models.AcceptReviewAndContinueInput(CandidateId("different-candidate"), "This must not commit."),
        )
        assert not isinstance(mismatch, DecisionFailure)
        before_mismatch = submitted_store.snapshot()
        rejected = execute(submitted_store, mismatch, SQLITE_NOW + timedelta(seconds=2))
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
        self.assertEqual(before_mismatch, submitted_store.snapshot())
        accept = bind_transition(
            self._coordinator_action(submitted_store, decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE),
            work_models.AcceptReviewAndContinueInput(
                CandidateId("protected-candidate"),
                "The reviewed checkpoint is accepted; continue this attempt.",
            ),
        )
        assert not isinstance(accept, DecisionFailure)

        accepted = execute(submitted_store, accept, SQLITE_NOW + timedelta(seconds=3))

        self.assertNotIsInstance(accepted, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(StoredWorkItemState.ACTIVE, item.state)
        self.assertEqual(work_models.AttemptState.ACTIVE, attempt.state)
        self.assertIsNone(attempt.candidate_revision)
        self.assertIsNone(attempt.candidate_recorded_at)
        self.assertEqual(AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)
        self.assertEqual("continue", reloaded.focus.next_action)
        receipt = reloaded.transition_receipts[-1]
        self.assertEqual(TransitionHistoryActionKind.ACCEPT_REVIEW_AND_CONTINUE, receipt.action_kind)
        self.assertEqual("transition-receipt/v1", receipt.outcome_schema)
        self.assertIn(b'"candidate":"protected-candidate"', receipt.outcome_payload)
        self.assertIn(
            b'"evidence":"The reviewed checkpoint is accepted; continue this attempt."', receipt.outcome_payload
        )
        self.assertNotIn(b'"checkpoint"', receipt.outcome_payload)

    def test_retained_attempt_closure_fences_authority_and_reloads_from_fresh_store(self) -> None:
        state = complete_sqlite_state()
        lifecycle = state.lifecycle
        state = replace(
            state,
            lifecycle=replace(
                lifecycle,
                work_items=tuple(
                    replace(value, state=StoredWorkItemState.PAUSED) if value.item_id == ItemId("work-a") else value
                    for value in lifecycle.work_items
                ),
                attempts=tuple(
                    replace(value, state=work_models.AttemptState.PAUSED)
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in lifecycle.attempts
                ),
            ),
        )
        store, database_path = self._store_with_state(state)
        close = bind_transition(
            self._coordinator_action(store, decision_models.ActionKind.CLOSE, "work-a"),
            work_models.CloseInput(work_models.CloseOutcome.DROPPED, "The retained attempt is no longer needed."),
        )
        assert not isinstance(close, DecisionFailure)

        closed = execute(store, close, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(closed, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(StoredWorkItemState.DROPPED, item.state)
        self.assertEqual("The retained attempt is no longer needed.", item.outcome_evidence)
        self.assertEqual(work_models.AttemptState.DONE, attempt.state)
        self.assertEqual(AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)
        self.assertIsNone(reloaded.focus.attempt_id)

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
        action = self._coordinator_action(store, decision_models.ActionKind.PAUSE)
        result = bind_transition(action, work_models.ReasonInput("Pause at a stable checkpoint."))
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
        action = self._coordinator_action(store, decision_models.ActionKind.TRANSFER_COORDINATOR)
        command = bind_transition(
            action, work_models.TransferCoordinatorInput(TaskId("next-task"), HostId("next-host"))
        )
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
        action = self._coordinator_action(store, decision_models.ActionKind.COMPLETE)
        command = bind_transition(action, work_models.EvidenceInput("The accepted outcome is complete."))
        assert not isinstance(command, DecisionFailure)

        receipt = execute(store, command, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        current_attempt = after.authority.attempt_leases[0]
        self.assertEqual(prior_attempt.generation + 1, current_attempt.generation)
        self.assertEqual(AttemptLeaseStatus.REVOKED, current_attempt.state)

    def test_sqlite_proposal_intake_is_visible_at_the_back_without_changing_current_work(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()),
            authority=replace(state.authority, coordination=None),
        )
        store, _database_path = self._store_with_state(state)
        before = store.snapshot()
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
            work_models.ProposalRelationKind.INDEPENDENT,
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
        visible = next(value for value in after.lifecycle.work_items if value.item_id == ItemId("sqlite-proposal"))
        self.assertEqual(StoredWorkItemState.INTAKE, visible.state)
        self.assertEqual(5, visible.queue_position)
        self.assertEqual("proposal:sqlite-proposal", visible.source)
        self.assertEqual(before.focus, after.focus)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertEqual(created_at, proposal.created_at)
        self.assertEqual(recorded_at, proposal.recorded_at)
        self.assertEqual(recorded_at, after.lifecycle.project.updated_at)
        self.assertEqual(recorded_at, after.transition_receipts[-1].committed_at)

    def test_proposal_requested_position_and_prerequisite_relation_update_one_transaction(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()))
        store, _database_path = self._store_with_state(state)
        before = store.snapshot()
        target_before = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-c"))
        intake = ProposalIntake(
            ProposalId("required-first"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Required first",
            "Work C needs one newly discovered prerequisite.",
            "The dependency must be visible before scheduling.",
            "Record the prerequisite candidate and relationship.",
            "A coordinator can evaluate it in queue order.",
            work_models.ProposalRelationKind.PREREQUISITE,
            ItemId("work-c"),
            "The relationship is current.",
            ("source:local",),
            ("Work C remains live.",),
            2,
        )

        receipt = create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.snapshot()
        positions = {
            str(value.item_id): value.queue_position
            for value in after.lifecycle.work_items
            if value.queue_position is not None
        }
        self.assertEqual(
            {
                "intake-work": 1,
                "required-first": 2,
                "work-a": 3,
                "work-c": 4,
                "zz-proposal-a": 5,
            },
            positions,
        )
        target_after = next(value for value in after.lifecycle.work_items if value.item_id == ItemId("work-c"))
        self.assertEqual(target_before.scope_revision + 1, target_after.scope_revision)
        self.assertIn(
            ItemId("required-first"),
            tuple(value.dependency_id for value in after.lifecycle.dependencies if value.item_id == ItemId("work-c")),
        )
        self.assertEqual(before.focus, after.focus)

    def test_proposal_position_outside_the_live_queue_is_rejected_atomically(self) -> None:
        store = self._store()
        intake = ProposalIntake(
            ProposalId("invalid-position"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Invalid position",
            "The requested queue position exceeds the live bounds.",
            "Invalid insertion must not partially persist.",
            "Reject the request before mutation.",
            "Keep the prior queue intact.",
            work_models.ProposalRelationKind.INDEPENDENT,
            None,
            "The queue currently contains four live items.",
            (),
            (),
            6,
        )
        before = store.snapshot()

        result = create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PROPOSAL_INVALID, result.code)
        self.assertEqual(before, store.snapshot())

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
            work_models.ProposalRelationKind.FOLLOW_UP,
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
        accept = self._coordinator_action(dependency_store, decision_models.ActionKind.ACCEPT_PROPOSAL)
        dependency_command = bind_transition(
            accept,
            work_models.AcceptProposalInput(
                ItemId("zz-proposal-a"),
                work_models.AcceptedProposalState.INTAKE,
                "review-intake",
                work_models.Timing.SAFE_TO_DEFER,
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
        merge = self._coordinator_action(merge_store, decision_models.ActionKind.MERGE_PROPOSAL)
        merge_command = bind_transition(merge, work_models.MergeProposalInput(ItemId("missing-target")))
        assert not isinstance(merge_command, DecisionFailure)
        before = merge_store.snapshot()
        merge_rejected = execute(merge_store, merge_command, SQLITE_NOW + timedelta(seconds=1))
        self.assertIsInstance(merge_rejected, DecisionFailure)
        assert isinstance(merge_rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, merge_rejected.code)
        self.assertEqual(before, merge_store.snapshot())

    def test_attempt_transfer_rejects_terminal_work_without_mutation(self) -> None:
        store = self._store()
        complete_action = self._coordinator_action(store, decision_models.ActionKind.COMPLETE)
        complete_command = bind_transition(
            complete_action, work_models.EvidenceInput("Terminal transfer must stay fenced.")
        )
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
