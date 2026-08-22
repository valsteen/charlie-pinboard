import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from repo_work.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    DecisionError,
    ResourceToken,
    Role,
    advance_scope,
    assign_resource,
    available_actions,
    decide,
    decide_planning_resolution,
    item_scope_bytes,
    item_scope_change_outcome,
    item_scope_digest,
    planning_impact_outcome,
    planning_resolution_outcome,
    reallocate_resource,
    release_resource,
    resolve_planning_obligation,
    revoke_resource,
    validate_history_outcome,
    validate_mutation_resources,
    validate_planning_impact,
)
from repo_work.model import (
    ArtifactRole,
    AttemptAuthority,
    AttemptRecord,
    AttemptState,
    ItemScope,
    LedgerSnapshot,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    ProposalRecord,
    QueueItem,
    ReservationState,
    ResourceAuthority,
    ResourceDefinition,
    ResourceInstance,
    ResourceRequirement,
    ResourceReservation,
    ResourceUseLease,
    ScopeAnchor,
    ScopeArtifact,
    ScopeDependency,
    UseLeaseState,
    WorkState,
)
from repo_work.transition_input import (
    AcceptedProposalState,
    AcceptProposalInput,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    ReasonInput,
    TransferCoordinatorInput,
)

NOW = datetime(2026, 8, 21, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def item(item_id: str, state: WorkState, *, attempt: str | None = None) -> QueueItem:
    return QueueItem(item_id, state, None, (), attempt, "design", "continue", "")


def action(kind: ActionKind, subject: str) -> Action:
    return Action(
        f"{kind.value}:{subject}",
        kind,
        subject,
        kind.value,
        "revision",
        1,
        authorization=AuthorizationKind.COORDINATOR,
    )


def native_scope(*, artifacts: tuple[ScopeArtifact, ...] | None = None) -> ItemScope:
    return ItemScope(
        item_id="build-map",
        user_label="Build the map",
        trigger="A route is missing",
        why_it_matters="The party cannot travel",
        effect="Add navigable routes",
        unlock="Reach the next area",
        dependencies=(ScopeDependency(1, "survey-east"), ScopeDependency(0, "survey-west")),
        resource_requirements=(ResourceRequirement(0, "checkout-main"),),
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
    def test_action_matrix_respects_exact_planning_boundaries(self) -> None:
        items = (
            item("source", WorkState.ACTIVE, attempt="source-1"),
            item("target", WorkState.ACTIVE, attempt="target-1"),
            item("unrelated", WorkState.READY),
        )
        impact = PlanningImpact(
            "impact-1",
            "source",
            "source-1",
            1,
            DIGEST_A,
            "Target scope changed",
            "Experiment result",
            (PlanningObligation("target", 0, 1, DIGEST_A),),
        )
        snapshot = LedgerSnapshot("revision", 1, items, planning_impacts=(impact,))

        action_ids = {value.action_id for value in available_actions(snapshot, ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, 1))}

        self.assertIn("activate:unrelated", action_ids)
        self.assertIn("continue:target-1", action_ids)
        self.assertNotIn("dispatch:target-1", action_ids)
        self.assertNotIn("complete:target-1", action_ids)
        self.assertNotIn("complete:source-1", action_ids)

    def test_terminal_decisions_require_and_return_outcome_evidence(self) -> None:
        active = item("target", WorkState.REVIEW, attempt="target-1")
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("target-1", "target", AttemptState.REVIEW),),
        )

        completed = decide(snapshot, action(ActionKind.COMPLETE, "target-1"), EvidenceInput("review accepted"), NOW)
        self.assertEqual("review accepted", completed.receipt.evidence)
        self.assertEqual("review accepted", completed.item_change.outcome_evidence if completed.item_change else None)

        with self.assertRaisesRegex(DecisionError, "TRANSITION_INPUT_INVALID"):
            decide(snapshot, action(ActionKind.COMPLETE, "target-1"), EmptyInput(), NOW)

        intake = LedgerSnapshot("revision", 1, (item("obsolete", WorkState.INTAKE),))
        closed = decide(
            intake,
            action(ActionKind.CLOSE, "obsolete"),
            CloseInput(CloseOutcome.DROPPED, "no longer needed"),
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
        with self.assertRaisesRegex(DecisionError, "ITEM_SCOPE_STALE"):
            decide(snapshot, action(ActionKind.SUBMIT_REVIEW, "build-map-1"), EmptyInput(), NOW)

    def test_transition_rejections_preserve_the_domain_boundary(self) -> None:
        ready = item("target", WorkState.READY)
        active = item("target", WorkState.ACTIVE, attempt="target-1")
        paused = item("target", WorkState.PAUSED, attempt="target-1")
        review = item("target", WorkState.REVIEW, attempt="target-1")
        attempt_active = AttemptRecord("target-1", "target", AttemptState.ACTIVE)
        attempt_review = AttemptRecord("target-1", "target", AttemptState.REVIEW)
        unresolved = PlanningImpact(
            "impact-1",
            "source",
            None,
            1,
            DIGEST_A,
            "Target changed",
            "Observed",
            (PlanningObligation("target", 0, 1, DIGEST_A),),
        )
        stale_scope = ScopeAnchor("target", 2, DIGEST_B, replace(native_scope(), item_id="target"))
        cases = (
            (LedgerSnapshot("r", 1, ()), ActionKind.ACTIVATE, "missing", EmptyInput(), "ITEM_NOT_FOUND"),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.ACTIVATE, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)), ActionKind.ACTIVATE, "target", EmptyInput(), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.PAUSE, "missing-1", ReasonInput("pause"), "ATTEMPT_NOT_FOUND"),
            (LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)), ActionKind.PAUSE, "target-1", ReasonInput("pause"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)), ActionKind.PAUSE, "target-1", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)), ActionKind.BLOCK, "target-1", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (paused,)), ActionKind.COMPLETE, "target-1", EvidenceInput("done"), "ACTION_NOT_AVAILABLE"),
            (
                LedgerSnapshot("r", 1, (item("source", WorkState.READY), active), attempts=(attempt_active,), planning_impacts=(unresolved,)),
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
                "PLANNING_IMPACT_UNRESOLVED",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),), scopes=(stale_scope,)),
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
                "ITEM_SCOPE_STALE",
            ),
            (LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,), history_items=("target",)), ActionKind.COMPLETE, "target-1", EvidenceInput("done"), "HISTORY_RECORD_EXISTS"),
            (LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)), ActionKind.CLOSE, "target", CloseInput(CloseOutcome.DONE, "done"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.CLOSE, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (
                LedgerSnapshot("r", 1, (ready, replace(item("dependent", WorkState.READY), depends_on=("target",)))),
                ActionKind.CLOSE,
                "target",
                CloseInput(CloseOutcome.DROPPED, "obsolete"),
                "LIVE_DEPENDENTS",
            ),
            (LedgerSnapshot("r", 1, (ready,), history_items=("target",)), ActionKind.CLOSE, "target", CloseInput(CloseOutcome.DONE, "done"), "HISTORY_RECORD_EXISTS"),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.RESUME, "target", EmptyInput(), "ACTION_NOT_AVAILABLE"),
            (
                LedgerSnapshot("r", 1, (replace(paused, depends_on=("source",)), item("source", WorkState.READY))),
                ActionKind.RESUME,
                "target",
                EmptyInput(),
                "DEPENDENCY_NOT_SATISFIED",
            ),
            (LedgerSnapshot("r", 1, (paused,)), ActionKind.RESUME, "target", EvidenceInput("resume"), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (review,), attempts=(attempt_review,)), ActionKind.SUBMIT_REVIEW, "target-1", EmptyInput(), "ACTION_NOT_AVAILABLE"),
            (
                LedgerSnapshot("r", 1, (item("source", WorkState.READY), active), attempts=(attempt_active,), planning_impacts=(unresolved,)),
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                EmptyInput(),
                "PLANNING_IMPACT_UNRESOLVED",
            ),
            (
                LedgerSnapshot("r", 1, (active,), attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),), scopes=(stale_scope,)),
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                EmptyInput(),
                "ITEM_SCOPE_STALE",
            ),
            (LedgerSnapshot("r", 1, (active,), attempts=(attempt_active,)), ActionKind.SUBMIT_REVIEW, "target-1", EvidenceInput("review"), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (active,)), ActionKind.BLOCK_ITEM, "target", BlockInput("blocked"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)), ActionKind.REOPEN, "target", EvidenceInput("reopen"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (item("target", WorkState.INTAKE),)), ActionKind.MARK_READY, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (active,)), ActionKind.DEFER, "target", DeferInput("safe-to-defer", "later"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.DEFER, "target", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, ()), ActionKind.ACCEPT_PROPOSAL, "proposal", EmptyInput(), "PROPOSAL_NOT_FOUND"),
            (LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)), ActionKind.ACCEPT_PROPOSAL, "proposal", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (
                LedgerSnapshot("r", 1, (ready,), proposals=(ProposalRecord("proposal", "p1"),)),
                ActionKind.ACCEPT_PROPOSAL,
                "proposal",
                AcceptProposalInput(item="target", state=AcceptedProposalState.READY, next_action="start"),
                "ITEM_ALREADY_EXISTS",
            ),
            (LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)), ActionKind.MERGE_PROPOSAL, "proposal", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, (), proposals=(ProposalRecord("proposal", "p1"),)), ActionKind.REJECT_PROPOSAL, "proposal", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, ()), ActionKind.TRANSFER_COORDINATOR, "ledger", TransferCoordinatorInput("task", "host"), "ACTION_NOT_AVAILABLE"),
            (LedgerSnapshot("r", 1, (), can_transfer_coordinator=True), ActionKind.TRANSFER_COORDINATOR, "ledger", EmptyInput(), "TRANSITION_INPUT_INVALID"),
            (LedgerSnapshot("r", 1, ()), ActionKind.INSPECT, "ledger", EmptyInput(), "ACTION_NOT_MUTATING"),
        )
        for snapshot, kind, subject, value, code in cases:
            with self.subTest(kind=kind.value, code=code), self.assertRaises(DecisionError) as raised:
                decide(snapshot, action(kind, subject), value, NOW)
            self.assertEqual(code, raised.exception.code)


class ScopeAndPlanningContractTest(unittest.TestCase):
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
            b'"resource_requirements":[{"position":0,"resource_id":"checkout-main"}],"schema":"item-scope/v1",'
            b'"trigger":"A route is missing","unlock":"Reach the next area","user_label":"Build the map",'
            b'"why_it_matters":"The party cannot travel"}\n'
        )
        self.assertEqual(expected, item_scope_bytes(scope))
        self.assertEqual(hashlib.sha256(expected).hexdigest(), item_scope_digest(scope))

        evidence = ScopeArtifact(ArtifactRole.EVIDENCE, 0, "evidence", "observation", 1, "artifacts/evidence/observation/1.json", DIGEST_A)
        self.assertEqual(item_scope_bytes(scope), item_scope_bytes(replace(scope, artifacts=(*scope.artifacts, evidence))))

        anchor = advance_scope(None, "build-map", scope)
        self.assertIs(anchor, advance_scope(anchor, "build-map", replace(scope, artifacts=(*scope.artifacts, evidence))))
        changed = advance_scope(anchor, "build-map", replace(scope, user_label="Build a safer map"))
        self.assertEqual(2, changed.revision)

        legacy = ItemScope("legacy-item", "Legacy item", None, None, None, None)
        self.assertEqual(
            b'{"artifacts":[],"dependencies":[],"effect":null,"item_id":"legacy-item",'
            b'"resource_requirements":[],"schema":"item-scope/v1","trigger":null,"unlock":null,'
            b'"user_label":"Legacy item","why_it_matters":null}\n',
            item_scope_bytes(legacy),
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
            with self.subTest(scope=scope), self.assertRaisesRegex(DecisionError, "ITEM_SCOPE_INVALID"):
                item_scope_bytes(scope)

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
            with self.subTest(scope=scope), self.assertRaises(DecisionError) as raised:
                item_scope_bytes(scope)
            self.assertEqual("ITEM_SCOPE_INVALID", raised.exception.code)

    def test_planning_resolution_enforces_scope_replacement_and_evidence_contracts(self) -> None:
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (item("source", WorkState.ACTIVE, attempt="source-1"), item("target", WorkState.READY)),
            attempts=(AttemptRecord("source-1", "source", AttemptState.ACTIVE),),
        )
        impact = PlanningImpact(
            "impact-1",
            "source",
            "source-1",
            1,
            DIGEST_A,
            "Target needs refinement",
            "Observed mismatch",
            (PlanningObligation("target", 0, 1, DIGEST_A),),
        )
        validate_planning_impact(snapshot, impact)

        revised = resolve_planning_obligation(
            snapshot,
            impact,
            "target",
            PlanningDisposition.REVISED,
            reason="Updated scope",
            resulting_scope_revision=2,
            resulting_scope_digest=DIGEST_B,
        )
        self.assertEqual("planning-impact-resolution/v1", planning_resolution_outcome(revised, "target").outcome_schema)
        validate_history_outcome(
            "planning-impact-resolution/v1",
            planning_resolution_outcome(revised, "target").payload,
        )

        with self.assertRaisesRegex(DecisionError, "PLANNING_RESOLUTION_INVALID"):
            resolve_planning_obligation(
                snapshot,
                impact,
                "target",
                PlanningDisposition.SUPERSEDED,
                reason="Split work",
                replacements=(),
                outcome_evidence="Superseded by smaller items",
            )

        superseded = resolve_planning_obligation(
            snapshot,
            impact,
            "target",
            PlanningDisposition.SUPERSEDED,
            reason="Split work",
            replacements=("target-a", "target-b"),
            outcome_evidence="Superseded by smaller items",
        )
        payload = planning_resolution_outcome(superseded, "target").payload
        self.assertIn(b'"replacements":[{"item_id":"target-a","position":0},{"item_id":"target-b","position":1}]', payload)
        impact_history = planning_impact_outcome(impact)
        self.assertEqual("planning-impact/v1", impact_history.outcome_schema)
        validate_history_outcome(impact_history.outcome_schema, impact_history.payload)

        lifecycle_snapshot = replace(
            snapshot,
            items=(*snapshot.items, item("target-a", WorkState.READY), item("target-b", WorkState.READY)),
        )
        terminal = decide_planning_resolution(
            lifecycle_snapshot,
            impact,
            "target",
            PlanningDisposition.SUPERSEDED,
            reason="Split work",
            replacements=("target-a", "target-b"),
            outcome_evidence="Superseded by smaller items",
        )
        self.assertEqual("Superseded by smaller items", terminal.item_change.outcome_evidence if terminal.item_change else None)

    def test_planning_impact_rejections_cover_identity_scope_and_evidence(self) -> None:
        source = item("source", WorkState.ACTIVE, attempt="source-1")
        target = item("target", WorkState.READY)
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (source, target),
            attempts=(AttemptRecord("source-1", "source", AttemptState.ACTIVE),),
        )
        impact = PlanningImpact(
            "impact-1",
            "source",
            "source-1",
            1,
            DIGEST_A,
            "Target needs refinement",
            "Observed mismatch",
            (PlanningObligation("target", 0, 1, DIGEST_A),),
        )
        cases = (
            (replace(snapshot, items=(target,)), impact),
            (replace(snapshot, attempts=()), impact),
            (snapshot, replace(impact, source_scope_revision=0)),
            (snapshot, replace(impact, summary="")),
            (snapshot, replace(impact, obligations=())),
            (snapshot, replace(impact, obligations=(replace(impact.obligations[0], position=1),))),
            (
                snapshot,
                replace(
                    impact,
                    obligations=(impact.obligations[0], replace(impact.obligations[0], position=1)),
                ),
            ),
            (snapshot, replace(impact, obligations=(replace(impact.obligations[0], target="missing"),))),
            (snapshot, replace(impact, obligations=(replace(impact.obligations[0], observed_scope_revision=0),))),
            (
                replace(
                    snapshot,
                    scopes=(ScopeAnchor("source", 2, DIGEST_B, replace(native_scope(), item_id="source")),),
                ),
                impact,
            ),
            (
                replace(
                    snapshot,
                    scopes=(ScopeAnchor("target", 2, DIGEST_B, replace(native_scope(), item_id="target")),),
                ),
                impact,
            ),
        )
        for candidate, invalid_impact in cases:
            with self.subTest(impact=invalid_impact), self.assertRaises(DecisionError) as raised:
                validate_planning_impact(candidate, invalid_impact)
            self.assertIn(raised.exception.code, {"PLANNING_IMPACT_INVALID", "PLANNING_ACTION_STALE"})

    def test_planning_dispositions_produce_exact_lifecycle_effects(self) -> None:
        source = item("source", WorkState.ACTIVE, attempt="source-1")
        source_attempt = AttemptRecord("source-1", "source", AttemptState.ACTIVE)
        cases = (
            (PlanningDisposition.UNCHANGED, item("target", WorkState.READY), None, None, None, None, None),
            (
                PlanningDisposition.REVISED,
                item("target", WorkState.READY),
                None,
                None,
                2,
                DIGEST_B,
                None,
            ),
            (
                PlanningDisposition.BLOCKED,
                item("target", WorkState.ACTIVE, attempt="target-1"),
                AttemptRecord("target-1", "target", AttemptState.ACTIVE),
                WorkState.BLOCKED,
                None,
                None,
                None,
            ),
            (PlanningDisposition.DEFERRED, item("target", WorkState.READY), None, WorkState.DEFERRED, None, None, None),
            (
                PlanningDisposition.DROPPED,
                item("target", WorkState.ACTIVE, attempt="target-1"),
                AttemptRecord("target-1", "target", AttemptState.ACTIVE),
                None,
                None,
                None,
                "No longer needed",
            ),
        )
        for disposition, target, target_attempt, expected_state, resulting_revision, resulting_digest, evidence in cases:
            attempts = (source_attempt,) if target_attempt is None else (source_attempt, target_attempt)
            snapshot = LedgerSnapshot("revision", 1, (source, target), attempts=attempts)
            impact = PlanningImpact(
                "impact-1",
                "source",
                "source-1",
                1,
                DIGEST_A,
                "Target needs refinement",
                "Observed mismatch",
                (PlanningObligation("target", 0, 1, DIGEST_A),),
            )
            decision = decide_planning_resolution(
                snapshot,
                impact,
                "target",
                disposition,
                reason="Reviewed impact",
                resulting_scope_revision=resulting_revision,
                resulting_scope_digest=resulting_digest,
                outcome_evidence=evidence,
            )
            with self.subTest(disposition=disposition.value):
                self.assertEqual(
                    expected_state,
                    None if decision.item_change is None else decision.item_change.after,
                )

    def test_planning_resolution_rejects_incoherent_disposition_payloads(self) -> None:
        source = item("source", WorkState.ACTIVE, attempt="source-1")
        target = item("target", WorkState.READY)
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (source, target),
            attempts=(AttemptRecord("source-1", "source", AttemptState.ACTIVE),),
        )
        impact = PlanningImpact(
            "impact-1",
            "source",
            "source-1",
            1,
            DIGEST_A,
            "Target needs refinement",
            "Observed mismatch",
            (PlanningObligation("target", 0, 1, DIGEST_A),),
        )
        resolution_cases = (
            (PlanningDisposition.REVISED, "reason", 1, DIGEST_A, (), None),
            (PlanningDisposition.UNCHANGED, "reason", 2, DIGEST_B, (), None),
            (PlanningDisposition.SUPERSEDED, "reason", None, None, (), "split"),
            (PlanningDisposition.UNCHANGED, "reason", None, None, ("replacement",), None),
            (PlanningDisposition.DROPPED, "reason", None, None, (), None),
            (PlanningDisposition.UNCHANGED, "reason", None, None, (), "unexpected"),
            (PlanningDisposition.UNCHANGED, "", None, None, (), None),
        )
        for disposition, reason, revision, digest, replacements, evidence in resolution_cases:
            with self.subTest(disposition=disposition.value), self.assertRaisesRegex(
                DecisionError,
                "PLANNING_RESOLUTION_INVALID",
            ):
                resolve_planning_obligation(
                    snapshot,
                    impact,
                    "target",
                    disposition,
                    reason=reason,
                    resulting_scope_revision=revision,
                    resulting_scope_digest=digest,
                    replacements=replacements,
                    outcome_evidence=evidence,
                )

        with self.assertRaisesRegex(DecisionError, "PLANNING_OBLIGATION_NOT_FOUND"):
            resolve_planning_obligation(snapshot, impact, "missing", PlanningDisposition.UNCHANGED, reason="reason")
        resolved = resolve_planning_obligation(
            snapshot,
            impact,
            "target",
            PlanningDisposition.UNCHANGED,
            reason="reason",
        )
        with self.assertRaisesRegex(DecisionError, "PLANNING_ACTION_STALE"):
            resolve_planning_obligation(snapshot, resolved, "target", PlanningDisposition.UNCHANGED, reason="again")

        retained = replace(target, state=WorkState.BLOCKED, attempt="target-1")
        retained_snapshot = replace(
            snapshot,
            items=(source, retained),
            attempts=(*snapshot.attempts, AttemptRecord("target-1", "target", AttemptState.BLOCKED)),
        )
        with self.assertRaisesRegex(DecisionError, "PLANNING_RESOLUTION_INVALID"):
            decide_planning_resolution(
                retained_snapshot,
                impact,
                "target",
                PlanningDisposition.DEFERRED,
                reason="later",
            )
        with self.assertRaisesRegex(DecisionError, "PLANNING_RESOLUTION_INVALID"):
            decide_planning_resolution(
                snapshot,
                impact,
                "target",
                PlanningDisposition.SUPERSEDED,
                reason="split",
                replacements=("missing",),
                outcome_evidence="split",
            )
        with self.assertRaisesRegex(DecisionError, "PLANNING_RESOLUTION_INVALID"):
            decide_planning_resolution(
                replace(snapshot, items=(source, replace(target, state=WorkState.ACTIVE))),
                impact,
                "target",
                PlanningDisposition.DEFERRED,
                reason="later",
            )

    def test_scope_change_history_recomputes_digests_and_requires_consecutive_revisions(self) -> None:
        first = advance_scope(None, "build-map", native_scope())
        second = advance_scope(first, "build-map", replace(native_scope(), effect="Add safe navigable routes"))
        outcome = item_scope_change_outcome(first, second)
        self.assertEqual("item-scope-change/v1", outcome.outcome_schema)
        self.assertTrue(outcome.payload.endswith(b"\n"))
        validate_history_outcome(outcome.outcome_schema, outcome.payload)

        with self.assertRaisesRegex(DecisionError, "HISTORY_OUTCOME_INVALID"):
            item_scope_change_outcome(first, replace(second, revision=3))

        decoded = json.loads(outcome.payload)
        invalid_payloads = (
            json.dumps({**decoded, "extra": None}, separators=(",", ":"), sort_keys=True).encode() + b"\n",
            json.dumps({key: value for key, value in decoded.items() if key != "item_id"}, separators=(",", ":"), sort_keys=True).encode() + b"\n",
            outcome.payload.replace(b'"scope_revision":2', b'"scope_revision":true', 1),
            outcome.payload[:-1],
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(DecisionError, "HISTORY_OUTCOME_INVALID"):
                validate_history_outcome(outcome.outcome_schema, payload)


class ResourceAuthorityTest(unittest.TestCase):
    def test_mutation_requires_exact_active_reservation_and_use_lease(self) -> None:
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (),
            resource_definitions=(ResourceDefinition("checkout", "git-checkout"),),
            resource_instances=(ResourceInstance("instance-1", "checkout", "host-a", 4),),
            resource_reservations=(
                ResourceReservation("reservation-1", "checkout", "instance-1", "attempt-1", 2, ReservationState.ACTIVE),
            ),
            resource_use_leases=(
                ResourceUseLease("use-1", "reservation-1", "attempt-lease", 3, 5, UseLeaseState.ACTIVE),
            ),
            attempt_authorities=(
                AttemptAuthority(
                    "attempt-1",
                    "item-1",
                    "attempt-lease",
                    3,
                    (ResourceAuthority("checkout", "host-a", "use-1", 5),),
                ),
            ),
        )
        validate_mutation_resources(snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 5),))
        with self.assertRaisesRegex(DecisionError, "RESOURCE_USE_LEASE_STALE"):
            validate_mutation_resources(snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 4),))

        invalid_authority = (
            ("absent", replace(snapshot, attempt_authorities=())),
            (
                "crosswired lease",
                replace(
                    snapshot,
                    resource_use_leases=(
                        replace(snapshot.resource_use_leases[0], attempt_lease_id="another-attempt-lease"),
                    ),
                ),
            ),
            (
                "stale generation",
                replace(
                    snapshot,
                    resource_use_leases=(replace(snapshot.resource_use_leases[0], attempt_generation=2),),
                ),
            ),
            (
                "unrelated token",
                replace(
                    snapshot,
                    attempt_authorities=(
                        replace(
                            snapshot.attempt_authorities[0],
                            resources=(ResourceAuthority("checkout", "host-a", "other-use", 5),),
                        ),
                    ),
                ),
            ),
        )
        for name, invalid_snapshot in invalid_authority:
            with self.subTest(name=name), self.assertRaises(DecisionError):
                validate_mutation_resources(
                    invalid_snapshot,
                    "attempt-1",
                    ("checkout",),
                    (ResourceToken("checkout", "host-a", "use-1", 5),),
                )

    def test_resource_lifecycle_is_kind_neutral_and_explicit(self) -> None:
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (),
            resource_definitions=(ResourceDefinition("workspace", "opaque-workspace"),),
            resource_instances=(
                ResourceInstance("instance-a", "workspace", "host-a", 1),
                ResourceInstance("instance-b", "workspace", "host-a", 1),
            ),
        )
        assigned = assign_resource(
            snapshot,
            reservation_id="reservation-a",
            resource_id="workspace",
            instance_id="instance-a",
            attempt="attempt-1",
            generation=1,
        ).changes[0].after
        with_reservation = replace(snapshot, resource_reservations=(assigned,))

        self.assertEqual(ReservationState.RELEASED, release_resource(with_reservation, "reservation-a").changes[0].after.state)
        self.assertEqual(
            ReservationState.REVOKED_PENDING_RECOVERY,
            revoke_resource(with_reservation, "reservation-a", unresolved_intent=True).changes[0].after.state,
        )
        reallocated = reallocate_resource(
            with_reservation,
            "reservation-a",
            replacement_id="reservation-b",
            instance_id="instance-b",
            generation=2,
        )
        self.assertEqual(
            (ReservationState.RELEASED, ReservationState.ACTIVE),
            tuple(change.after.state for change in reallocated.changes),
        )

    def test_resource_rejections_distinguish_identity_reservation_instance_and_lease_failures(self) -> None:
        authority = AttemptAuthority(
            "attempt-1",
            "item-1",
            "attempt-lease",
            3,
            (ResourceAuthority("checkout", "host-a", "use-1", 5),),
        )
        reservation = ResourceReservation(
            "reservation-1",
            "checkout",
            "instance-1",
            "attempt-1",
            2,
            ReservationState.ACTIVE,
        )
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (),
            attempt_authorities=(authority,),
            resource_definitions=(ResourceDefinition("checkout", "git-checkout"),),
            resource_instances=(ResourceInstance("instance-1", "checkout", "host-a", 4),),
            resource_reservations=(reservation,),
            resource_use_leases=(ResourceUseLease("use-1", "reservation-1", "attempt-lease", 3, 5, UseLeaseState.ACTIVE),),
        )
        token = ResourceToken("checkout", "host-a", "use-1", 5)
        validation_cases = (
            ("duplicate requirement", snapshot, ("checkout", "checkout"), (token,), "RESOURCE_REQUIREMENT_INVALID"),
            ("missing token", snapshot, ("checkout",), (), "RESOURCE_RESERVATION_STALE"),
            ("missing reservation", replace(snapshot, resource_reservations=()), ("checkout",), (token,), "RESOURCE_RESERVATION_STALE"),
            (
                "wrong host",
                snapshot,
                ("checkout",),
                (replace(token, host_id="host-b"),),
                "RESOURCE_INSTANCE_REQUIRED",
            ),
            (
                "inactive use lease",
                replace(snapshot, resource_use_leases=(replace(snapshot.resource_use_leases[0], state=UseLeaseState.EXPIRED),)),
                ("checkout",),
                (token,),
                "RESOURCE_USE_LEASE_STALE",
            ),
        )
        for name, candidate, requirements, tokens, code in validation_cases:
            with self.subTest(name=name), self.assertRaises(DecisionError) as raised:
                validate_mutation_resources(candidate, "attempt-1", requirements, tokens)
            self.assertEqual(code, raised.exception.code)

        lifecycle_cases = (
            (
                "unknown reservation",
                lambda: release_resource(snapshot, "missing"),
                "RESOURCE_RESERVATION_STALE",
            ),
            (
                "unknown instance",
                lambda: assign_resource(
                    snapshot,
                    reservation_id="new",
                    resource_id="checkout",
                    instance_id="missing",
                    attempt="attempt-2",
                    generation=1,
                ),
                "RESOURCE_INSTANCE_REQUIRED",
            ),
            (
                "invalid generation",
                lambda: assign_resource(
                    replace(snapshot, resource_reservations=()),
                    reservation_id="new",
                    resource_id="checkout",
                    instance_id="instance-1",
                    attempt="attempt-2",
                    generation=0,
                ),
                "RESOURCE_RESERVATION_STALE",
            ),
            (
                "reserved instance",
                lambda: assign_resource(
                    snapshot,
                    reservation_id="new",
                    resource_id="checkout",
                    instance_id="instance-1",
                    attempt="attempt-2",
                    generation=1,
                ),
                "RESOURCE_INSTANCE_RESERVED",
            ),
            (
                "duplicate attempt requirement",
                lambda: assign_resource(
                    replace(
                        snapshot,
                        resource_instances=(
                            *snapshot.resource_instances,
                            ResourceInstance("instance-2", "checkout", "host-a", 4),
                        ),
                    ),
                    reservation_id="new",
                    resource_id="checkout",
                    instance_id="instance-2",
                    attempt="attempt-1",
                    generation=1,
                ),
                "RESOURCE_INSTANCE_RESERVED",
            ),
            (
                "released twice",
                lambda: release_resource(
                    replace(snapshot, resource_reservations=(replace(reservation, state=ReservationState.RELEASED),)),
                    "reservation-1",
                ),
                "RESOURCE_RESERVATION_STALE",
            ),
            (
                "revoked twice",
                lambda: revoke_resource(
                    replace(snapshot, resource_reservations=(replace(reservation, state=ReservationState.REVOKED),)),
                    "reservation-1",
                    unresolved_intent=False,
                ),
                "RESOURCE_RESERVATION_STALE",
            ),
        )
        for name, operation, code in lifecycle_cases:
            with self.subTest(name=name), self.assertRaises(DecisionError) as raised:
                operation()
            self.assertEqual(code, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
