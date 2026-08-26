import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.errors import StorageError
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
)
from charlie_pinboard.application.mutations import project_transition_mutation
from charlie_pinboard.application.ports import WorkStore, WorkTransaction
from charlie_pinboard.application.stored_state import (
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_models import (
    AttemptAuthorityDecision,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    CoordinationAuthorityDecision,
)
from charlie_pinboard.domain.decision_models import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
    TransitionReceipt,
)
from charlie_pinboard.domain.decisions import available_actions as available_actions_outcome
from charlie_pinboard.domain.decisions import bind_transition as bind_transition_outcome
from charlie_pinboard.domain.decisions import decide as decision_outcome
from charlie_pinboard.domain.history import HistoryOutcome
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.proposal_models import (
    ProposalCreationDecision,
    ProposalIntake,
)
from charlie_pinboard.domain.work_models import (
    AcceptedProposalState,
    AcceptProposalInput,
    ActivateInput,
    AttemptState,
    CanonicalJson,
    CloseInput,
    CloseOutcome,
    DeferInput,
    MergeProposalInput,
    ProposalDispositionKind,
    ProposalRelationKind,
    ReasonInput,
    ResumeInput,
    SubmitReviewInput,
    Timing,
    TransitionInput,
)
from tests.support import SQLITE_NOW, complete_sqlite_state


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    return cast(tuple[Action, ...], available_actions_outcome(snapshot, actor))


def bind_transition(action: Action, value: TransitionInput) -> TransitionCommand:
    return cast(TransitionCommand, bind_transition_outcome(action, value))


def decide(snapshot: LedgerSnapshot, command: TransitionCommand, now: datetime) -> Decision:
    return cast(Decision, decision_outcome(snapshot, command, now))


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

    def _store_with_state(self, state: StoredWorkState) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        return store

    def _proposal_decision(
        self,
        decided_at: datetime,
        proposal: StoredProposal | None = None,
    ) -> ProposalCreationDecision:
        if proposal is None:
            intake = ProposalIntake(
                ProposalId("proposal-carrier"),
                decided_at,
                TaskId("source-carrier"),
                "Carrier proposal",
                "A carrier test needs an accepted decision.",
                "The mutation remains decision-backed.",
                "Exercise the exact relational delta.",
                "Invalid carrier shapes are rejected.",
                ProposalRelationKind.INDEPENDENT,
                None,
                "No scheduling effect.",
                (),
                (),
            )
        else:
            intake = ProposalIntake(
                proposal.proposal_id,
                proposal.created_at,
                proposal.source_task_id,
                proposal.user_label,
                proposal.trigger,
                proposal.why_it_matters,
                proposal.effect,
                proposal.unlock,
                ProposalRelationKind(proposal.relation.value),
                proposal.relation_item_id,
                proposal.urgency_evidence,
                (),
                (),
            )
        return ProposalCreationDecision(intake, intake.evidence, intake.freshness_assumptions)

    def _coordination_decision(
        self,
        before: StoredWorkState,
        *,
        extension: timedelta = timedelta(minutes=1),
    ) -> CoordinationAuthorityDecision:
        retained = project_decision_snapshot(before).coordination_lease
        assert retained is not None
        return CoordinationAuthorityDecision(retained, replace(retained, expires_at=retained.expires_at + extension))

    def _attempt_renewal_decision(self, before: StoredWorkState) -> AttemptAuthorityDecision:
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

    def _receipt_state(self, before: StoredWorkState, action: str) -> tuple[TransitionReceipt, StoredWorkState]:
        decided_at = before.lifecycle.project.updated_at + timedelta(seconds=1)
        receipt = TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
        stored_receipt = StoredTransitionReceipt(
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            receipt.action_id,
            TransitionHistoryActionKind.INSPECT,
            HistorySubjectId("work-a"),
            None,
            TransitionHistoryAuthorizationKind.COORDINATOR,
            None,
            None,
            "mutation/v1",
            CanonicalJson(b"{}"),
            "transition-receipt/v1",
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
                project=replace(
                    before.lifecycle.project,
                    revision=before.lifecycle.project.revision + 1,
                    updated_at=decided_at,
                ),
            ),
            transition_receipts=(*before.transition_receipts, stored_receipt),
        )
        return receipt, after

    def _stored_receipt(self, after: StoredWorkState) -> StoredTransitionReceipt:
        return after.transition_receipts[-1]

    def _mutation_receipt(self, receipt: TransitionReceipt, after: StoredWorkState) -> MutationReceipt:
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

    def _history_outcome(self, after: StoredWorkState, outcome: HistoryOutcome) -> StoredWorkState:
        stored_receipt = replace(
            self._stored_receipt(after),
            outcome_schema=outcome.outcome_schema,
            outcome_payload=CanonicalJson(outcome.payload),
        )
        return replace(after, transition_receipts=(*after.transition_receipts[:-1], stored_receipt))

    def test_activation_decision_retains_and_persists_creation_facts(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACTIVATE)
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        decision = decide(
            snapshot,
            bind_transition(
                action,
                ActivateInput(
                    AttemptId("work-c-1"),
                    "codex/work-c",
                    "base-c",
                    "worker-c",
                    ArtifactRefId(1),
                ),
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
        current = state.artifact_references[0]
        replacement = replace(
            current,
            artifact_ref_id=ArtifactRefId(99),
            revision=current.revision + 1,
            selector="artifacts/briefs/work-a/2.md",
            content_sha256="b" * 64,
            size_bytes=23,
        )
        attempt = state.lifecycle.attempts[0]
        items = tuple(
            replace(item, state=StoredWorkItemState.PAUSED) if item.item_id == attempt.item_id else item
            for item in state.lifecycle.work_items
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=items,
                dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != attempt.item_id),
                attempts=(replace(attempt, state=AttemptState.PAUSED),),
            ),
            artifact_references=(*state.artifact_references, replacement),
        )
        store = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.RESUME)
        decision = decide(snapshot, bind_transition(action, ResumeInput(replacement.artifact_ref_id)), SQLITE_NOW)

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        persisted = next(
            value for value in store.snapshot().lifecycle.attempts if value.attempt_id == attempt.attempt_id
        )
        self.assertEqual(AttemptState.ACTIVE, persisted.state)
        self.assertEqual(replacement.artifact_ref_id, persisted.brief_artifact_ref_id)

    def test_proposal_acceptance_round_trips_semantics_and_ordered_dependencies(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACCEPT_PROPOSAL)
        decision = decide(
            snapshot,
            bind_transition(
                action,
                AcceptProposalInput(
                    ItemId("accepted-proposal"),
                    AcceptedProposalState.READY,
                    "activate",
                    timing=None,
                    depends_on=(ItemId("work-c"), ItemId("intake-work")),
                ),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )

        self.assertIsNotNone(decision.proposal_change)
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        reopened = store.snapshot()
        item = next(value for value in reopened.lifecycle.work_items if value.item_id == ItemId("accepted-proposal"))
        proposal = reopened.proposals.proposals[0]
        self.assertEqual(
            ("Proposal A", "A related observation", "Record the follow-up."),
            (item.user_label, item.trigger, item.effect),
        )
        self.assertEqual(StoredWorkItemState.READY, item.state)
        self.assertEqual(
            (ItemId("work-c"), ItemId("intake-work")),
            tuple(value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == item.item_id),
        )
        self.assertEqual(ProposalDispositionKind.ACCEPTED, proposal.disposition)
        self.assertEqual(ItemId("accepted-proposal"), proposal.disposition_target_item_id)
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.transition_receipts))

    def test_two_writers_reject_the_stale_typed_mutation_without_partial_state(self) -> None:
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
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._coordination_decision(before),
        )
        with first.write() as transaction:
            transaction.commit(first_mutation)

        stale_receipt, stale_after = self._receipt_state(before, "inspect:stale-writer")
        stale_after = replace(
            stale_after,
            authority=replace(
                before.authority,
                coordination=replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=2)),
            ),
        )
        stale_mutation = CoordinationAuthorityMutation(
            before,
            stale_after,
            self._mutation_receipt(stale_receipt, stale_after),
            self._coordination_decision(before, extension=timedelta(minutes=2)),
        )
        with self.assertRaises(StorageError), second.write() as transaction:
            transaction.commit(stale_mutation)
        self.assertEqual(after, second.snapshot())

    def test_proposal_creation_carrier_round_trips(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:proposal-create")
        proposal = StoredProposal(
            ProposalId("proposal-b"),
            receipt.decided_at,
            receipt.decided_at,
            TaskId("source-b"),
            "Proposal B",
            "A new observation",
            "The queue needs the observation.",
            ProposalRelationKind.INDEPENDENT,
            None,
            "Preserve the proposal.",
            "A coordinator can assess it.",
            "Scheduling remains unchanged.",
            None,
            None,
            None,
            13,
            None,
        )
        after = replace(
            after,
            proposals=replace(before.proposals, proposals=(*before.proposals.proposals, proposal)),
        )
        mutation = ProposalCreationMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._proposal_decision(receipt.decided_at, proposal),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(proposal, store.snapshot().proposals.proposals[-1])

    def test_authority_carriers_change_only_their_owned_rows(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:coordination-authority")
        coordination = before.authority.coordination
        assert coordination is not None
        after = replace(
            after,
            authority=replace(
                before.authority,
                coordination=replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
            ),
        )
        mutation = CoordinationAuthorityMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._coordination_decision(before),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(after, store.snapshot())

        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:attempt-authority")
        lease = before.authority.attempt_leases[0]
        after = replace(
            after,
            authority=replace(
                before.authority,
                attempt_leases=(replace(lease, expires_at=lease.expires_at + timedelta(minutes=1)),),
            ),
        )
        mutation = AttemptAuthorityMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._attempt_renewal_decision(before),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(after, store.snapshot())

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
                decision = decide(snapshot, bind_transition(action, payload), SQLITE_NOW + timedelta(seconds=1))
                with store.write() as transaction:
                    transaction.commit(project_transition_mutation(transaction.snapshot(), decision))
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
            if value.kind == ActionKind.CLOSE and value.subject == ItemId("intake-work")
        )
        close = decide(
            snapshot,
            bind_transition(
                close_action,
                CloseInput(CloseOutcome.DROPPED, "The intake is no longer needed."),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), close))
        self.assertEqual(StoredWorkItemState.DROPPED, store.snapshot().lifecycle.work_items[0].state)

        for outcome in ("active", "not-a-state"):
            store = self._store()
            before = store.snapshot()
            snapshot = project_decision_snapshot(before)
            actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
            close_action = next(
                value
                for value in available_actions(snapshot, actor)
                if value.kind == ActionKind.CLOSE and value.subject == ItemId("intake-work")
            )
            close = decide(
                snapshot,
                bind_transition(
                    close_action,
                    CloseInput(CloseOutcome.DROPPED, "The intake is no longer needed."),
                ),
                SQLITE_NOW + timedelta(seconds=1),
            )
            invalid = replace(close, receipt=replace(close.receipt, outcome=outcome))
            with self.subTest(outcome=outcome), self.assertRaises(StorageError), store.write() as transaction:
                transaction.commit(replace(project_transition_mutation(before, close), decision=invalid))
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
            bind_transition(
                activation_action,
                ActivateInput(AttemptId("work-c-1"), "branch", "base", "owner", ArtifactRefId(1)),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert activation.attempt_change is not None
        incomplete = replace(activation, attempt_change=replace(activation.attempt_change, branch=None))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(replace(project_transition_mutation(before, activation), decision=incomplete))
        self.assertEqual(before, store.snapshot())

        pause_action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.PAUSE)
        pause = decide(
            snapshot,
            bind_transition(pause_action, ReasonInput("Pause for an invalid-attempt-shape probe.")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert pause.attempt_change is not None
        missing_attempt_state = replace(pause, attempt_change=replace(pause.attempt_change, after=None))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(replace(project_transition_mutation(before, pause), decision=missing_attempt_state))
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
            bind_transition(review_action, SubmitReviewInput(CandidateId("candidate"))),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert review.attempt_change is not None
        missing_candidate = replace(
            review,
            attempt_change=replace(review.attempt_change, protected_candidate_after=None),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(replace(project_transition_mutation(before, review), decision=missing_candidate))
        self.assertEqual(before, store.snapshot())

    def test_application_port_contract_is_importable_at_the_composition_boundary(self) -> None:
        self.assertTrue(hasattr(WorkTransaction, "commit"))
        self.assertTrue(hasattr(WorkStore, "write"))

    def test_incomplete_proposal_creation_facts_are_rejected_atomically(self) -> None:
        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.ACCEPT_PROPOSAL)
        accepted = decide(
            snapshot,
            bind_transition(
                action,
                AcceptProposalInput(ItemId("incomplete-item"), AcceptedProposalState.READY, "activate"),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        assert accepted.proposal_change is not None
        incomplete = replace(
            accepted,
            proposal_change=replace(accepted.proposal_change, accepted_item=None),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(replace(project_transition_mutation(before, accepted), decision=incomplete))
        self.assertEqual(before, store.snapshot())

    def test_defer_decision_persists_reopen_focus(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == ActionKind.DEFER and value.subject == ItemId("intake-work")
        )
        decision = decide(
            snapshot,
            bind_transition(action, DeferInput(Timing.SAFE_TO_DEFER, "Reopen when the prerequisite is accepted.")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))
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
                before = store.snapshot()
                snapshot = project_decision_snapshot(before)
                actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
                action = next(value for value in available_actions(snapshot, actor) if value.kind == kind)
                decision = decide(
                    snapshot,
                    bind_transition(action, payload),
                    SQLITE_NOW + timedelta(seconds=1),
                )
                mutation = project_transition_mutation(before, decision)
                with store.write() as transaction:
                    transaction.commit(mutation)
                committed = store.snapshot()
                refreshed = replace(
                    decision,
                    action=replace(decision.action, expected_revision="13", subject_revision="13"),
                )
                with self.assertRaises(StorageError), store.write() as transaction:
                    transaction.commit(replace(mutation, decision=refreshed))
                self.assertEqual(committed, store.snapshot())

    def test_stored_receipt_full_identity_is_bound_to_the_mutation(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:receipt-identity")
        coordination = before.authority.coordination
        assert coordination is not None
        after = replace(
            after,
            authority=replace(
                before.authority,
                coordination=replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
            ),
        )
        accepted = after.transition_receipts[-1]
        mutation_receipt = self._mutation_receipt(receipt, after)
        changed_identities = (
            replace(mutation_receipt, history_id=HistoryId(9)),
            replace(mutation_receipt, project_revision=99),
            replace(mutation_receipt, transition=replace(receipt, action_id=ActionId("different-action"))),
            replace(mutation_receipt, action_kind=TransitionHistoryActionKind.ACTIVATE),
            replace(mutation_receipt, subject_id=HistorySubjectId("work-b")),
            replace(mutation_receipt, artifact_ref_id=ArtifactRefId(1)),
            replace(mutation_receipt, authorization=TransitionHistoryAuthorizationKind.ATTEMPT),
            replace(mutation_receipt, actor_task_id=TaskId("different-task")),
            replace(mutation_receipt, actor_host_id=HostId("different-host")),
            replace(mutation_receipt, input_schema="unrelated/v9"),
            replace(mutation_receipt, input_payload=CanonicalJson(b'{"different":true}')),
            replace(
                mutation_receipt,
                transition=replace(receipt, decided_at=receipt.decided_at + timedelta(seconds=1)),
            ),
        )
        for changed_identity in changed_identities:
            with self.subTest(identity=changed_identity), self.assertRaises(StorageError), store.write() as transaction:
                transaction.commit(
                    CoordinationAuthorityMutation(
                        before,
                        after,
                        changed_identity,
                        self._coordination_decision(before),
                    )
                )
            self.assertEqual(before, store.snapshot())

        changed_receipts = (
            replace(accepted, history_id=HistoryId(9)),
            replace(accepted, project_revision=99),
            replace(accepted, action_id=ActionId("different-action")),
            replace(accepted, action_kind=TransitionHistoryActionKind.ACTIVATE),
            replace(accepted, subject_id=HistorySubjectId("work-b")),
            replace(accepted, artifact_ref_id=ArtifactRefId(1)),
            replace(accepted, authorization=TransitionHistoryAuthorizationKind.ATTEMPT),
            replace(accepted, actor_task_id=TaskId("different-task")),
            replace(accepted, actor_host_id=HostId("different-host")),
            replace(accepted, input_schema="unrelated/v9"),
            replace(accepted, input_payload=CanonicalJson(b'{"different":true}')),
            replace(accepted, outcome_schema="unrelated-receipt/v9"),
            replace(accepted, outcome_payload=CanonicalJson(b'{"outcome":"different"}')),
            replace(accepted, committed_at=accepted.committed_at + timedelta(seconds=1)),
        )
        for changed_receipt in changed_receipts:
            with self.subTest(field=changed_receipt):
                changed_after = replace(after, transition_receipts=(*before.transition_receipts, changed_receipt))
                with self.assertRaises(StorageError), store.write() as transaction:
                    transaction.commit(
                        CoordinationAuthorityMutation(
                            before,
                            changed_after,
                            self._mutation_receipt(receipt, after),
                            self._coordination_decision(before),
                        )
                    )
                self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
