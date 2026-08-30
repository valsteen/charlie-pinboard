import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
)
from pinboard.application.mutations import project_transition_mutation
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_models import (
    AttemptAuthorityDecision,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    CoordinationAuthorityDecision,
)
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import bind_transition as bind_transition_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    ItemId,
)
from pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, reject_table_deletes


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def bind_transition(
    action: decision_models.Action, value: work_models.TransitionInput
) -> decision_models.TransitionCommand:
    return expect_success(bind_transition_outcome(action, value))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.TransitionDecision:
    result = expect_success(decision_outcome(snapshot, command, now))
    if not isinstance(result, decision_models.TransitionDecision):
        raise AssertionError(f"Expected a non-checkpoint decision, received {result!r}")
    return result


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

    def _store_with_state(self, state: stored_state.StoredWorkState) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        return store

    def _coordination_decision(
        self,
        before: stored_state.StoredWorkState,
        *,
        extension: timedelta = timedelta(minutes=1),
    ) -> CoordinationAuthorityDecision:
        retained = project_decision_snapshot(before).coordination_lease
        assert retained is not None
        return CoordinationAuthorityDecision(retained, replace(retained, expires_at=retained.expires_at + extension))

    def _attempt_renewal_decision(self, before: stored_state.StoredWorkState) -> AttemptAuthorityDecision:
        snapshot = project_decision_snapshot(before)
        command = snapshot.command_attempt_authorities[0]
        stored = before.authority.attempt_leases[0]
        retained = AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            stored.acquired_at,
            command.expires_at,
            AttemptLeaseStatus.ACTIVE,
        )
        return AttemptAuthorityDecision(
            command.attempt,
            command.generation,
            command.generation,
            retained,
            replace(retained, expires_at=retained.expires_at + timedelta(minutes=1)),
        )

    def _receipt_state(
        self, before: stored_state.StoredWorkState, action: str
    ) -> tuple[decision_models.TransitionReceipt, stored_state.StoredWorkState]:
        decided_at = before.lifecycle.project.updated_at + timedelta(seconds=1)
        receipt = decision_models.TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
        stored_receipt = stored_state.StoredTransitionReceipt(
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            receipt.action_id,
            stored_state.TransitionHistoryActionKind.INSPECT,
            HistorySubjectId("work-a"),
            None,
            stored_state.TransitionHistoryAuthorizationKind.COORDINATOR,
            None,
            None,
            "mutation/v1",
            work_models.CanonicalJson(b"{}"),
            "transition-receipt/v1",
            work_models.CanonicalJson(
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
                project=replace(
                    before.lifecycle.project,
                    revision=before.lifecycle.project.revision + 1,
                    updated_at=decided_at,
                ),
            ),
            transition_receipts=(*before.transition_receipts, stored_receipt),
        )
        return receipt, after

    def _stored_receipt(self, after: stored_state.StoredWorkState) -> stored_state.StoredTransitionReceipt:
        return after.transition_receipts[-1]

    def _mutation_receipt(
        self, receipt: decision_models.TransitionReceipt, after: stored_state.StoredWorkState
    ) -> MutationReceipt:
        stored = self._stored_receipt(after)
        return MutationReceipt(
            receipt,
            stored.history_id,
            stored.project_revision,
            stored.action_kind,
            stored.subject_id,
            stored.artifact_ref_id,
            stored.authorization,
            stored.actor_task_id,
            stored.actor_host_id,
            stored.input_schema,
            stored.input_payload,
        )

    def test_activation_decision_retains_and_persists_creation_facts(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.ACTIVATE
        )
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        decision = decide(
            snapshot,
            bind_transition(
                action,
                work_models.ActivateInput(
                    AttemptId("work-c-1"),
                    "codex/work-c",
                    "base-c",
                    "worker-c",
                    ArtifactRefId(1),
                ),
            ),
            decided_at,
        )

        self.assertIsInstance(decision.change, decision_models.ActivationChange)
        assert isinstance(decision.change, decision_models.ActivationChange)
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (
                decision.change.branch,
                decision.change.base_revision,
                decision.change.owner,
                decision.change.brief_artifact_ref_id,
            ),
        )
        with reject_table_deletes("work_items"), store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        reopened = store.snapshot()
        attempt = next(value for value in reopened.lifecycle.attempts if value.attempt_id == AttemptId("work-c-1"))
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (attempt.branch, attempt.base_revision, attempt.provenance, attempt.brief_artifact_ref_id),
        )
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.transition_receipts))
        self.assertEqual("transition-receipt/v1", reopened.transition_receipts[-1].outcome_schema)

    def test_resume_replaces_the_attempt_brief_and_reloads_it_from_sqlite(self) -> None:
        state = complete_sqlite_state()
        revised_scope_digest = "c" * 64
        current = state.artifact_references[0]
        replacement = replace(
            current,
            artifact_ref_id=ArtifactRefId(99),
            revision=current.revision + 1,
            selector="artifacts/briefs/work-a/2.json",
            content_sha256="b" * 64,
            size_bytes=23,
        )
        attempt = state.lifecycle.attempts[0]
        items = tuple(
            replace(
                item,
                state=stored_state.StoredWorkItemState.PAUSED,
                scope_revision=2,
                scope_digest=revised_scope_digest,
            )
            if item.item_id == attempt.item_id
            else item
            for item in state.lifecycle.work_items
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=items,
                scope_revisions=(
                    *state.lifecycle.scope_revisions,
                    stored_state.ItemScopeRevision(
                        attempt.item_id,
                        2,
                        revised_scope_digest,
                        state.lifecycle.project.revision,
                        SQLITE_NOW,
                    ),
                ),
                dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != attempt.item_id),
                attempts=(replace(attempt, state=work_models.AttemptState.PAUSED),),
            ),
            artifact_references=(*state.artifact_references, replacement),
        )
        store = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.RESUME
        )
        decision = decide(
            snapshot, bind_transition(action, work_models.ResumeInput(replacement.artifact_ref_id)), SQLITE_NOW
        )

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        persisted = next(
            value for value in store.snapshot().lifecycle.attempts if value.attempt_id == attempt.attempt_id
        )
        self.assertEqual(work_models.AttemptState.ACTIVE, persisted.state)
        self.assertEqual(replacement.artifact_ref_id, persisted.brief_artifact_ref_id)
        self.assertEqual(2, persisted.accepted_scope_revision)
        self.assertEqual(revised_scope_digest, persisted.accepted_scope_digest)

    def test_proposal_acceptance_round_trips_semantics_and_ordered_dependencies(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.ACCEPT_PROPOSAL
        )
        decision = decide(
            snapshot,
            bind_transition(
                action,
                work_models.AcceptProposalInput(
                    ItemId("zz-proposal-a"),
                    work_models.AcceptedProposalState.READY,
                    "activate",
                    timing=None,
                    depends_on=(ItemId("intake-work"),),
                ),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )

        self.assertIsInstance(decision.change, decision_models.AcceptedProposalChange)
        with reject_table_deletes("work_items"), store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        reopened = store.snapshot()
        item = next(value for value in reopened.lifecycle.work_items if value.item_id == ItemId("zz-proposal-a"))
        proposal = reopened.proposals.proposals[0]
        self.assertEqual(
            ("Proposal A", "A related observation", "Record the follow-up."),
            (item.user_label, item.trigger, item.effect),
        )
        self.assertEqual(stored_state.StoredWorkItemState.READY, item.state)
        self.assertEqual(4, item.queue_position)
        self.assertEqual(5, len(reopened.lifecycle.work_items))
        self.assertEqual(
            (ItemId("work-c"), ItemId("intake-work")),
            tuple(value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == item.item_id),
        )
        self.assertEqual(
            work_models.AcceptedProposalDisposition(
                ItemId("zz-proposal-a"),
                SQLITE_NOW + timedelta(seconds=1),
            ),
            proposal.disposition,
        )
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.transition_receipts))

    def test_cross_family_stale_mutation_is_rejected_without_partial_state(self) -> None:
        first, second = self._store_pair()
        before = first.snapshot()
        self.assertEqual(before, second.snapshot())
        receipt, after = self._receipt_state(before, "inspect:first-writer")
        coordination = before.authority.coordination
        assert coordination is not None
        after = replace(
            after,
            authority=replace(
                before.authority,
                coordination=replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
            ),
        )
        first_mutation = CoordinationAuthorityMutation(
            self._mutation_receipt(receipt, after),
            self._coordination_decision(before),
        )
        with first.write() as transaction:
            transaction.commit(first_mutation)

        stale_receipt, stale_after = self._receipt_state(before, "inspect:stale-attempt-authority")
        stale_mutation = AttemptAuthorityMutation(
            self._mutation_receipt(stale_receipt, stale_after),
            self._attempt_renewal_decision(before),
        )
        with second.write() as transaction:
            rejected = transaction.commit(stale_mutation)
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)
        reloaded = second.snapshot()
        self.assertEqual(after, reloaded)
        self.assertEqual(before.authority.attempt_counters, reloaded.authority.attempt_counters)
        self.assertEqual(before.authority.attempt_generations, reloaded.authority.attempt_generations)
        self.assertEqual(before.authority.attempt_leases, reloaded.authority.attempt_leases)

    def test_proposal_disposition_matrix_persists_each_closed_value(self) -> None:
        for kind, payload, expected in (
            (
                decision_models.ActionKind.MERGE_PROPOSAL,
                work_models.MergeProposalInput(ItemId("work-c")),
                work_models.MergedProposalDisposition(
                    ItemId("work-c"),
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
            (
                decision_models.ActionKind.RETURN_PROPOSAL,
                work_models.ReasonInput("Clarify the evidence."),
                work_models.ReturnedProposalDisposition(
                    "Clarify the evidence.",
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
            (
                decision_models.ActionKind.REJECT_PROPOSAL,
                work_models.ReasonInput("The finding is obsolete."),
                work_models.RejectedProposalDisposition(
                    "The finding is obsolete.",
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
        ):
            with self.subTest(kind=kind):
                store = self._store()
                snapshot = project_decision_snapshot(store.snapshot())
                actor = decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
                )
                action = next(value for value in available_actions(snapshot, actor) if value.kind == kind)
                decision = decide(snapshot, bind_transition(action, payload), SQLITE_NOW + timedelta(seconds=1))
                with store.write() as transaction:
                    transaction.commit(project_transition_mutation(transaction.snapshot(), decision))
                proposal = store.snapshot().proposals.proposals[0]
                self.assertEqual(expected, proposal.disposition)

    def test_defer_decision_persists_reopen_focus(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.DEFER and value.capability.subject == ItemId("intake-work")
        )
        decision = decide(
            snapshot,
            bind_transition(
                action,
                work_models.DeferInput(work_models.Timing.SAFE_TO_DEFER, "Reopen when the prerequisite is accepted."),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))
        reopened = store.snapshot()
        self.assertEqual(stored_state.StoredWorkItemState.DEFERRED, reopened.lifecycle.work_items[0].state)
        self.assertEqual("reopen", reopened.focus.next_action)

    def test_real_stale_transition_effects_return_failure_and_roll_back(self) -> None:
        ignore_item_update = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            BEFORE UPDATE OF state ON work_items
            BEGIN
                SELECT RAISE(IGNORE);
            END
        """
        stale_attempt_after_item = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            AFTER UPDATE OF state ON work_items
            BEGIN
                UPDATE attempts SET subject_revision = subject_revision + 1
                WHERE attempt_id = 'work-a-1';
            END
        """
        stale_focus_after_attempt = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            AFTER UPDATE OF state ON attempts
            BEGIN
                UPDATE current_focus SET subject_revision = subject_revision + 1;
            END
        """
        stale_proposal_after_item = f"""
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            AFTER UPDATE OF state ON work_items
            BEGIN
                UPDATE proposals
                SET disposition = 'returned', disposition_reason = 'Competing disposition.',
                    disposition_recorded_at = '{SQLITE_NOW.isoformat()}'
                WHERE proposal_id = 'zz-proposal-a';
            END
        """
        ignore_proposal_update = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            BEFORE UPDATE OF disposition ON proposals
            BEGIN
                SELECT RAISE(IGNORE);
            END
        """
        scenarios: tuple[tuple[decision_models.ActionKind, work_models.TransitionInput, str], ...] = (
            (
                decision_models.ActionKind.PAUSE,
                work_models.ReasonInput("Pause at the checkpoint boundary."),
                ignore_item_update,
            ),
            (
                decision_models.ActionKind.PAUSE,
                work_models.ReasonInput("Pause at the checkpoint boundary."),
                stale_attempt_after_item,
            ),
            (
                decision_models.ActionKind.PAUSE,
                work_models.ReasonInput("Pause at the checkpoint boundary."),
                stale_focus_after_attempt,
            ),
            (
                decision_models.ActionKind.ACCEPT_PROPOSAL,
                work_models.AcceptProposalInput(
                    ItemId("zz-proposal-a"),
                    work_models.AcceptedProposalState.READY,
                    "activate",
                    timing=None,
                    depends_on=(ItemId("intake-work"),),
                ),
                ignore_item_update,
            ),
            (
                decision_models.ActionKind.ACCEPT_PROPOSAL,
                work_models.AcceptProposalInput(
                    ItemId("zz-proposal-a"),
                    work_models.AcceptedProposalState.READY,
                    "activate",
                    timing=None,
                    depends_on=(ItemId("intake-work"),),
                ),
                stale_proposal_after_item,
            ),
            (
                decision_models.ActionKind.MERGE_PROPOSAL,
                work_models.MergeProposalInput(ItemId("work-c")),
                ignore_item_update,
            ),
            (
                decision_models.ActionKind.MERGE_PROPOSAL,
                work_models.MergeProposalInput(ItemId("work-c")),
                stale_proposal_after_item,
            ),
            (
                decision_models.ActionKind.RETURN_PROPOSAL,
                work_models.ReasonInput("Clarify the evidence."),
                ignore_proposal_update,
            ),
            (
                decision_models.ActionKind.REJECT_PROPOSAL,
                work_models.ReasonInput("The finding is obsolete."),
                ignore_item_update,
            ),
            (
                decision_models.ActionKind.REJECT_PROPOSAL,
                work_models.ReasonInput("The finding is obsolete."),
                stale_proposal_after_item,
            ),
        )
        for kind, payload, trigger in scenarios:
            with self.subTest(kind=kind, trigger=trigger):
                project = Path(tempfile.mkdtemp()).resolve()
                roots = resolve_durable_roots(project)
                initialize_database(roots, SQLITE_NOW)
                store = SQLiteWorkStore(roots.database_path)
                store.initialize_state(complete_sqlite_state())
                before = store.snapshot()
                snapshot = project_decision_snapshot(before)
                actor = decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR,
                    decision_models.AuthorizationKind.COORDINATOR,
                    snapshot.generation,
                )
                action = next(value for value in available_actions(snapshot, actor) if value.kind == kind)
                decision = decide(
                    snapshot,
                    bind_transition(action, payload),
                    SQLITE_NOW + timedelta(seconds=1),
                )
                mutation = project_transition_mutation(before, decision)
                connection = open_database(roots.database_path, OpenMode.READ_WRITE)
                connection.execute(trigger)

                with (
                    patch("pinboard.adapters.sqlite.store.open_database", return_value=connection),
                    store.write() as transaction,
                ):
                    result = transaction.commit(mutation)

                self.assertIsInstance(result, DecisionFailure)
                assert isinstance(result, DecisionFailure)
                self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)
                self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
