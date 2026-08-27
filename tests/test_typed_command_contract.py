import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast, override

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
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
from charlie_pinboard.domain.decisions import (
    rediscover_action as rediscover_action_outcome,
)
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
)
from charlie_pinboard.domain.model import (
    ActivateInput,
    ArtifactRecord,
    AttemptRecord,
    AttemptState,
    BlockInput,
    EmptyInput,
    EvidenceInput,
    LedgerSnapshot,
    LegacyActivateInput,
    ReasonInput,
    ResumeInput,
    SubmitReviewInput,
    TransitionInput,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.interfaces.transition_input import (
    encoded_legacy_transition_input_schema,
    parse_legacy_transition_input,
    parse_transition_input,
)
from tests.domain_support import action
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


def _worker_actor() -> ActorAuthority:
    return ActorAuthority(
        Role.WORKER,
        AuthorizationKind.ATTEMPT,
        3,
        LeaseId("attempt-lease-a"),
        (AttemptId("work-a-1"),),
        False,
    )


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    return cast(tuple[Action, ...], available_actions_outcome(snapshot, actor))


def bind_transition(action_value: Action, value: TransitionInput) -> TransitionCommand:
    return cast(TransitionCommand, bind_transition_outcome(action_value, value))


def decide(snapshot: LedgerSnapshot, command: TransitionCommand, now: datetime) -> Decision:
    return cast(Decision, decision_outcome(snapshot, command, now))


def rediscover_action(snapshot: LedgerSnapshot, actor: ActorAuthority, supplied: Action) -> Action:
    return cast(Action, rediscover_action_outcome(snapshot, actor, supplied))


def _stored_action(snapshot: LedgerSnapshot) -> Action:
    return next(
        candidate
        for candidate in available_actions(snapshot, _worker_actor())
        if candidate.kind == ActionKind.SUBMIT_REVIEW
    )


class TypedTransitionContractTest(unittest.TestCase):
    def test_submit_review_candidate_is_required_typed_and_preserved(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        snapshot = project_decision_snapshot(state)
        submit = _stored_action(snapshot)

        rejected = bind_transition_outcome(submit, EmptyInput())
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        candidate = CandidateId("candidate-that-is-not-a-subject-revision")
        parsed = parse_transition_input("submit-review", b'{"candidate":"candidate-that-is-not-a-subject-revision"}')
        self.assertEqual(SubmitReviewInput(candidate), parsed)
        decision = decide(snapshot, bind_transition(submit, parsed), SQLITE_NOW)
        assert decision.attempt_change is not None
        self.assertEqual(candidate, decision.attempt_change.protected_candidate_after)
        self.assertEqual(SQLITE_NOW, decision.attempt_change.candidate_observed_at)

    def test_activation_requires_one_existing_brief_artifact_reference(self) -> None:
        ready = WorkItem(ItemId("ready-item"), WorkState.READY, None, (), None, "test", "activate", "")
        snapshot = LedgerSnapshot(
            "project-revision",
            1,
            (ready,),
            artifacts=(
                ArtifactRecord(ArtifactRefId(1), "brief"),
                ArtifactRecord(ArtifactRefId(2), "design"),
            ),
        )
        activation = action(ActionKind.ACTIVATE, "ready-item")
        rejected = bind_transition_outcome(activation, EmptyInput())
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        variants = (
            ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(99)),
            ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(2)),
        )
        for value in variants:
            with self.subTest(value=value):
                command = bind_transition(activation, value)
                rejected = decision_outcome(snapshot, command, SQLITE_NOW)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        accepted = decide(
            snapshot,
            bind_transition(
                activation,
                ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(1)),
            ),
            SQLITE_NOW,
        )
        assert accepted.attempt_change is not None
        self.assertEqual(ArtifactRefId(1), accepted.attempt_change.brief_artifact_ref_id)

    def test_resume_may_replace_the_attempt_brief_with_one_existing_brief_reference(self) -> None:
        without_attempt = LedgerSnapshot(
            "project-revision",
            1,
            (
                WorkItem(
                    ItemId("ready-item"),
                    WorkState.PAUSED,
                    None,
                    (),
                    None,
                    "test",
                    "resume",
                    "",
                ),
            ),
            artifacts=(ArtifactRecord(ArtifactRefId(1), "brief"),),
        )
        rejected_without_attempt = decision_outcome(
            without_attempt,
            bind_transition(action(ActionKind.RESUME, "ready-item"), ResumeInput(ArtifactRefId(1))),
            SQLITE_NOW,
        )
        self.assertIsInstance(rejected_without_attempt, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected_without_attempt.code)

        paused = WorkItem(
            ItemId("ready-item"),
            WorkState.PAUSED,
            None,
            (),
            AttemptId("ready-item-1"),
            "test",
            "resume",
            "",
        )
        snapshot = LedgerSnapshot(
            "project-revision",
            1,
            (paused,),
            attempts=(
                AttemptRecord(
                    AttemptId("ready-item-1"),
                    ItemId("ready-item"),
                    AttemptState.PAUSED,
                    brief_artifact_ref_id=ArtifactRefId(1),
                ),
            ),
            artifacts=(
                ArtifactRecord(ArtifactRefId(1), "brief"),
                ArtifactRecord(ArtifactRefId(2), "brief"),
                ArtifactRecord(ArtifactRefId(3), "design"),
            ),
        )
        resume = action(ActionKind.RESUME, "ready-item")

        for value in (ResumeInput(ArtifactRefId(99)), ResumeInput(ArtifactRefId(3))):
            with self.subTest(value=value):
                rejected = decision_outcome(snapshot, bind_transition(resume, value), SQLITE_NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        accepted = decide(snapshot, bind_transition(resume, ResumeInput(ArtifactRefId(2))), SQLITE_NOW)
        assert accepted.attempt_change is not None
        self.assertEqual(ArtifactRefId(2), accepted.attempt_change.brief_artifact_ref_id)

    def test_legacy_input_contract_remains_explicitly_separate(self) -> None:
        self.assertEqual(EmptyInput(), parse_legacy_transition_input("submit-review", b"{}"))
        self.assertIn(b'"properties":{}', encoded_legacy_transition_input_schema("submit-review"))
        legacy_activation = parse_legacy_transition_input(
            "activate",
            b'{"attempt":"ready-item-1","branch":"branch","base_revision":"base","owner":"task"}',
        )
        self.assertIsInstance(legacy_activation, LegacyActivateInput)


class ExactMutationAuthorityTest(unittest.TestCase):
    def test_action_preserves_every_attempt_and_resource_fact(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())
        selected = _stored_action(snapshot)
        authority = selected.command_authority
        self.assertIsNotNone(authority)
        self.assertEqual((), selected.resource_claims)
        assert authority is not None
        self.assertEqual(
            (2, "work-a", "7", "work-a-1", "8", "worker", "host-a", "attempt-lease-a", 3),
            (
                authority.host_epoch,
                authority.item,
                authority.item_subject_revision,
                authority.attempt,
                authority.attempt_subject_revision,
                authority.task_id,
                authority.host_id,
                authority.lease_id,
                authority.generation,
            ),
        )
        self.assertEqual(1, len(selected.resource_capabilities))
        capability = selected.resource_capabilities[0]
        self.assertEqual(
            ("workspace", "reservation-a", 1, "workspace-on-host", 4, 2, SQLITE_DIGEST, "use-successor", 3),
            (
                capability.resource_id,
                capability.reservation_id,
                capability.reservation_generation,
                capability.instance_id,
                capability.instance_subject_revision,
                capability.locator_observation_generation,
                capability.locator_observation_digest,
                capability.task_use_lease_id,
                capability.task_use_generation,
            ),
        )

    def test_exact_rediscovery_rejects_single_fact_substitutions_not_global_revision(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state)
        selected = _stored_action(snapshot)
        authority = selected.command_authority
        assert authority is not None
        capability = selected.resource_capabilities[0]
        substitutions = (
            replace(selected, command_authority=replace(authority, generation=authority.generation + 1)),
            replace(
                selected,
                resource_capabilities=(
                    replace(
                        capability,
                        locator_observation_generation=capability.locator_observation_generation + 1,
                    ),
                ),
            ),
        )
        for supplied in substitutions:
            with self.subTest(supplied=supplied):
                rejected = rediscover_action_outcome(snapshot, _worker_actor(), supplied)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)

        advanced_state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                project=replace(state.lifecycle.project, revision=state.lifecycle.project.revision + 1),
            ),
        )
        rediscovered = rediscover_action(project_decision_snapshot(advanced_state), _worker_actor(), selected)
        self.assertEqual(selected.action_id, rediscovered.action_id)


class ResourceIntentDecisionTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        state = complete_sqlite_state()
        self.with_intent = project_decision_snapshot(state)
        self.snapshot = project_decision_snapshot(
            replace(state, resources=replace(state.resources, mutation_intents=()))
        )
        self.action = _stored_action(self.snapshot)

    def test_planned_intent_blocks_every_attempt_lifecycle_boundary(self) -> None:
        coordination = self.with_intent.coordination_authority
        assert coordination is not None
        coordinator = ActorAuthority(
            Role.COORDINATOR,
            AuthorizationKind.COORDINATION,
            coordination.generation,
            coordination.lease_id,
        )
        coordinator_actions = available_actions(self.with_intent, coordinator)
        values: tuple[tuple[ActionKind, TransitionInput], ...] = (
            (ActionKind.PAUSE, ReasonInput("Pause.")),
            (ActionKind.BLOCK, BlockInput("Block.", ())),
            (ActionKind.COMPLETE, EvidenceInput("Complete.")),
        )
        commands = [
            bind_transition(next(action for action in coordinator_actions if action.kind == kind), value)
            for kind, value in values
        ]
        commands.append(bind_transition(self.action, SubmitReviewInput(CandidateId("candidate"))))

        for command in commands:
            with self.subTest(command=type(command).__name__):
                result = decision_outcome(self.with_intent, command, SQLITE_NOW + timedelta(seconds=1))
                self.assertIsInstance(result, DecisionFailure)
                self.assertEqual(DecisionFailureCode.RESOURCE_MUTATION_INTENT_UNRESOLVED, result.code)

    def test_attempt_lifecycle_boundaries_fence_every_current_task_use_grant(self) -> None:
        coordination = self.snapshot.coordination_authority
        assert coordination is not None
        coordinator = ActorAuthority(
            Role.COORDINATOR,
            AuthorizationKind.COORDINATION,
            coordination.generation,
            coordination.lease_id,
        )
        coordinator_actions = available_actions(self.snapshot, coordinator)
        commands = (
            bind_transition(
                next(action for action in coordinator_actions if action.kind == ActionKind.PAUSE),
                ReasonInput("Pause."),
            ),
            bind_transition(
                next(action for action in coordinator_actions if action.kind == ActionKind.BLOCK),
                BlockInput("Block.", ()),
            ),
            bind_transition(self.action, SubmitReviewInput(CandidateId("candidate"))),
        )

        for command in commands:
            with self.subTest(command=type(command).__name__):
                result = decision_outcome(self.snapshot, command, SQLITE_NOW + timedelta(seconds=1))
                self.assertNotIsInstance(result, DecisionFailure)
                self.assertEqual(1, len(result.resource_use_lease_changes))
                self.assertEqual(UseLeaseState.REVOKED, result.resource_use_lease_changes[0].after.state)


if __name__ == "__main__":
    unittest.main()
