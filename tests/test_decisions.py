import hashlib
import unittest
from datetime import UTC, datetime
from typing import cast

from charlie_pinboard.domain.decision_models import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
)
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
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.work_models import (
    AcceptedProposalState,
    ActivateInput,
    ArtifactRole,
    AttemptState,
    BlockInput,
    CloseInput,
    CloseOutcome,
    EmptyInput,
    EvidenceInput,
    ItemScope,
    ReasonInput,
    ResumeInput,
    ScopeArtifact,
    SubmitReviewInput,
    TransitionInput,
    WorkItem,
    WorkState,
)
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
    item_scope as make_item_scope,
)
from tests.domain_support import (
    proposal_record as ProposalRecord,
)
from tests.domain_support import (
    replace,
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


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    return cast(tuple[Action, ...], available_actions_outcome(snapshot, actor))


def bind_transition(action: Action, value: TransitionInput) -> TransitionCommand:
    return cast(TransitionCommand, bind_transition_outcome(action, value))


def decide(snapshot: LedgerSnapshot, command: TransitionCommand, now: datetime) -> Decision:
    return cast(Decision, decision_outcome(snapshot, command, now))


def item_scope_bytes(scope: ItemScope) -> bytes:
    return cast(bytes, item_scope_bytes_outcome(scope))


def item_scope_digest(scope: ItemScope) -> str:
    return cast(str, item_scope_digest_outcome(scope))


def item(item_id: str, state: WorkState, *, attempt: str | None = None) -> WorkItem:
    return WorkItem(
        ItemId(item_id),
        state,
        None,
        (),
        AttemptId(attempt) if attempt is not None else None,
        "design",
        "continue",
        "",
    )


def action(kind: ActionKind, subject: str) -> Action:
    return replace(make_action(kind, subject), authorization=AuthorizationKind.COORDINATOR)


def native_scope(*, artifacts: tuple[ScopeArtifact, ...] | None = None) -> ItemScope:
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
            ScopeArtifact(ArtifactRole.PLAN, 0, "plan", "route-plan", 2, "artifacts/plans/route-plan/2.md", DIGEST_B),
            ScopeArtifact(
                ArtifactRole.REQUIREMENTS,
                0,
                "requirements",
                "route-needs",
                1,
                "artifacts/requirements/route-needs/1.md",
                DIGEST_A,
            ),
            ScopeArtifact(
                ArtifactRole.DESIGN,
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
    def test_missing_attempt_is_a_returned_failure(self) -> None:
        snapshot = LedgerSnapshot("revision", 1, ())
        command = bind_transition(action(ActionKind.PAUSE, "missing-attempt"), ReasonInput("pause"))

        outcome = decision_outcome(snapshot, command, NOW)

        self.assertEqual(
            DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, "Attempt 'missing-attempt' does not exist."),
            outcome,
        )

    def test_terminal_decisions_require_and_return_outcome_evidence(self) -> None:
        active = item("target", WorkState.REVIEW, attempt="target-1")
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("target-1", "target", AttemptState.REVIEW),),
        )

        completed_action = action(ActionKind.COMPLETE, "target-1")
        completed = decide(snapshot, bind_transition(completed_action, EvidenceInput("review accepted")), NOW)
        self.assertEqual("review accepted", completed.receipt.evidence)
        self.assertEqual("review accepted", completed.item_change.outcome_evidence if completed.item_change else None)

        rejected = bind_transition_outcome(action(ActionKind.COMPLETE, "target-1"), EmptyInput())
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        intake = LedgerSnapshot("revision", 1, (item("obsolete", WorkState.INTAKE),))
        closed = decide(
            intake,
            bind_transition(
                action(ActionKind.CLOSE, "obsolete"),
                CloseInput(CloseOutcome.DROPPED, "no longer needed"),
            ),
            NOW,
        )
        self.assertEqual(("dropped", "no longer needed"), (closed.receipt.outcome, closed.receipt.evidence))

    def test_changed_semantic_scope_blocks_the_next_attempt_boundary(self) -> None:
        current_scope = native_scope()
        current = ScopeAnchor("build-map", 2, item_scope_digest(current_scope), current_scope)
        active = item("build-map", WorkState.ACTIVE, attempt="build-map-1")
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("build-map-1", "build-map", AttemptState.ACTIVE, 1, DIGEST_A),),
            scopes=(current,),
        )

        action_ids = {
            value.action_id
            for value in available_actions(
                snapshot,
                ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, 1),
            )
        }
        self.assertIn("continue:build-map-1", action_ids)
        self.assertNotIn("dispatch:build-map-1", action_ids)
        self.assertNotIn("complete:build-map-1", action_ids)
        submit = action(ActionKind.SUBMIT_REVIEW, "build-map-1")
        command = bind_transition(submit, SubmitReviewInput(CandidateId("candidate")))
        rejected = decision_outcome(snapshot, command, NOW)
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_SCOPE_STALE, rejected.code)

    def test_transition_rejections_preserve_the_domain_boundary(self) -> None:
        ready = item("target", WorkState.READY)
        active = item("target", WorkState.ACTIVE, attempt="target-1")
        paused = item("target", WorkState.PAUSED, attempt="target-1")
        review = item("target", WorkState.REVIEW, attempt="target-1")
        attempt_active = AttemptRecord("target-1", "target", AttemptState.ACTIVE)
        attempt_review = AttemptRecord("target-1", "target", AttemptState.REVIEW)
        stale_scope = ScopeAnchor("target", 2, DIGEST_B, replace(native_scope(), item_id="target"))
        cases = (
            (
                LedgerSnapshot("r", 1, ()),
                ActionKind.ACTIVATE,
                "missing",
                ActivateInput(AttemptId("missing-1"), "branch", "base", "owner", ArtifactRefId(1)),
                "ITEM_NOT_FOUND",
            ),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.ACTIVATE, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (
                LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)),
                ActionKind.ACTIVATE,
                "target",
                ActivateInput(AttemptId("target-1"), "branch", "base", "owner", ArtifactRefId(1)),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (ready,)),
                ActionKind.PAUSE,
                "missing-1",
                ReasonInput("pause"),
                "ATTEMPT_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)),
                ActionKind.PAUSE,
                "target-1",
                ReasonInput("pause"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                ActionKind.PAUSE,
                "target-1",
                SubmitReviewInput(CandidateId("candidate")),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                ActionKind.BLOCK,
                "target-1",
                SubmitReviewInput(CandidateId("candidate")),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (paused,)),
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
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
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
                "ITEM_SCOPE_STALE",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,), history_items=(ItemId("target"),)),
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
                "HISTORY_RECORD_EXISTS",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                ActionKind.CLOSE,
                "target",
                CloseInput(CloseOutcome.DONE, "done"),
                "ACTION_NOT_AVAILABLE",
            ),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.CLOSE, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (
                LedgerSnapshot("r", 1, (ready, replace(item("dependent", WorkState.READY), depends_on=("target",)))),
                ActionKind.CLOSE,
                "target",
                CloseInput(CloseOutcome.DROPPED, "obsolete"),
                "LIVE_DEPENDENTS",
            ),
            (
                LedgerSnapshot("r", 1, (ready,), history_items=(ItemId("target"),)),
                ActionKind.CLOSE,
                "target",
                CloseInput(CloseOutcome.DONE, "done"),
                "HISTORY_RECORD_EXISTS",
            ),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.RESUME, "target", ResumeInput(), "ACTION_NOT_AVAILABLE"),
            (
                LedgerSnapshot("r", 1, (replace(paused, depends_on=("source",)), item("source", WorkState.READY))),
                ActionKind.RESUME,
                "target",
                ResumeInput(),
                "DEPENDENCY_NOT_SATISFIED",
            ),
            (
                LedgerSnapshot("r", 1, (paused,)),
                ActionKind.RESUME,
                "target",
                EvidenceInput("resume"),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)),
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                SubmitReviewInput(CandidateId("candidate")),
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
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                SubmitReviewInput(CandidateId("candidate")),
                "ITEM_SCOPE_STALE",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)),
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                EvidenceInput("review"),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,)),
                ActionKind.BLOCK_ITEM,
                "target",
                BlockInput("blocked"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)),
                ActionKind.REOPEN,
                "target",
                EvidenceInput("reopen"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)),
                ActionKind.MARK_READY,
                "target",
                EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (active,)),
                ActionKind.DEFER,
                "target",
                DeferInput("safe-to-defer", "later"),
                "ACTION_NOT_AVAILABLE",
            ),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.DEFER, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (
                LedgerSnapshot("r", 1, ()),
                ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                AcceptProposalInput(item="new-item", state=AcceptedProposalState.READY, next_action="start"),
                "PROPOSAL_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)),
                ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (ready,), proposals=(ProposalRecord("proposal", "p1"),)),
                ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                AcceptProposalInput(item="target", state=AcceptedProposalState.READY, next_action="start"),
                "ITEM_ALREADY_EXISTS",
            ),
            (
                LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)),
                ActionKind.MERGE_PROPOSAL,
                "proposal",
                EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)),
                ActionKind.REJECT_PROPOSAL,
                "proposal",
                EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (
                LedgerSnapshot("r", 1, ()),
                ActionKind.TRANSFER_COORDINATOR,
                "ledger",
                TransferCoordinatorInput("task", "host"),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", 1, (), can_transfer_coordinator=True),
                ActionKind.TRANSFER_COORDINATOR,
                "ledger",
                EmptyInput(),
                "TRANSITION_INPUT_INVALID",
            ),
            (LedgerSnapshot("r", 1, ()), ActionKind.INSPECT, "ledger", EmptyInput(), "ACTION_NOT_MUTATING"),
        )
        for snapshot, kind, subject, value, code in cases:
            with self.subTest(kind=kind.value, code=code):
                bound = bind_transition_outcome(action(kind, subject), value)
                match bound:
                    case DecisionFailure():
                        rejected = bound
                    case _:
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

        evidence = ScopeArtifact(
            ArtifactRole.EVIDENCE, 0, "evidence", "observation", 1, "artifacts/evidence/observation/1.json", DIGEST_A
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
                    ScopeArtifact(ArtifactRole.PLAN, 0, "plan", "same", 1, "artifacts/plans/same/1.md", DIGEST_A),
                    ScopeArtifact(ArtifactRole.PLAN, 0, "plan", "other", 1, "artifacts/plans/other/1.md", DIGEST_B),
                ),
            ),
        )
        for scope in invalid:
            with self.subTest(scope=scope):
                rejected = item_scope_bytes_outcome(scope)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ITEM_SCOPE_INVALID, rejected.code)

    def test_scope_rejects_malformed_semantic_fields_as_one_contract(self) -> None:
        artifact = ScopeArtifact(
            ArtifactRole.PLAN,
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
