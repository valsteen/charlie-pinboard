import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.errors import MutationContractError, MutationContractErrorCode
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationReceipt,
    ProposalCreationMutation,
)
from pinboard.application.mutations import expected_stored_state, project_transition_mutation
from pinboard.application.ports import WorkStore, WorkTransaction
from pinboard.application.stored_state import (
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
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
from pinboard.domain.history import HistoryOutcome
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    ProposalId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_models import (
    ProposalCreationDecision,
    ProposalIntake,
    VisibleProposalItem,
)
from tests.domain_support import expect_success
from tests.domain_support import replace as replace_dataclass
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


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
) -> decision_models.Decision:
    return expect_success(decision_outcome(snapshot, command, now))


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
                work_models.IndependentProposalRelation(),
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
                proposal.relation,
                proposal.urgency_evidence,
                (),
                (),
            )
        return ProposalCreationDecision(
            intake,
            VisibleProposalItem(ItemId(intake.proposal_id), 5, (), SQLITE_DIGEST),
            None,
            intake.evidence,
            intake.freshness_assumptions,
        )

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

    def _receipt_state(
        self, before: StoredWorkState, action: str
    ) -> tuple[decision_models.TransitionReceipt, StoredWorkState]:
        decided_at = before.lifecycle.project.updated_at + timedelta(seconds=1)
        receipt = decision_models.TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
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

    def _stored_receipt(self, after: StoredWorkState) -> StoredTransitionReceipt:
        return after.transition_receipts[-1]

    def _mutation_receipt(self, receipt: decision_models.TransitionReceipt, after: StoredWorkState) -> MutationReceipt:
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
            outcome_payload=work_models.CanonicalJson(outcome.payload),
        )
        return replace(after, transition_receipts=(*after.transition_receipts[:-1], stored_receipt))

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
            selector="artifacts/briefs/work-a/2.json",
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
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        reopened = store.snapshot()
        item = next(value for value in reopened.lifecycle.work_items if value.item_id == ItemId("zz-proposal-a"))
        proposal = reopened.proposals.proposals[0]
        self.assertEqual(
            ("Proposal A", "A related observation", "Record the follow-up."),
            (item.user_label, item.trigger, item.effect),
        )
        self.assertEqual(StoredWorkItemState.READY, item.state)
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
            work_models.IndependentProposalRelation(),
            "Preserve the proposal.",
            "A coordinator can assess it.",
            "Scheduling remains unchanged.",
            None,
            13,
        )
        draft = ProposalCreationMutation(
            before,
            before,
            self._mutation_receipt(receipt, after),
            self._proposal_decision(receipt.decided_at, proposal),
        )
        mutation = replace(draft, after=expected_stored_state(draft))
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(
            proposal,
            next(value for value in store.snapshot().proposals.proposals if value.proposal_id == proposal.proposal_id),
        )

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

    def test_attempt_transition_reports_the_missing_or_stale_before_state_invariant(self) -> None:
        before = self._store().snapshot()
        snapshot = project_decision_snapshot(before)
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.PAUSE
        )
        decision = decide(
            snapshot,
            bind_transition(action, work_models.ReasonInput("Pause at a stable checkpoint.")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        without_attempt = replace(before, lifecycle=replace(before.lifecycle, attempts=()))

        with self.assertRaises(MutationContractError) as raised:
            project_transition_mutation(without_attempt, decision)

        self.assertEqual(MutationContractErrorCode.ATTEMPT_MISSING_OR_BEFORE_STATE_STALE, raised.exception.code)
        self.assertIn("stored attempt is missing or its before state is stale", str(raised.exception))

    def test_lifecycle_writer_rejects_invalid_terminal_receipt_atomically(self) -> None:
        store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
        )
        close_action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.CLOSE and value.capability.subject == ItemId("intake-work")
        )
        close = decide(
            snapshot,
            bind_transition(
                close_action,
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "The intake is no longer needed."),
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
            actor = decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
            )
            close_action = next(
                value
                for value in available_actions(snapshot, actor)
                if value.kind == decision_models.ActionKind.CLOSE and value.capability.subject == ItemId("intake-work")
            )
            close = decide(
                snapshot,
                bind_transition(
                    close_action,
                    work_models.CloseInput(work_models.CloseOutcome.DROPPED, "The intake is no longer needed."),
                ),
                SQLITE_NOW + timedelta(seconds=1),
            )
            invalid = replace(close, receipt=replace(close.receipt, outcome=outcome))
            with self.subTest(outcome=outcome), self.assertRaises(StorageError), store.write() as transaction:
                transaction.commit(replace(project_transition_mutation(before, close), decision=invalid))
            self.assertEqual(before, store.snapshot())

    def test_application_port_contract_is_importable_at_the_composition_boundary(self) -> None:
        self.assertTrue(hasattr(WorkTransaction, "commit"))
        self.assertTrue(hasattr(WorkStore, "write"))

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
        self.assertEqual(StoredWorkItemState.DEFERRED, reopened.lifecycle.work_items[0].state)
        self.assertEqual("reopen", reopened.focus.next_action)

    def test_row_level_staleness_rejects_after_revision_tokens_are_refreshed(self) -> None:
        for kind, payload in (
            (decision_models.ActionKind.PAUSE, work_models.ReasonInput("Pause once.")),
            (decision_models.ActionKind.MERGE_PROPOSAL, work_models.MergeProposalInput(ItemId("work-c"))),
        ):
            with self.subTest(kind=kind):
                store = self._store()
                before = store.snapshot()
                snapshot = project_decision_snapshot(before)
                actor = decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, snapshot.generation
                )
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
                    action=replace_dataclass(
                        decision.action,
                        capability=replace_dataclass(
                            decision.action.capability,
                            expected_revision="13",
                            subject_revision="13",
                        ),
                    ),
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
            replace(mutation_receipt, input_payload=work_models.CanonicalJson(b'{"different":true}')),
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
            replace(accepted, input_payload=work_models.CanonicalJson(b'{"different":true}')),
            replace(accepted, outcome_schema="unrelated-receipt/v9"),
            replace(accepted, outcome_payload=work_models.CanonicalJson(b'{"outcome":"different"}')),
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
