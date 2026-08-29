import unittest
from dataclasses import replace
from datetime import datetime

from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import bind_transition as bind_transition_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.decisions import rediscover_action as rediscover_action_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.interfaces.transition_input import parse_transition_input
from tests.domain_support import action, expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state


def _worker_actor() -> decision_models.ActorAuthority:
    return decision_models.ActorAuthority(
        decision_models.Role.WORKER,
        decision_models.AuthorizationKind.ATTEMPT,
        3,
        LeaseId("attempt-lease-a"),
        (AttemptId("work-a-1"),),
        False,
    )


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def bind_transition(
    action_value: decision_models.Action, value: work_models.TransitionInput
) -> decision_models.TransitionCommand:
    return expect_success(bind_transition_outcome(action_value, value))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.Decision:
    return expect_success(decision_outcome(snapshot, command, now))


def rediscover_action(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority, supplied: decision_models.Action
) -> decision_models.Action:
    return expect_success(rediscover_action_outcome(snapshot, actor, supplied))


def _stored_action(snapshot: LedgerSnapshot) -> decision_models.SubmitReviewAction:
    selected = next(
        candidate
        for candidate in available_actions(snapshot, _worker_actor())
        if candidate.kind == decision_models.ActionKind.SUBMIT_REVIEW
    )
    assert isinstance(selected, decision_models.SubmitReviewAction)
    return selected


class TypedTransitionContractTest(unittest.TestCase):
    def test_submit_review_candidate_is_required_typed_and_preserved(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())
        submit = _stored_action(snapshot)

        rejected = bind_transition_outcome(submit, work_models.EmptyInput())
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        candidate = CandidateId("candidate-that-is-not-a-subject-revision")
        parsed = parse_transition_input(
            decision_models.ActionKind.SUBMIT_REVIEW,
            b'{"candidate":"candidate-that-is-not-a-subject-revision"}',
        )
        self.assertEqual(work_models.SubmitReviewInput(candidate), parsed)
        decision = decide(snapshot, bind_transition(submit, parsed), SQLITE_NOW)
        self.assertIsInstance(decision.change, decision_models.ReviewSubmissionChange)
        assert isinstance(decision.change, decision_models.ReviewSubmissionChange)
        self.assertEqual(candidate, decision.change.protected_candidate_after)
        self.assertEqual(SQLITE_NOW, decision.change.candidate_observed_at)

    def test_activation_requires_one_existing_brief_artifact_reference(self) -> None:
        ready = work_models.WorkItem(
            ItemId("ready-item"), work_models.WorkState.READY, None, (), None, "test", "activate", "", 1
        )
        snapshot = LedgerSnapshot(
            "project-revision",
            1,
            (ready,),
            artifacts=(
                work_models.ArtifactRecord(ArtifactRefId(1), "brief"),
                work_models.ArtifactRecord(ArtifactRefId(2), "design"),
            ),
        )
        activation = action(decision_models.ActivateAction, ItemId("ready-item"))
        rejected = bind_transition_outcome(activation, work_models.EmptyInput())
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        variants = (
            work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(99)),
            work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(2)),
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
                work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(1)),
            ),
            SQLITE_NOW,
        )
        self.assertIsInstance(accepted.change, decision_models.ActivationChange)
        assert isinstance(accepted.change, decision_models.ActivationChange)
        self.assertEqual(ArtifactRefId(1), accepted.change.brief_artifact_ref_id)

    def test_resume_may_replace_the_attempt_brief_with_one_existing_brief_reference(self) -> None:
        without_attempt = LedgerSnapshot(
            "project-revision",
            1,
            (
                work_models.WorkItem(
                    ItemId("ready-item"),
                    work_models.WorkState.PAUSED,
                    None,
                    (),
                    None,
                    "test",
                    "resume",
                    "",
                    1,
                ),
            ),
            artifacts=(work_models.ArtifactRecord(ArtifactRefId(1), "brief"),),
        )
        rejected_without_attempt = decision_outcome(
            without_attempt,
            bind_transition(
                action(decision_models.ResumeAction, ItemId("ready-item")), work_models.ResumeInput(ArtifactRefId(1))
            ),
            SQLITE_NOW,
        )
        self.assertIsInstance(rejected_without_attempt, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected_without_attempt.code)

        paused = work_models.WorkItem(
            ItemId("ready-item"),
            work_models.WorkState.PAUSED,
            None,
            (),
            AttemptId("ready-item-1"),
            "test",
            "resume",
            "",
            1,
        )
        snapshot = LedgerSnapshot(
            "project-revision",
            1,
            (paused,),
            attempts=(
                work_models.AttemptRecord(
                    AttemptId("ready-item-1"),
                    ItemId("ready-item"),
                    work_models.AttemptState.PAUSED,
                    brief_artifact_ref_id=ArtifactRefId(1),
                ),
            ),
            artifacts=(
                work_models.ArtifactRecord(ArtifactRefId(1), "brief"),
                work_models.ArtifactRecord(ArtifactRefId(2), "brief"),
                work_models.ArtifactRecord(ArtifactRefId(3), "design"),
            ),
        )
        resume = action(decision_models.ResumeAction, ItemId("ready-item"))

        for value in (work_models.ResumeInput(ArtifactRefId(99)), work_models.ResumeInput(ArtifactRefId(3))):
            with self.subTest(value=value):
                rejected = decision_outcome(snapshot, bind_transition(resume, value), SQLITE_NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        accepted = decide(snapshot, bind_transition(resume, work_models.ResumeInput(ArtifactRefId(2))), SQLITE_NOW)
        self.assertIsInstance(accepted.change, decision_models.ResumeAttemptChange)
        assert isinstance(accepted.change, decision_models.ResumeAttemptChange)
        self.assertEqual(ArtifactRefId(2), accepted.change.brief_artifact_ref_id)


class ExactMutationAuthorityTest(unittest.TestCase):
    def test_action_preserves_every_attempt_authority_fact(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())
        selected = _stored_action(snapshot)
        authority = selected.capability.command_authority
        self.assertIsNotNone(authority)
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

    def test_exact_rediscovery_rejects_single_fact_substitutions_not_global_revision(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state)
        selected = _stored_action(snapshot)
        authority = selected.capability.command_authority
        assert authority is not None
        substitutions = (
            replace(
                selected,
                capability=replace(
                    selected.capability,
                    command_authority=replace(authority, generation=authority.generation + 1),
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
        self.assertEqual(decision_models.action_id(selected), decision_models.action_id(rediscovered))


if __name__ == "__main__":
    unittest.main()
