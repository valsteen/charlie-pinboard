import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import assert_type, cast

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import StorageError, initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.mutations import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    DependencyEditMutation,
    MutationContractError,
    MutationReceipt,
    PlanningImpactMutation,
    PlanningMutationReceipt,
    PlanningResolutionMutation,
    ProposalCreationMutation,
    ReservationTaskUseMutation,
    ResourceIntentMutation,
    ResourceMutation,
    ResourceRequirementEditMutation,
    expected_stored_state,
    project_transition_mutation,
)
from charlie_pinboard.application.ports import WorkStore, WorkTransaction
from charlie_pinboard.application.stored_state import (
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    CanonicalJson,
    HistoryRecords,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    OriginKind,
    PlanningObligationState,
    PlanningRecords,
    ProposalDisposition,
    ProposalRelation,
    StoredAttemptLease,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import (
    AttemptAuthorityDecision,
    AttemptLeaseAuthority,
    AttemptLeaseStatus,
    CoordinationAuthorityDecision,
    TaskUseAuthorityDecision,
)
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
    TransitionReceipt,
)
from charlie_pinboard.domain.decisions import (
    available_actions as available_actions_outcome,
)
from charlie_pinboard.domain.decisions import (
    bind_transition as bind_transition_outcome,
)
from charlie_pinboard.domain.decisions import (
    decide as decision_outcome,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import HistoryOutcome, planning_impact_outcome, planning_resolution_outcome
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
    ActivateInput,
    AttemptState,
    CloseInput,
    CloseOutcome,
    DeferInput,
    LedgerSnapshot,
    MergeProposalInput,
    MutationIntentState,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    ProposalRelationKind,
    ReasonInput,
    ResourceIntentCapability,
    ResourceMutationCapability,
    ResourceRequirement,
    ResumeInput,
    ScopeAnchor,
    ScopeDependency,
    SubmitReviewInput,
    Timing,
    TransitionInput,
    UseLeaseState,
)
from charlie_pinboard.domain.planning_decisions import (
    LivePlanningAttemptAuthority,
    NoAttemptPlanningAuthority,
    PlanningImpactDecision,
    PlanningResolutionDecision,
    PlanningTargetAuthority,
    decide_planning_resolution,
)
from charlie_pinboard.domain.proposal_decisions import ProposalCreationDecision, ProposalIntake
from charlie_pinboard.domain.resource_decisions import (
    AdvanceResourceObservationInput,
    FencedIntentDisposition,
    IntentDecisionKind,
    MutationIntentChange,
    ObservedResource,
    RegisterMutationIntentInput,
    ResolveFencedIntentInput,
    ResolverEvidenceDecision,
    ResourceDecision,
    ResourceDecisionKind,
    ResourceIntentDecision,
    advance_resource_observation,
    reallocate_resource,
    register_mutation_intent,
    release_resource,
    resolve_fenced_resource_intent,
)
from charlie_pinboard.domain.scope_decisions import ItemScopeEditDecision
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


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

    def _scope_decision(self, before: StoredWorkState, *, requirements: bool) -> ItemScopeEditDecision:
        snapshot = project_decision_snapshot(before)
        current = next(value for value in snapshot.scopes if value.item == ItemId("work-c"))
        if requirements:
            dependencies = current.scope.dependencies
            resource_requirements = (ResourceRequirement(0, ResourceId("workspace")),)
            digest = "c" * 64
        else:
            dependencies = (
                ScopeDependency(0, ItemId("legacy-work")),
                ScopeDependency(1, ItemId("work-b")),
            )
            resource_requirements = current.scope.resource_requirements
            digest = "b" * 64
        return ItemScopeEditDecision(
            current.item,
            current,
            ScopeAnchor(
                current.item,
                current.revision + 1,
                digest,
                replace(
                    current.scope,
                    dependencies=dependencies,
                    resource_requirements=resource_requirements,
                ),
            ),
            dependencies,
            resource_requirements,
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
            (),
        )

    def _task_use_renewal_decision(
        self,
        before: StoredWorkState,
        changed_at: datetime,
    ) -> TaskUseAuthorityDecision:
        current = project_decision_snapshot(before).mutation_use_leases[-1]
        return TaskUseAuthorityDecision(
            current,
            replace(current, expires_at=current.expires_at + timedelta(minutes=1)),
            None,
            changed_at,
        )

    def _planning_target_authority(
        self,
        before: StoredWorkState,
        target: ItemId,
    ) -> PlanningTargetAuthority:
        snapshot = project_decision_snapshot(before)
        attempt = next((value for value in snapshot.attempts if value.item == target), None)
        if attempt is None:
            return NoAttemptPlanningAuthority(target)
        authority = next(value for value in snapshot.command_attempt_authorities if value.attempt == attempt.attempt)
        return LivePlanningAttemptAuthority(authority)

    def _receipt_state(self, before: StoredWorkState, action: str) -> tuple[TransitionReceipt, StoredWorkState]:
        decided_at = before.lifecycle.project.updated_at + timedelta(seconds=1)
        receipt = TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
        stored_receipt = StoredTransitionReceipt(
            HistoryId(1 + max((int(value.history_id) for value in before.history.receipts), default=0)),
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
            history=HistoryRecords((*before.history.receipts, stored_receipt)),
        )
        return receipt, after

    def _stored_receipt(self, after: StoredWorkState) -> StoredTransitionReceipt:
        return after.history.receipts[-1]

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

    def _planning_mutation_receipt(
        self,
        receipt: TransitionReceipt,
        after: StoredWorkState,
    ) -> PlanningMutationReceipt:
        stored = self._stored_receipt(after)
        return PlanningMutationReceipt(
            receipt.action_id,
            receipt.decided_at,
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
        return replace(after, history=HistoryRecords((*after.history.receipts[:-1], stored_receipt)))

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
        self.assertEqual(2, len(reopened.history.receipts))
        self.assertEqual("transition-receipt/v1", reopened.history.receipts[-1].outcome_schema)

    def test_resume_replaces_the_attempt_brief_and_reloads_it_from_sqlite(self) -> None:
        state = complete_sqlite_state()
        current = state.artifacts.references[0]
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
            artifacts=replace(state.artifacts, references=(*state.artifacts.references, replacement)),
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
                    depends_on=(ItemId("work-c"), ItemId("legacy-work")),
                    resource_requirements=(ResourceId("workspace"),),
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
        mutation = DependencyEditMutation(
            before,
            invalid,
            self._mutation_receipt(receipt, after),
            self._scope_decision(before, requirements=False),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(before, store.snapshot())

        receipt, after = self._receipt_state(before, "inspect:constraint-failure")
        items = list(after.lifecycle.work_items)
        items[0] = replace(items[0], scope_digest="invalid")
        constrained = replace(after, lifecycle=replace(after.lifecycle, work_items=tuple(items)))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(
                DependencyEditMutation(
                    before,
                    constrained,
                    self._mutation_receipt(receipt, after),
                    self._scope_decision(before, requirements=False),
                )
            )
        self.assertEqual(before, store.snapshot())

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
        mutation = DependencyEditMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._scope_decision(before, requirements=False),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
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
                requirements=(
                    *before.resources.requirements,
                    ItemResourceRequirement(ItemId("work-c"), ResourceId("workspace"), 0),
                ),
            ),
        )
        mutation = ResourceRequirementEditMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._scope_decision(before, requirements=True),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        reopened = store.snapshot()
        self.assertEqual(
            (ResourceId("workspace"),),
            tuple(value.resource_id for value in reopened.resources.requirements if value.item_id == ItemId("work-c")),
        )

    def test_authority_and_task_use_carriers_change_only_their_owned_rows(self) -> None:
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

        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:reservation-task-use")
        leases = tuple(
            replace(value, expires_at=value.expires_at + timedelta(minutes=1))
            if value.lease_id == LeaseId("use-successor")
            else value
            for value in before.resources.use_leases
        )
        after = replace(after, resources=replace(before.resources, use_leases=leases))
        mutation = ReservationTaskUseMutation(
            before,
            after,
            self._mutation_receipt(receipt, after),
            self._task_use_renewal_decision(before, receipt.decided_at),
        )
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(after, store.snapshot())

    def test_planning_impact_and_resolution_decisions_determine_exact_rows(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:planning-impact")
        source = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-a"))
        target = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-c"))
        impact = PlanningImpact(
            PlanningImpactId("impact-b"),
            source.item_id,
            AttemptId("work-a-1"),
            source.scope_revision,
            source.scope_digest,
            "The active work changes the queued item.",
            "The accepted dependency now has a different outcome.",
            (PlanningObligation(target.item_id, 0, target.scope_revision, target.scope_digest),),
        )
        stored_impact = StoredPlanningImpact(
            impact.impact_id,
            impact.source_item,
            impact.source_attempt,
            impact.source_scope_revision,
            impact.source_scope_digest,
            target.item_id,
            impact.summary,
            impact.evidence,
            13,
            receipt.decided_at,
        )
        obligation = StoredPlanningObligation(
            impact.impact_id,
            target.item_id,
            0,
            target.scope_revision,
            target.scope_digest,
            PlanningObligationState.UNRESOLVED,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            receipt.decided_at,
            None,
        )
        after = replace(
            after,
            planning=PlanningRecords(
                (*before.planning.impacts, stored_impact),
                (*before.planning.obligations, obligation),
                before.planning.replacements,
            ),
        )
        after = self._history_outcome(after, cast(HistoryOutcome, planning_impact_outcome(impact)))
        mutation = PlanningImpactMutation(
            PlanningImpactDecision(impact, project_decision_snapshot(before).command_attempt_authorities[0]),
            before,
            after,
            self._planning_mutation_receipt(receipt, after),
        )
        assert_type(mutation.receipt, PlanningMutationReceipt)
        with store.write() as transaction:
            committed = transaction.commit(mutation)
        self.assertIsInstance(committed, PlanningMutationReceipt)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(after, store.snapshot())
        self.assertEqual(
            cast(HistoryOutcome, planning_impact_outcome(impact)).payload,
            store.snapshot().history.receipts[-1].outcome_payload,
        )

        before = store.snapshot()
        receipt, common_after = self._receipt_state(before, "inspect:planning-resolution-block")
        recorded_impact = next(
            value for value in project_decision_snapshot(before).planning_impacts if value.impact_id == impact.impact_id
        )
        resolution = cast(
            PlanningResolutionDecision,
            decide_planning_resolution(
                project_decision_snapshot(before),
                recorded_impact,
                target.item_id,
                PlanningDisposition.SUPERSEDED,
                reason="The changed outcome is now owned by the replacement item.",
                replacements=(ItemId("legacy-work"),),
                outcome_evidence="The replacement item retains the accepted outcome.",
            ),
        )
        draft = PlanningResolutionMutation(
            resolution,
            target.item_id,
            before,
            common_after,
            self._planning_mutation_receipt(receipt, common_after),
            self._planning_target_authority(before, target.item_id),
        )
        resolved_after = expected_stored_state(draft)
        mutation = replace(draft, after=resolved_after)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(replace(mutation, target=ItemId("legacy-work")))
        self.assertEqual(before, store.snapshot())
        with store.write() as transaction:
            transaction.commit(mutation)
        reopened_item = next(
            value for value in store.snapshot().lifecycle.work_items if value.item_id == target.item_id
        )
        self.assertEqual(StoredWorkItemState.SUPERSEDED, reopened_item.state)
        self.assertEqual(
            (ItemId("legacy-work"),),
            tuple(
                value.replacement_item_id
                for value in store.snapshot().planning.replacements
                if value.impact_id == impact.impact_id
            ),
        )
        self.assertEqual(
            cast(HistoryOutcome, planning_resolution_outcome(resolution.impact, target.item_id)).payload,
            store.snapshot().history.receipts[-1].outcome_payload,
        )

    def test_resource_decisions_determine_release_and_reallocation_rows(self) -> None:
        baseline = complete_sqlite_state()
        baseline = replace(
            baseline,
            resources=replace(
                baseline.resources,
                use_leases=tuple(
                    replace(value, state=UseLeaseState.RELEASED)
                    if value.lease_id == LeaseId("use-successor")
                    else value
                    for value in baseline.resources.use_leases
                ),
            ),
        )
        store = self._store_with_state(baseline)
        before = store.snapshot()
        receipt, common_after = self._receipt_state(before, "inspect:resource-release")
        decision = cast(
            ResourceDecision,
            release_resource(project_decision_snapshot(before), ReservationId("reservation-a")),
        )
        locator = before.resources.locators[0]
        draft = ResourceMutation(
            decision,
            before,
            common_after,
            locator.locator_schema,
            locator.locator,
            locator.observation_generation,
            locator.observation_digest,
            locator.observed_at,
            self._mutation_receipt(receipt, common_after),
        )
        after = expected_stored_state(draft)
        mutation = replace(draft, after=after)
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(after, store.snapshot())

        store = self._store_with_state(baseline)
        before = store.snapshot()
        receipt, common_after = self._receipt_state(before, "inspect:resource-reallocate")
        decision = cast(
            ResourceDecision,
            reallocate_resource(
                project_decision_snapshot(before),
                ReservationId("reservation-a"),
                replacement_id=ReservationId("reservation-b"),
                instance_id=before.resources.reservations[0].instance_id,
                generation=2,
            ),
        )
        locator = before.resources.locators[0]
        draft = ResourceMutation(
            decision,
            before,
            common_after,
            locator.locator_schema,
            locator.locator,
            locator.observation_generation,
            locator.observation_digest,
            locator.observed_at,
            self._mutation_receipt(receipt, common_after),
        )
        reallocated_after = expected_stored_state(draft)
        mutation = replace(draft, after=reallocated_after)
        with store.write() as transaction:
            transaction.commit(mutation)
        reopened = store.snapshot()
        self.assertEqual(
            ("released", "active"),
            tuple(value.state.value for value in reopened.resources.reservations),
        )
        counter = next(
            value
            for value in reopened.resources.reservation_counters
            if value.instance_id == before.resources.reservations[0].instance_id
        )
        self.assertEqual(2, counter.generation_high_water)

    def test_already_resolved_planning_target_rejects_and_resource_intent_evidence_persists_exactly(self) -> None:
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
        after = self._history_outcome(
            after,
            cast(HistoryOutcome, planning_resolution_outcome(decision.impact, ItemId("work-b"))),
        )
        mutation = PlanningResolutionMutation(
            decision,
            ItemId("work-b"),
            before,
            after,
            self._planning_mutation_receipt(receipt, after),
            self._planning_target_authority(before, ItemId("work-b")),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(before, store.snapshot())

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
        mutation = ResourceIntentMutation(decision, before, after, self._mutation_receipt(receipt, after))
        with store.write() as transaction:
            transaction.commit(mutation)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(mutation)
        reopened = store.snapshot().resources.mutation_intents[0]
        self.assertEqual(("abandoned", TaskId("worker")), (reopened.state.value, reopened.disposition_task_id))
        self.assertEqual(SQLITE_DIGEST, reopened.start_observation_digest)

    def test_unchanged_fenced_resolution_requires_a_reason_and_persists_the_abandoned_row_shape(self) -> None:
        state = complete_sqlite_state()
        intent_record = state.resources.mutation_intents[0]
        retained_uses = tuple(
            value
            for value in state.resources.use_leases
            if value.generation <= intent_record.resource_use_generation + 1
        )
        recovery_generation = intent_record.attempt_lease_generation + 1
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(AttemptLeaseCounter(intent_record.attempt_id, recovery_generation),),
                attempt_generations=(
                    *state.authority.attempt_generations,
                    AttemptLeaseGeneration(
                        intent_record.attempt_id,
                        recovery_generation,
                        LeaseId("attempt-lease-fenced-recovery"),
                        TaskId("fenced-recovery-worker"),
                        HostId("host-a"),
                    ),
                ),
                attempt_leases=(
                    StoredAttemptLease(
                        intent_record.attempt_id,
                        recovery_generation,
                        SQLITE_NOW + timedelta(seconds=1),
                        SQLITE_NOW + timedelta(minutes=5),
                        AttemptLeaseState.ACTIVE,
                    ),
                ),
            ),
            resources=replace(state.resources, use_leases=retained_uses),
        )
        store = self._store_with_state(state)
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        intent = snapshot.mutation_intents[0]
        reservation = snapshot.mutation_reservation(intent.reservation_id)
        instance = snapshot.resource_instance(intent.instance_id)
        observation = snapshot.resource_observation(intent.instance_id)
        prior_use = snapshot.mutation_use_lease(intent.resource_use_lease_id, intent.resource_use_generation)
        coordination = snapshot.coordination_authority
        assert reservation is not None
        definition = snapshot.resource_definition(reservation.resource_id)
        assert instance is not None
        assert observation is not None
        assert definition is not None
        assert prior_use is not None
        assert coordination is not None
        capability = ResourceIntentCapability(
            ResourceMutationCapability(
                reservation.resource_id,
                reservation.reservation_id,
                reservation.acquisition_generation,
                instance.instance_id,
                prior_use.instance_subject_revision,
                prior_use.observation_generation,
                prior_use.observation_digest,
                prior_use.lease_id,
                prior_use.generation,
                prior_use.task_id,
                prior_use.host_id,
                prior_use.host_epoch,
                prior_use.attempt_lease_id,
                prior_use.attempt_lease_generation,
            ),
            intent.intent_id,
            intent.policy_digest,
            intent.state,
        )
        supplied_observation = ObservedResource(
            instance.instance_id,
            instance.host_id,
            definition.kind,
            instance.discovery_fingerprint,
            observation.locator_schema,
            observation.locator,
            observation.digest,
            SQLITE_NOW + timedelta(seconds=2),
        )
        empty_reason = ResolveFencedIntentInput(
            capability,
            coordination,
            supplied_observation,
            snapshot.resource_reservation_counters[-1].generation_high_water,
            FencedIntentDisposition.UNCHANGED,
            "",
            None,
            None,
            None,
            None,
            SQLITE_NOW + timedelta(seconds=2),
        )
        rejected = resolve_fenced_resource_intent(snapshot, empty_reason)
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
        accepted = resolve_fenced_resource_intent(
            snapshot, replace(empty_reason, reason="The fenced dispatch left the resource unchanged.")
        )
        self.assertNotIsInstance(accepted, DecisionFailure)
        decision = accepted
        receipt, common_after = self._receipt_state(before, "inspect:resolve-fenced")
        draft = ResourceIntentMutation(
            decision,
            before,
            common_after,
            self._mutation_receipt(receipt, common_after),
        )
        mutation = replace(draft, after=expected_stored_state(draft))
        with store.write() as transaction:
            transaction.commit(mutation)

        reopened = store.snapshot()
        persisted = reopened.resources.mutation_intents[0]
        self.assertEqual(MutationIntentState.ABANDONED, persisted.state)
        self.assertEqual(
            (None, None, None),
            (
                persisted.result_observation_generation,
                persisted.result_observation_digest,
                persisted.evidence_digest,
            ),
        )
        self.assertEqual("The fenced dispatch left the resource unchanged.", persisted.disposition_reason)

    def test_resource_intent_registration_and_observation_changes_apply_every_decision_member(self) -> None:
        baseline = complete_sqlite_state()
        baseline = replace(baseline, resources=replace(baseline.resources, mutation_intents=()))
        store = self._store_with_state(baseline)
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(
            Role.WORKER,
            AuthorizationKind.ATTEMPT,
            3,
            LeaseId("attempt-lease-a"),
            (AttemptId("work-a-1"),),
            False,
        )
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.SUBMIT_REVIEW)
        capability = action.resource_capabilities[0]
        receipt, common_after = self._receipt_state(before, "inspect:intent-register")
        registration = cast(
            ResourceIntentDecision,
            register_mutation_intent(
                snapshot,
                RegisterMutationIntentInput(
                    capability,
                    MutationIntentId("intent-new"),
                    "mutation-policy/v1",
                    CanonicalJson(b'{"paths":["src"]}'),
                    "c" * 64,
                    receipt.decided_at,
                ),
            ),
        )
        draft = ResourceIntentMutation(
            registration,
            before,
            common_after,
            self._mutation_receipt(receipt, common_after),
        )
        registered_after = expected_stored_state(draft)
        with store.write() as transaction:
            transaction.commit(replace(draft, after=registered_after))
        self.assertEqual("planned", store.snapshot().resources.mutation_intents[-1].state.value)

        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.SUBMIT_REVIEW)
        capability = action.resource_capabilities[0]
        intent = snapshot.mutation_intents[-1]
        intent_capability = ResourceIntentCapability(
            capability,
            intent.intent_id,
            intent.policy_digest,
            intent.state,
        )
        instance = snapshot.resource_instances[0]
        locator = snapshot.resource_observations[0]
        definition = snapshot.resource_definitions[0]
        receipt, common_after = self._receipt_state(before, "inspect:intent-advance")
        observation = ObservedResource(
            instance.instance_id,
            instance.host_id,
            definition.kind,
            instance.discovery_fingerprint,
            locator.locator_schema,
            locator.locator,
            "b" * 64,
            receipt.decided_at,
        )
        advanced = cast(
            ResourceIntentDecision,
            advance_resource_observation(
                snapshot,
                AdvanceResourceObservationInput(
                    intent_capability,
                    observation,
                    "change-evidence/v1",
                    CanonicalJson(b'{"accepted":true}'),
                    "d" * 64,
                    ResolverEvidenceDecision.ACCEPTED,
                    receipt.decided_at,
                ),
            ),
        )
        draft = ResourceIntentMutation(
            advanced,
            before,
            common_after,
            self._mutation_receipt(receipt, common_after),
        )
        advanced_after = expected_stored_state(draft)
        with store.write() as transaction:
            transaction.commit(replace(draft, after=advanced_after))
        reopened = store.snapshot()
        self.assertEqual("accepted", reopened.resources.mutation_intents[-1].state.value)
        reopened_locator = next(
            value for value in reopened.resources.locators if value.instance_id == instance.instance_id
        )
        reopened_instance = next(
            value for value in reopened.resources.instances if value.instance_id == instance.instance_id
        )
        self.assertEqual("b" * 64, reopened_locator.observation_digest)
        self.assertEqual(instance.subject_revision + 1, reopened_instance.subject_revision)

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
            if value.kind == ActionKind.CLOSE and value.subject == ItemId("legacy-work")
        )
        close = decide(
            snapshot,
            bind_transition(
                close_action,
                CloseInput(CloseOutcome.DROPPED, "The legacy intake is no longer needed."),
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
                if value.kind == ActionKind.CLOSE and value.subject == ItemId("legacy-work")
            )
            close = decide(
                snapshot,
                bind_transition(
                    close_action,
                    CloseInput(CloseOutcome.DROPPED, "The legacy intake is no longer needed."),
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
            replace(snapshot, mutation_intents=()),
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
            replace(snapshot, mutation_intents=()),
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

    def test_mutation_envelope_and_creation_fact_rejections_are_atomic(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, after = self._receipt_state(before, "inspect:missing-history")
        missing_history = replace(after, history=before.history)
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(
                DependencyEditMutation(
                    before,
                    missing_history,
                    self._mutation_receipt(receipt, after),
                    self._scope_decision(before, requirements=False),
                )
            )
        self.assertEqual(before, store.snapshot())

        wrong_receipt = replace(after.history.receipts[-1], history_id=HistoryId(9))
        mismatched_history = replace(after, history=HistoryRecords((*before.history.receipts, wrong_receipt)))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(
                DependencyEditMutation(
                    before,
                    mismatched_history,
                    self._mutation_receipt(receipt, after),
                    self._scope_decision(before, requirements=False),
                )
            )
        self.assertEqual(before, store.snapshot())

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
            if value.kind == ActionKind.DEFER and value.subject == ItemId("legacy-work")
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
                    replace(snapshot, mutation_intents=()),
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

    def test_pure_decision_and_carrier_deltas_are_authoritative(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, unchanged_after = self._receipt_state(before, "inspect:mismatched-intent")
        intent = project_decision_snapshot(before).mutation_intents[0]
        abandoned = replace(
            intent,
            state=intent.state.ABANDONED,
            resolved_at=receipt.decided_at,
            disposition_task_id=TaskId("worker"),
            disposition_reason="The resource remained unchanged.",
        )
        decision = ResourceIntentDecision(IntentDecisionKind.ABANDON, MutationIntentChange(intent, abandoned))
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(
                ResourceIntentMutation(
                    decision,
                    before,
                    unchanged_after,
                    self._mutation_receipt(receipt, unchanged_after),
                )
            )
        self.assertEqual(before, store.snapshot())

    def test_closed_mutations_reject_empty_or_malformed_owned_deltas(self) -> None:
        store = self._store()
        before = store.snapshot()
        receipt, common_after = self._receipt_state(before, "inspect:invalid-owned-delta")
        mutation_receipt = self._mutation_receipt(receipt, common_after)
        empty_carriers = (
            ProposalCreationMutation(
                before,
                common_after,
                mutation_receipt,
                self._proposal_decision(receipt.decided_at),
            ),
            DependencyEditMutation(
                before,
                common_after,
                mutation_receipt,
                self._scope_decision(before, requirements=False),
            ),
            ResourceRequirementEditMutation(
                before,
                common_after,
                mutation_receipt,
                self._scope_decision(before, requirements=True),
            ),
            CoordinationAuthorityMutation(
                before,
                common_after,
                mutation_receipt,
                self._coordination_decision(before),
            ),
            AttemptAuthorityMutation(
                before,
                common_after,
                mutation_receipt,
                self._attempt_renewal_decision(before),
            ),
            ReservationTaskUseMutation(
                before,
                common_after,
                mutation_receipt,
                self._task_use_renewal_decision(before, receipt.decided_at),
            ),
        )
        for mutation in empty_carriers:
            with (
                self.subTest(mutation=type(mutation).__name__),
                self.assertRaises(StorageError),
                store.write() as transaction,
            ):
                transaction.commit(mutation)
            self.assertEqual(before, store.snapshot())

        fewer_items = replace(
            common_after,
            lifecycle=replace(common_after.lifecycle, work_items=before.lifecycle.work_items[:-1]),
        )
        reordered_items = replace(
            common_after,
            lifecycle=replace(common_after.lifecycle, work_items=tuple(reversed(before.lifecycle.work_items))),
        )
        first = before.lifecycle.work_items[0]
        changed_state = replace(
            common_after,
            lifecycle=replace(
                common_after.lifecycle,
                work_items=(replace(first, state=StoredWorkItemState.BLOCKED), *before.lifecycle.work_items[1:]),
            ),
        )
        work_c = next(value for value in before.lifecycle.work_items if value.item_id == ItemId("work-c"))
        changed_work_c = replace(
            work_c,
            scope_revision=2,
            scope_digest="e" * 64,
            subject_revision=13,
            origin_updated_at=receipt.decided_at,
            updated_at=receipt.decided_at,
        )
        unrelated_scope = replace(
            common_after,
            lifecycle=replace(
                common_after.lifecycle,
                work_items=tuple(
                    changed_work_c if value.item_id == work_c.item_id else value
                    for value in before.lifecycle.work_items
                ),
                scope_revisions=(
                    *before.lifecycle.scope_revisions,
                    ItemScopeRevision(ItemId("work-b"), 2, "e" * 64, 13, receipt.decided_at),
                ),
            ),
        )
        malformed_states = (
            ("fewer-items", fewer_items),
            ("reordered-items", reordered_items),
            ("changed-state", changed_state),
            ("unrelated-scope", unrelated_scope),
        )
        for name, malformed in malformed_states:
            with self.subTest(malformed=name), self.assertRaises(StorageError), store.write() as transaction:
                transaction.commit(
                    DependencyEditMutation(
                        before,
                        malformed,
                        mutation_receipt,
                        self._scope_decision(before, requirements=False),
                    )
                )
            self.assertEqual(before, store.snapshot())

        snapshot = project_decision_snapshot(before)
        existing_impact = snapshot.planning_impacts[0]
        planning_mutation_receipt = self._planning_mutation_receipt(receipt, common_after)
        empty_impact = PlanningImpact(
            PlanningImpactId("impact-empty"),
            ItemId("work-a"),
            AttemptId("work-a-1"),
            1,
            SQLITE_DIGEST,
            "Impact",
            "Evidence",
            (),
        )
        pure_noops = (
            PlanningImpactMutation(
                PlanningImpactDecision(existing_impact, snapshot.command_attempt_authorities[0]),
                before,
                common_after,
                planning_mutation_receipt,
            ),
            PlanningImpactMutation(
                PlanningImpactDecision(empty_impact, snapshot.command_attempt_authorities[0]),
                before,
                common_after,
                planning_mutation_receipt,
            ),
            PlanningResolutionMutation(
                PlanningResolutionDecision(existing_impact, None, None),
                existing_impact.obligations[0].target,
                before,
                common_after,
                planning_mutation_receipt,
                self._planning_target_authority(before, existing_impact.obligations[0].target),
            ),
            ResourceMutation(
                ResourceDecision(ResourceDecisionKind.RELEASE, ()),
                before,
                common_after,
                before.resources.locators[0].locator_schema,
                before.resources.locators[0].locator,
                before.resources.locators[0].observation_generation,
                before.resources.locators[0].observation_digest,
                before.resources.locators[0].observed_at,
                mutation_receipt,
            ),
        )
        intent = snapshot.mutation_intents[0]
        intent_noop = ResourceIntentMutation(
            ResourceIntentDecision(IntentDecisionKind.ABANDON, MutationIntentChange(intent, intent)),
            before,
            common_after,
            mutation_receipt,
        )
        for mutation in (*pure_noops, intent_noop):
            with self.subTest(mutation=mutation), self.assertRaises(MutationContractError):
                expected_stored_state(mutation)

        coordination = before.authority.coordination
        assert coordination is not None
        unrelated_after = replace(
            common_after,
            authority=replace(
                before.authority,
                coordination=replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
            ),
        )
        with self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(
                DependencyEditMutation(
                    before,
                    unrelated_after,
                    mutation_receipt,
                    self._scope_decision(before, requirements=False),
                )
            )
        self.assertEqual(before, store.snapshot())

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
        accepted = after.history.receipts[-1]
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
                changed_after = replace(after, history=HistoryRecords((*before.history.receipts, changed_receipt)))
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
