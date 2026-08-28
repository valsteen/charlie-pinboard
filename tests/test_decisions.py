import hashlib
import unittest
from dataclasses import replace as replace_dataclass
from datetime import UTC, datetime

from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.decisions import available_actions as available_actions_outcome
from charlie_pinboard.domain.decisions import bind_transition as bind_transition_outcome
from charlie_pinboard.domain.decisions import decide as decision_outcome
from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.history import (
    item_scope_bytes as item_scope_bytes_outcome,
)
from charlie_pinboard.domain.history import (
    item_scope_digest as item_scope_digest_outcome,
)
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import (
    accept_proposal_input as AcceptProposalInput,
)
from tests.domain_support import (
    action as make_action,
)
from tests.domain_support import (
    attempt_record as AttemptRecord,
)
from tests.domain_support import (
    defer_input as DeferInput,
)
from tests.domain_support import (
    expect_success,
    replace,
)
from tests.domain_support import (
    item_scope as make_item_scope,
)
from tests.domain_support import (
    proposal_record as ProposalRecord,
)
from tests.domain_support import (
    scope_anchor as ScopeAnchor,
)
from tests.domain_support import (
    scope_dependency as ScopeDependency,
)
from tests.domain_support import (
    transfer_coordinator_input as TransferCoordinatorInput,
)

NOW = datetime(2026, 8, 21, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


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


def item_scope_bytes(scope: work_models.ItemScope) -> bytes:
    return expect_success(item_scope_bytes_outcome(scope))


def item_scope_digest(scope: work_models.ItemScope) -> str:
    return expect_success(item_scope_digest_outcome(scope))


def item(item_id: str, state: work_models.WorkState, *, attempt: str | None = None) -> work_models.WorkItem:
    return work_models.WorkItem(
        ItemId(item_id),
        state,
        None,
        (),
        AttemptId(attempt) if attempt is not None else None,
        "design",
        "continue",
        "",
        1,
    )


def action(kind: decision_models.ActionKind, subject: str) -> decision_models.Action:
    return replace_dataclass(make_action(kind, subject), authorization=decision_models.AuthorizationKind.COORDINATOR)


def native_scope(*, artifacts: tuple[work_models.ScopeArtifact, ...] | None = None) -> work_models.ItemScope:
    return make_item_scope(
        item_id="build-map",
        user_label="Build the map",
        trigger="A route is missing",
        why_it_matters="The party cannot travel",
        effect="Add navigable routes",
        unlock="Reach the next area",
        dependencies=(ScopeDependency(1, "survey-east"), ScopeDependency(0, "survey-west")),
        artifacts=artifacts
        if artifacts is not None
        else (
            work_models.ScopeArtifact(
                work_models.ArtifactRole.PLAN, 0, "plan", "route-plan", 2, "artifacts/plans/route-plan/2.md", DIGEST_B
            ),
            work_models.ScopeArtifact(
                work_models.ArtifactRole.REQUIREMENTS,
                0,
                "requirements",
                "route-needs",
                1,
                "artifacts/requirements/route-needs/1.md",
                DIGEST_A,
            ),
            work_models.ScopeArtifact(
                work_models.ArtifactRole.DESIGN,
                0,
                "design",
                "route-design",
                1,
                "artifacts/designs/route-design/1.md",
                "c" * 64,
            ),
        ),
    )


class LifecycleDecisionTest(unittest.TestCase):
    def test_review_continuation_is_coordination_only_and_requires_the_protected_candidate(self) -> None:
        review = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        attempt = AttemptRecord(
            "target-1", "target", work_models.AttemptState.REVIEW, protected_candidate_revision="candidate-a"
        )
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 3)
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (review,),
            attempts=(attempt,),
            attempt_authorities=(authority,),
        )

        coordinator_kinds = {
            value.kind
            for value in available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, 1
                ),
            )
        }
        coordination_actions = available_actions(
            snapshot,
            decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATION, 1
            ),
        )
        self.assertNotIn(decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, coordinator_kinds)
        selected = next(
            value
            for value in coordination_actions
            if value.kind == decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
        )

        mismatch = decision_outcome(
            snapshot,
            bind_transition(selected, work_models.AcceptReviewAndContinueInput(CandidateId("candidate-b"), "accepted")),
            NOW,
        )
        self.assertIsInstance(mismatch, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, mismatch.code)

        accepted = decide(
            snapshot,
            bind_transition(selected, work_models.AcceptReviewAndContinueInput(CandidateId("candidate-a"), "accepted")),
            NOW,
        )
        self.assertIsInstance(accepted.change, decision_models.ReviewAcceptanceChange)
        assert isinstance(accepted.change, decision_models.ReviewAcceptanceChange)
        self.assertEqual(CandidateId("candidate-a"), accepted.change.candidate)
        self.assertEqual(4, accepted.change.authority_change.after.generation)
        self.assertIsNone(accepted.change.authority_change.after.lease_id)
        self.assertEqual("accepted", accepted.receipt.evidence)

        for retained_authorities in ((), (authority, authority)):
            with self.subTest(retained_authorities=len(retained_authorities)):
                rejected = decision_outcome(
                    replace_dataclass(snapshot, attempt_authorities=retained_authorities),
                    bind_transition(
                        selected,
                        work_models.AcceptReviewAndContinueInput(CandidateId("candidate-a"), "accepted"),
                    ),
                    NOW,
                )
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, rejected.code)

        inconsistent = replace_dataclass(
            snapshot,
            attempts=(replace_dataclass(attempt, state=work_models.AttemptState.ACTIVE),),
        )
        inconsistent_kinds = {
            value.kind
            for value in available_actions(
                inconsistent,
                decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATION, 1
                ),
            )
        }
        self.assertNotIn(decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, inconsistent_kinds)

        empty_evidence = decision_outcome(
            snapshot,
            bind_transition(selected, work_models.AcceptReviewAndContinueInput(CandidateId("candidate-a"), "")),
            NOW,
        )
        self.assertIsInstance(empty_evidence, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, empty_evidence.code)

    def test_blocker_actions_expose_distinct_roles_subjects_preconditions_and_effects(self) -> None:
        active = item("target", work_models.WorkState.ACTIVE, attempt="target-1")
        intake = item("unstarted", work_models.WorkState.INTAKE)
        prerequisite = item("prerequisite", work_models.WorkState.READY)
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active, intake, prerequisite),
            attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE),),
            attempt_authorities=(
                work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 4),
            ),
        )
        coordinator = available_actions(
            snapshot,
            decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, 1
            ),
        )
        worker = available_actions(
            snapshot,
            decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                decision_models.AuthorizationKind.ATTEMPT,
                4,
                LeaseId("worker-lease"),
                (AttemptId("target-1"),),
                False,
            ),
        )
        selected = {
            action.kind: action
            for action in (*coordinator, *worker)
            if action.kind
            in {
                decision_models.ActionKind.REPORT_BLOCKER,
                decision_models.ActionKind.BLOCK,
                decision_models.ActionKind.BLOCK_ITEM,
            }
        }

        expected = {
            decision_models.ActionKind.REPORT_BLOCKER: (
                "report-blocker:target-1",
                "Prepare blocker report for target",
                ("advisory", "worker", "attempt", "active-attempt"),
            ),
            decision_models.ActionKind.BLOCK: (
                "block:target-1",
                "Block active attempt for target",
                ("mutating", "coordinator", "attempt", "active-attempt"),
            ),
            decision_models.ActionKind.BLOCK_ITEM: (
                "block-item:unstarted",
                "Block unstarted work item unstarted",
                ("mutating", "coordinator", "item", "intake-item"),
            ),
        }
        self.assertEqual(set(expected), set(selected))
        for kind, (action_id, label, semantics) in expected.items():
            with self.subTest(kind=kind):
                action = selected[kind]
                descriptor = decision_models.blocker_action_descriptor(action.kind)
                self.assertIsNotNone(descriptor)
                assert descriptor is not None
                self.assertEqual(action_id, action.action_id)
                self.assertEqual(label, action.label)
                self.assertEqual(
                    semantics,
                    (
                        descriptor.effect.value,
                        descriptor.required_role.value,
                        descriptor.subject_kind.value,
                        descriptor.lifecycle_precondition.value,
                    ),
                )

        rejected = bind_transition_outcome(
            selected[decision_models.ActionKind.REPORT_BLOCKER], work_models.EmptyInput()
        )
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_MUTATING, rejected.code)

        blocked = decide(
            snapshot,
            bind_transition(
                selected[decision_models.ActionKind.BLOCK],
                work_models.BlockInput("Waiting for prerequisite.", (ItemId("prerequisite"),)),
            ),
            NOW,
        )
        self.assertIsInstance(blocked.change, decision_models.BlockAttemptChange)
        assert isinstance(blocked.change, decision_models.BlockAttemptChange)
        self.assertEqual((ItemId("prerequisite"),), blocked.change.dependencies_after)

    def test_missing_attempt_is_a_returned_failure(self) -> None:
        snapshot = LedgerSnapshot("revision", 1, ())
        command = bind_transition(
            action(decision_models.ActionKind.PAUSE, "missing-attempt"), work_models.ReasonInput("pause")
        )

        outcome = decision_outcome(snapshot, command, NOW)

        self.assertEqual(
            DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, "Attempt 'missing-attempt' does not exist."),
            outcome,
        )

    def test_terminal_decisions_require_and_return_outcome_evidence(self) -> None:
        active = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.REVIEW),),
        )

        completed_action = action(decision_models.ActionKind.COMPLETE, "target-1")
        completed = decide(
            snapshot, bind_transition(completed_action, work_models.EvidenceInput("review accepted")), NOW
        )
        self.assertEqual("review accepted", completed.receipt.evidence)
        self.assertIsInstance(completed.change, decision_models.CompletionChange)
        assert isinstance(completed.change, decision_models.CompletionChange)
        self.assertEqual("review accepted", completed.change.evidence)

        rejected = bind_transition_outcome(
            action(decision_models.ActionKind.COMPLETE, "target-1"), work_models.EmptyInput()
        )
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        intake = LedgerSnapshot("revision", 1, (item("obsolete", work_models.WorkState.INTAKE),))
        closed = decide(
            intake,
            bind_transition(
                action(decision_models.ActionKind.CLOSE, "obsolete"),
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "no longer needed"),
            ),
            NOW,
        )
        self.assertEqual(("dropped", "no longer needed"), (closed.receipt.outcome, closed.receipt.evidence))

    def test_changed_semantic_scope_blocks_the_next_attempt_boundary(self) -> None:
        current_scope = native_scope()
        current = ScopeAnchor("build-map", 2, item_scope_digest(current_scope), current_scope)
        active = item("build-map", work_models.WorkState.ACTIVE, attempt="build-map-1")
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("build-map-1", "build-map", work_models.AttemptState.ACTIVE, 1, DIGEST_A),),
            scopes=(current,),
        )

        action_ids = {
            value.action_id
            for value in available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.COORDINATOR, decision_models.AuthorizationKind.COORDINATOR, 1
                ),
            )
        }
        self.assertIn("continue:build-map-1", action_ids)
        self.assertNotIn("dispatch:build-map-1", action_ids)
        self.assertNotIn("complete:build-map-1", action_ids)
        submit = action(decision_models.ActionKind.SUBMIT_REVIEW, "build-map-1")
        command = bind_transition(submit, work_models.SubmitReviewInput(CandidateId("candidate")))
        rejected = decision_outcome(snapshot, command, NOW)
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_SCOPE_STALE, rejected.code)

    def test_transition_rejections_preserve_the_domain_boundary(self) -> None:
        ready = item("target", work_models.WorkState.READY)
        active = item("target", work_models.WorkState.ACTIVE, attempt="target-1")
        paused = item("target", work_models.WorkState.PAUSED, attempt="target-1")
        review = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        attempt_active = AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE)
        attempt_review = AttemptRecord("target-1", "target", work_models.AttemptState.REVIEW)
        stale_scope = ScopeAnchor("target", 2, DIGEST_B, replace(native_scope(), item_id="target"))
        cases = (
            (
                LedgerSnapshot("r", 1, ()),
                decision_models.ActionKind.ACTIVATE,
                "missing",
                work_models.ActivateInput(AttemptId("missing-1"), "branch", "base", "owner", ArtifactRefId(1)),
                "ITEM_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                decision_models.ActionKind.ACTIVATE,
                "target",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (item("target", work_models.WorkState.INTAKE),)),
                decision_models.ActionKind.ACTIVATE,
                "target",
                work_models.ActivateInput(AttemptId("target-1"), "branch", "base", "owner", ArtifactRefId(1)),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                decision_models.ActionKind.PAUSE,
                "missing-1",
                work_models.ReasonInput("pause"),
                "ATTEMPT_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)),
                decision_models.ActionKind.PAUSE,
                "target-1",
                work_models.ReasonInput("pause"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                decision_models.ActionKind.PAUSE,
                "target-1",
                work_models.SubmitReviewInput(CandidateId("candidate")),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                decision_models.ActionKind.BLOCK,
                "target-1",
                work_models.SubmitReviewInput(CandidateId("candidate")),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (paused,)),
                decision_models.ActionKind.COMPLETE,
                "target-1",
                work_models.EvidenceInput("done"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r",
                    1,
                    (active,),
                    attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),),
                    scopes=(stale_scope,),
                ),
                decision_models.ActionKind.COMPLETE,
                "target-1",
                work_models.EvidenceInput("done"),
                "ITEM_SCOPE_STALE",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,), history_items=(ItemId("target"),)),
                decision_models.ActionKind.COMPLETE,
                "target-1",
                work_models.EvidenceInput("done"),
                "HISTORY_RECORD_EXISTS",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                decision_models.ActionKind.CLOSE,
                "target",
                work_models.CloseInput(work_models.CloseOutcome.DONE, "done"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                decision_models.ActionKind.CLOSE,
                "target",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot(
                    "r", 1, (ready, replace(item("dependent", work_models.WorkState.READY), depends_on=("target",)))
                ),
                decision_models.ActionKind.CLOSE,
                "target",
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "obsolete"),
                "LIVE_DEPENDENTS",
            ),
            (
                LedgerSnapshot("r", 1, (ready,), history_items=(ItemId("target"),)),
                decision_models.ActionKind.CLOSE,
                "target",
                work_models.CloseInput(work_models.CloseOutcome.DONE, "done"),
                "HISTORY_RECORD_EXISTS",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                decision_models.ActionKind.RESUME,
                "target",
                work_models.ResumeInput(),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r", 1, (replace(paused, depends_on=("source",)), item("source", work_models.WorkState.READY))
                ),
                decision_models.ActionKind.RESUME,
                "target",
                work_models.ResumeInput(),
                "DEPENDENCY_NOT_SATISFIED",
            ),
            (
                LedgerSnapshot("r", 1, (paused,)),
                decision_models.ActionKind.RESUME,
                "target",
                work_models.EvidenceInput("resume"),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)),
                decision_models.ActionKind.SUBMIT_REVIEW,
                "target-1",
                work_models.SubmitReviewInput(CandidateId("candidate")),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r",
                    1,
                    (active,),
                    attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),),
                    scopes=(stale_scope,),
                ),
                decision_models.ActionKind.SUBMIT_REVIEW,
                "target-1",
                work_models.SubmitReviewInput(CandidateId("candidate")),
                "ITEM_SCOPE_STALE",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                decision_models.ActionKind.SUBMIT_REVIEW,
                "target-1",
                work_models.EvidenceInput("review"),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,)),
                decision_models.ActionKind.BLOCK_ITEM,
                "target",
                work_models.BlockInput("blocked"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (item("target", work_models.WorkState.INTAKE),)),
                decision_models.ActionKind.REOPEN,
                "target",
                work_models.EvidenceInput("reopen"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (item("target", work_models.WorkState.INTAKE),)),
                decision_models.ActionKind.MARK_READY,
                "target",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,)),
                decision_models.ActionKind.DEFER,
                "target",
                DeferInput("safe-to-defer", "later"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                decision_models.ActionKind.DEFER,
                "target",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, ()),
                decision_models.ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                AcceptProposalInput(
                    item="new-item", state=work_models.AcceptedProposalState.READY, next_action="start"
                ),
                "PROPOSAL_NOT_FOUND",
            ),
            (
                LedgerSnapshot(
                    "r",
                    1,
                    (item("proposal", work_models.WorkState.INTAKE),),
                    proposals=(ProposalRecord("proposal", "p1"),),
                ),
                decision_models.ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot(
                    "r",
                    1,
                    (ready, item("proposal", work_models.WorkState.INTAKE)),
                    proposals=(ProposalRecord("proposal", "p1"),),
                ),
                decision_models.ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                AcceptProposalInput(item="target", state=work_models.AcceptedProposalState.READY, next_action="start"),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)),
                decision_models.ActionKind.MERGE_PROPOSAL,
                "proposal",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)),
                decision_models.ActionKind.REJECT_PROPOSAL,
                "proposal",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, ()),
                decision_models.ActionKind.TRANSFER_COORDINATOR,
                "ledger",
                TransferCoordinatorInput("task", "host"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (), can_transfer_coordinator=True),
                decision_models.ActionKind.TRANSFER_COORDINATOR,
                "ledger",
                work_models.EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (), can_transfer_coordinator=True),
                decision_models.ActionKind.TRANSFER_COORDINATOR,
                "ledger",
                TransferCoordinatorInput("task", "host"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, ()),
                decision_models.ActionKind.INSPECT,
                "ledger",
                work_models.EmptyInput(),
                "ACTION_NOT_MUTATING",
            ),
        )
        for snapshot, kind, subject, value, code in cases:
            with self.subTest(kind=kind.value, code=code):
                bound = bind_transition_outcome(action(kind, subject), value)
                if isinstance(bound, DecisionFailure):
                    rejected = bound
                else:
                    rejected = decision_outcome(snapshot, bound, NOW)
                    self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode(code), rejected.code)


class ScopeContractTest(unittest.TestCase):
    def test_scope_has_frozen_canonical_bytes_and_evidence_is_operational(self) -> None:
        scope = native_scope()
        expected = (
            b'{"artifacts":[{"content_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
            b'"key":"route-design","kind":"design","position":0,"revision":1,"role":"design",'
            b'"selector":"artifacts/designs/route-design/1.md"},{"content_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            b'"key":"route-plan","kind":"plan","position":0,"revision":2,"role":"plan",'
            b'"selector":"artifacts/plans/route-plan/2.md"},{"content_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"key":"route-needs","kind":"requirements","position":0,"revision":1,"role":"requirements",'
            b'"selector":"artifacts/requirements/route-needs/1.md"}],"dependencies":[{"dependency_id":"survey-west","position":0},'
            b'{"dependency_id":"survey-east","position":1}],"effect":"Add navigable routes","item_id":"build-map",'
            b'"schema":"item-scope/v2",'
            b'"trigger":"A route is missing","unlock":"Reach the next area","user_label":"Build the map",'
            b'"why_it_matters":"The party cannot travel"}\n'
        )
        self.assertEqual(expected, item_scope_bytes(scope))
        self.assertEqual(hashlib.sha256(expected).hexdigest(), item_scope_digest(scope))

        evidence = work_models.ScopeArtifact(
            work_models.ArtifactRole.EVIDENCE,
            0,
            "evidence",
            "observation",
            1,
            "artifacts/evidence/observation/1.json",
            DIGEST_A,
        )
        self.assertEqual(
            item_scope_bytes(scope), item_scope_bytes(replace(scope, artifacts=(*scope.artifacts, evidence)))
        )

        sparse = make_item_scope("sparse-item", "Sparse item", None, None, None, None)
        self.assertEqual(
            b'{"artifacts":[],"dependencies":[],"effect":null,"item_id":"sparse-item",'
            b'"schema":"item-scope/v2","trigger":null,"unlock":null,'
            b'"user_label":"Sparse item","why_it_matters":null}\n',
            item_scope_bytes(sparse),
        )

    def test_scope_rejects_noncanonical_positions_and_semantic_duplicates(self) -> None:
        invalid = (
            replace(native_scope(), dependencies=(ScopeDependency(1, "missing-zero"),)),
            replace(
                native_scope(),
                artifacts=(
                    work_models.ScopeArtifact(
                        work_models.ArtifactRole.PLAN, 0, "plan", "same", 1, "artifacts/plans/same/1.md", DIGEST_A
                    ),
                    work_models.ScopeArtifact(
                        work_models.ArtifactRole.PLAN, 0, "plan", "other", 1, "artifacts/plans/other/1.md", DIGEST_B
                    ),
                ),
            ),
        )
        for scope in invalid:
            with self.subTest(scope=scope):
                rejected = item_scope_bytes_outcome(scope)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ITEM_SCOPE_INVALID, rejected.code)

    def test_scope_rejects_malformed_semantic_fields_as_one_contract(self) -> None:
        artifact = work_models.ScopeArtifact(
            work_models.ArtifactRole.PLAN,
            0,
            "plan",
            "route-plan",
            1,
            "artifacts/plans/route-plan/1.md",
            DIGEST_A,
        )
        invalid = (
            replace(native_scope(artifacts=(artifact,)), item_id=""),
            replace(native_scope(artifacts=(artifact,)), trigger=""),
            replace(
                native_scope(artifacts=(artifact,)),
                dependencies=(ScopeDependency(0, "same"), ScopeDependency(1, "same")),
            ),
            native_scope(artifacts=(replace(artifact, position=-1),)),
            native_scope(artifacts=(replace(artifact, kind="design"),)),
            native_scope(artifacts=(replace(artifact, revision=0),)),
            native_scope(artifacts=(replace(artifact, selector=""),)),
            native_scope(artifacts=(replace(artifact, content_sha256="not-a-digest"),)),
            native_scope(artifacts=(replace(artifact, selector="../route-plan.md"),)),
            native_scope(
                artifacts=(
                    artifact,
                    replace(artifact, position=1),
                )
            ),
            native_scope(artifacts=(artifact, replace(artifact, key="other", position=2))),
        )
        for scope in invalid:
            with self.subTest(scope=scope):
                rejected = item_scope_bytes_outcome(scope)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ITEM_SCOPE_INVALID, rejected.code)


if __name__ == "__main__":
    unittest.main()
