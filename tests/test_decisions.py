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
    AttemptRecord,
    AttemptState,
    ItemScope,
    LedgerSnapshot,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    QueueItem,
    ReservationState,
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
from repo_work.transition_input import CloseInput, CloseOutcome, EmptyInput, EvidenceInput

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
        )
        validate_mutation_resources(snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 5),))
        with self.assertRaisesRegex(DecisionError, "RESOURCE_USE_LEASE_STALE"):
            validate_mutation_resources(snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 4),))

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


if __name__ == "__main__":
    unittest.main()
