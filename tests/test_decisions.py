import hashlib
import json
import unittest
from datetime import UTC, datetime
from typing import cast

from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Role,
    available_actions,
    bind_transition,
    decide,
)
from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
from charlie_pinboard.domain.history import (
    item_scope_bytes,
    item_scope_change_outcome,
    item_scope_digest,
    planning_impact_outcome,
    validate_history_outcome,
)
from charlie_pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId, ItemId
from charlie_pinboard.domain.model import (
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
    LedgerSnapshot,
    PlanningDisposition,
    ReasonInput,
    ReservationState,
    ScopeArtifact,
    SubmitReviewInput,
    UseLeaseGenerationKind,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.domain.planning_decisions import validate_planning_impact
from tests.domain_support import (
    accept_proposal_input as AcceptProposalInput,
)
from tests.domain_support import (
    action as make_action,
)
from tests.domain_support import (
    advance_scope,
    assign_resource,
    decide_planning_resolution,
    planning_resolution_outcome,
    reallocate_resource,
    release_resource,
    replace,
    resolve_planning_obligation,
    revoke_resource,
    validate_mutation_resources,
)
from tests.domain_support import (
    attempt_authority as AttemptAuthority,
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
    planning_impact as PlanningImpact,
)
from tests.domain_support import (
    planning_obligation as PlanningObligation,
)
from tests.domain_support import (
    proposal_record as ProposalRecord,
)
from tests.domain_support import (
    resource_authority as ResourceAuthority,
)
from tests.domain_support import (
    resource_definition as ResourceDefinition,
)
from tests.domain_support import (
    resource_instance as ResourceInstance,
)
from tests.domain_support import (
    resource_requirement as ResourceRequirement,
)
from tests.domain_support import (
    resource_reservation as ResourceReservation,
)
from tests.domain_support import (
    resource_reservation_counter as ResourceReservationCounter,
)
from tests.domain_support import (
    resource_token as ResourceToken,
)
from tests.domain_support import (
    resource_use_lease as ResourceUseLease,
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

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

NOW = datetime(2026, 8, 21, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def canonical_history(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def history_replace(payload: bytes, path: tuple[str | int, ...], value: JsonValue) -> bytes:
    root = cast(JsonValue, json.loads(payload))
    if not isinstance(root, dict):
        raise AssertionError("History fixture must be an object.")
    current: JsonValue = root
    for segment in path[:-1]:
        if isinstance(segment, str) and isinstance(current, dict):  # noqa: SIM114 - preserves type narrowing
            current = current[segment]
        elif isinstance(segment, int) and isinstance(current, list):
            current = current[segment]
        else:
            raise AssertionError("History fixture path is invalid.")
    final = path[-1]
    if isinstance(final, str) and isinstance(current, dict):  # noqa: SIM114 - preserves type narrowing
        current[final] = value
    elif isinstance(final, int) and isinstance(current, list):
        current[final] = value
    else:
        raise AssertionError("History fixture path is invalid.")
    return canonical_history(root)


def history_without(payload: bytes, member: str) -> bytes:
    root = cast(JsonValue, json.loads(payload))
    if not isinstance(root, dict):
        raise AssertionError("History fixture must be an object.")
    del root[member]
    return canonical_history(root)


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

        action_ids = {
            value.action_id
            for value in available_actions(snapshot, ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, 1))
        }

        self.assertIn("activate:unrelated", action_ids)
        self.assertIn("continue:target-1", action_ids)
        self.assertNotIn("dispatch:target-1", action_ids)
        self.assertNotIn("complete:target-1", action_ids)
        self.assertNotIn("complete:source-1", action_ids)

    def test_terminal_decisions_require_and_return_outcome_evidence(self) -> None:
        active = item("target", WorkState.REVIEW, attempt="target-1")
        reservation = ResourceReservation(
            "reservation-1",
            "checkout",
            "instance-1",
            "target-1",
            2,
            ReservationState.ACTIVE,
        )
        fenced_reservation = replace(
            reservation,
            reservation_id="fenced-reservation",
            instance_id="fenced-instance",
            generation=1,
        )
        current_grant = ResourceUseLease(
            "current-grant",
            reservation.reservation_id,
            "attempt-lease",
            3,
            5,
            UseLeaseState.ACTIVE,
            UseLeaseGenerationKind.GRANT,
        )
        historical_grant = replace(
            current_grant,
            lease_id="historical-grant",
            generation=3,
            state=UseLeaseState.REVOKED,
        )
        historical_fence = replace(
            current_grant,
            lease_id="historical-fence",
            generation=4,
            state=UseLeaseState.REVOKED,
            generation_kind=UseLeaseGenerationKind.FENCE,
        )
        fenced_grant = replace(
            current_grant,
            lease_id="fenced-grant",
            reservation_id=fenced_reservation.reservation_id,
            generation=1,
        )
        later_fence = replace(
            fenced_grant,
            lease_id="later-fence",
            generation=2,
            state=UseLeaseState.REVOKED,
            generation_kind=UseLeaseGenerationKind.FENCE,
        )
        snapshot = LedgerSnapshot(
            "revision",
            1,
            (active,),
            attempts=(AttemptRecord("target-1", "target", AttemptState.REVIEW),),
            resource_reservations=(reservation, fenced_reservation),
            resource_use_leases=(historical_grant, historical_fence, current_grant, fenced_grant, later_fence),
        )

        completed_action = action(ActionKind.COMPLETE, "target-1")
        completed = decide(snapshot, bind_transition(completed_action, EvidenceInput("review accepted")), NOW)
        self.assertEqual("review accepted", completed.receipt.evidence)
        self.assertEqual("review accepted", completed.item_change.outcome_evidence if completed.item_change else None)
        self.assertEqual(
            (
                (reservation, replace(reservation, state=ReservationState.RELEASED)),
                (fenced_reservation, replace(fenced_reservation, state=ReservationState.RELEASED)),
            ),
            tuple((change.before, change.after) for change in completed.reservation_changes),
        )
        self.assertEqual(
            ((current_grant, replace(current_grant, state=UseLeaseState.RELEASED)),),
            tuple((change.before, change.after) for change in completed.resource_use_lease_changes),
        )

        with self.assertRaisesRegex(DecisionError, "TRANSITION_INPUT_INVALID"):
            bind_transition(action(ActionKind.COMPLETE, "target-1"), EmptyInput())

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
        with self.assertRaisesRegex(DecisionError, "ITEM_SCOPE_STALE"):
            submit = action(ActionKind.SUBMIT_REVIEW, "build-map-1")
            decide(snapshot, bind_transition(submit, SubmitReviewInput(CandidateId("candidate"))), NOW)

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
                    (item("source", WorkState.READY), active),
                    attempts=(attempt_active,),
                    planning_impacts=(unresolved,),
                ),
                ActionKind.COMPLETE,
                "target-1",
                EvidenceInput("done"),
                "PLANNING_IMPACT_UNRESOLVED",
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
            (LedgerSnapshot("r", 1, (ready,)), ActionKind.RESUME, "target", EmptyInput(), "ACTION_NOT_AVAILABLE"),
            (
                LedgerSnapshot("r", 1, (replace(paused, depends_on=("source",)), item("source", WorkState.READY))),
                ActionKind.RESUME,
                "target",
                EmptyInput(),
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
                    (item("source", WorkState.READY), active),
                    attempts=(attempt_active,),
                    planning_impacts=(unresolved,),
                ),
                ActionKind.SUBMIT_REVIEW,
                "target-1",
                SubmitReviewInput(CandidateId("candidate")),
                "PLANNING_IMPACT_UNRESOLVED",
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
            with self.subTest(kind=kind.value, code=code), self.assertRaises(DecisionError) as raised:
                command = bind_transition(action(kind, subject), value)
                decide(snapshot, command, NOW)
            self.assertEqual(DecisionErrorCode(code), raised.exception.code)


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

        evidence = ScopeArtifact(
            ArtifactRole.EVIDENCE, 0, "evidence", "observation", 1, "artifacts/evidence/observation/1.json", DIGEST_A
        )
        self.assertEqual(
            item_scope_bytes(scope), item_scope_bytes(replace(scope, artifacts=(*scope.artifacts, evidence)))
        )

        anchor = advance_scope(None, "build-map", scope)
        self.assertIs(
            anchor, advance_scope(anchor, "build-map", replace(scope, artifacts=(*scope.artifacts, evidence)))
        )
        changed = advance_scope(anchor, "build-map", replace(scope, user_label="Build a safer map"))
        self.assertEqual(2, changed.revision)

        legacy = make_item_scope("legacy-item", "Legacy item", None, None, None, None)
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
            self.assertEqual(DecisionErrorCode.ITEM_SCOPE_INVALID, raised.exception.code)

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
        resolution_history = planning_resolution_outcome(revised, "target")
        self.assertEqual("planning-impact-resolution/v1", resolution_history.outcome_schema)
        validate_history_outcome(resolution_history.outcome_schema, resolution_history.payload)

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
        self.assertIn(
            b'"replacements":[{"item_id":"target-a","position":0},{"item_id":"target-b","position":1}]', payload
        )
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
        self.assertEqual(
            "Superseded by smaller items", terminal.item_change.outcome_evidence if terminal.item_change else None
        )

    def test_planning_impact_history_has_frozen_bytes_and_rejects_malformed_records(self) -> None:
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
        fixture = (
            b'{"evidence":"Observed mismatch","impact_id":"impact-1","source":{"attempt_id":"source-1","item_id":"source",'
            b'"scope":{"scope_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scope_revision":1}},'
            b'"summary":"Target needs refinement","targets":[{"item_id":"target","position":0,"scope":'
            b'{"scope_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scope_revision":1}}]}\n'
        )
        outcome = planning_impact_outcome(impact)
        self.assertEqual(fixture, outcome.payload)
        target_scope: dict[str, JsonValue] = {"scope_digest": DIGEST_A, "scope_revision": 1}
        target: dict[str, JsonValue] = {"item_id": "target", "position": 0, "scope": target_scope}
        invalid_payloads = (
            history_without(fixture, "evidence"),
            history_replace(fixture, ("unexpected",), None),
            history_replace(fixture, ("source", "scope", "scope_revision"), True),
            history_replace(fixture, ("summary",), None),
            history_replace(fixture, ("impact_id",), ""),
            history_replace(fixture, ("targets", 0, "position"), 1),
            history_replace(fixture, ("targets",), [target, {**target, "position": 1}]),
            history_replace(fixture, ("targets", 0, "scope", "scope_digest"), "not-a-digest"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(DecisionError, "HISTORY_OUTCOME_INVALID"):
                validate_history_outcome(outcome.outcome_schema, payload)

    def test_planning_resolution_history_has_frozen_bytes_and_rejects_malformed_records(self) -> None:
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
        revised = resolve_planning_obligation(
            snapshot,
            impact,
            "target",
            PlanningDisposition.REVISED,
            reason="Updated scope",
            resulting_scope_revision=2,
            resulting_scope_digest=DIGEST_B,
        )
        fixture = (
            b'{"disposition":"revised","evaluated_scope":{"scope_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"scope_revision":1},"impact_id":"impact-1","observed_scope":{"scope_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"scope_revision":1},"outcome_evidence":null,"reason":"Updated scope","replacements":[],"resulting_scope":'
            b'{"scope_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","scope_revision":2},'
            b'"target_item_id":"target"}\n'
        )
        outcome = planning_resolution_outcome(revised, "target")
        self.assertEqual(fixture, outcome.payload)
        superseded = resolve_planning_obligation(
            snapshot,
            impact,
            "target",
            PlanningDisposition.SUPERSEDED,
            reason="Split work",
            replacements=("target-a", "target-b"),
            outcome_evidence="Superseded by smaller items",
        )
        superseded_payload = planning_resolution_outcome(superseded, "target").payload
        invalid_payloads = (
            history_without(fixture, "reason"),
            history_replace(fixture, ("unexpected",), None),
            history_replace(fixture, ("evaluated_scope", "scope_revision"), True),
            history_replace(fixture, ("reason",), None),
            history_replace(fixture, ("target_item_id",), ""),
            history_replace(fixture, ("resulting_scope",), None),
            history_replace(fixture, ("outcome_evidence",), "unexpected"),
            history_replace(fixture, ("replacements",), [{"item_id": "target-a", "position": 0}]),
            history_replace(fixture, ("resulting_scope", "scope_digest"), "not-a-digest"),
            history_replace(fixture, ("resulting_scope",), {"scope_digest": DIGEST_A, "scope_revision": 1}),
            history_replace(superseded_payload, ("replacements", 1, "position"), 2),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(DecisionError, "HISTORY_OUTCOME_INVALID"):
                validate_history_outcome(outcome.outcome_schema, payload)

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
            self.assertIn(
                raised.exception.code,
                {DecisionErrorCode.PLANNING_IMPACT_INVALID, DecisionErrorCode.PLANNING_ACTION_STALE},
            )

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
        for (
            disposition,
            target,
            target_attempt,
            expected_state,
            resulting_revision,
            resulting_digest,
            evidence,
        ) in cases:
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
            with (
                self.subTest(disposition=disposition.value),
                self.assertRaisesRegex(
                    DecisionError,
                    "PLANNING_RESOLUTION_INVALID",
                ),
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
        fixture = (
            b'{"after":{"scope_digest":"15f7f87cb61537942075eddacc25d029ba505c53e23006e6f5d4f4f091f91192","scope_revision":2,'
            b'"semantic":{"artifacts":[{"content_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
            b'"key":"route-design","kind":"design","position":0,"revision":1,"role":"design","selector":"artifacts/designs/route-design/1.md"},'
            b'{"content_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","key":"route-plan","kind":"plan",'
            b'"position":0,"revision":2,"role":"plan","selector":"artifacts/plans/route-plan/2.md"},{"content_sha256":'
            b'"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key":"route-needs","kind":"requirements",'
            b'"position":0,"revision":1,"role":"requirements","selector":"artifacts/requirements/route-needs/1.md"}],"dependencies":'
            b'[{"dependency_id":"survey-west","position":0},{"dependency_id":"survey-east","position":1}],"effect":"Add safe navigable routes",'
            b'"item_id":"build-map","resource_requirements":[{"position":0,"resource_id":"checkout-main"}],"schema":"item-scope/v1",'
            b'"trigger":"A route is missing","unlock":"Reach the next area","user_label":"Build the map","why_it_matters":"The party cannot travel"}},'
            b'"before":{"scope_digest":"c9b3d4f68dc05ffe1a54183d4405109abeefa54704c51e991c94cada57d6c94b","scope_revision":1,'
            b'"semantic":{"artifacts":[{"content_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
            b'"key":"route-design","kind":"design","position":0,"revision":1,"role":"design","selector":"artifacts/designs/route-design/1.md"},'
            b'{"content_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","key":"route-plan","kind":"plan",'
            b'"position":0,"revision":2,"role":"plan","selector":"artifacts/plans/route-plan/2.md"},{"content_sha256":'
            b'"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","key":"route-needs","kind":"requirements",'
            b'"position":0,"revision":1,"role":"requirements","selector":"artifacts/requirements/route-needs/1.md"}],"dependencies":'
            b'[{"dependency_id":"survey-west","position":0},{"dependency_id":"survey-east","position":1}],"effect":"Add navigable routes",'
            b'"item_id":"build-map","resource_requirements":[{"position":0,"resource_id":"checkout-main"}],"schema":"item-scope/v1",'
            b'"trigger":"A route is missing","unlock":"Reach the next area","user_label":"Build the map","why_it_matters":"The party cannot travel"}},'
            b'"item_id":"build-map"}\n'
        )
        self.assertEqual("item-scope-change/v1", outcome.outcome_schema)
        self.assertEqual(fixture, outcome.payload)
        validate_history_outcome(outcome.outcome_schema, outcome.payload)

        with self.assertRaisesRegex(DecisionError, "HISTORY_OUTCOME_INVALID"):
            item_scope_change_outcome(first, replace(second, revision=3))

        decoded = json.loads(fixture)
        before_null = json.loads(fixture)
        before_null["before"] = None
        empty_identity = json.loads(fixture)
        empty_identity["item_id"] = ""
        digest_mismatch = json.loads(fixture)
        digest_mismatch["after"]["scope_digest"] = DIGEST_A
        relational_mismatch = json.loads(fixture)
        relational_mismatch["after"]["scope_revision"] = 3
        invalid_payloads = (
            canonical_history({**decoded, "extra": None}),
            canonical_history({key: value for key, value in decoded.items() if key != "item_id"}),
            fixture.replace(b'"scope_revision":2', b'"scope_revision":true', 1),
            canonical_history(before_null),
            canonical_history(empty_identity),
            canonical_history(digest_mismatch),
            canonical_history(relational_mismatch),
            fixture[:-1],
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
        validate_mutation_resources(
            snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 5),)
        )
        with self.assertRaisesRegex(DecisionError, "RESOURCE_USE_LEASE_STALE"):
            validate_mutation_resources(
                snapshot, "attempt-1", ("checkout",), (ResourceToken("checkout", "host-a", "use-1", 4),)
            )

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
            (
                "later fence",
                replace(
                    snapshot,
                    resource_use_leases=(
                        snapshot.resource_use_leases[0],
                        ResourceUseLease(
                            "use-fence",
                            "reservation-1",
                            "attempt-lease",
                            3,
                            6,
                            UseLeaseState.REVOKED,
                            UseLeaseGenerationKind.FENCE,
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
            resource_reservation_counters=(
                ResourceReservationCounter("instance-a", 0),
                ResourceReservationCounter("instance-b", 0),
            ),
        )
        assignment = assign_resource(
            snapshot,
            reservation_id="reservation-a",
            resource_id="workspace",
            instance_id="instance-a",
            attempt="attempt-1",
            generation=1,
        )
        assigned = assignment.changes[0].after
        self.assertEqual(
            ((ResourceReservationCounter("instance-a", 0), ResourceReservationCounter("instance-a", 1)),),
            tuple((change.before, change.after) for change in assignment.counter_changes),
        )
        with_reservation = replace(
            snapshot,
            resource_reservations=(assigned,),
            resource_reservation_counters=(
                assignment.counter_changes[0].after,
                ResourceReservationCounter("instance-b", 0),
            ),
        )

        self.assertEqual(
            ReservationState.RELEASED, release_resource(with_reservation, "reservation-a").changes[0].after.state
        )
        revoked = revoke_resource(with_reservation, "reservation-a", unresolved_intent=True)
        self.assertEqual(ReservationState.REVOKED_PENDING_RECOVERY, revoked.changes[0].after.state)
        self.assertEqual(assigned.generation, revoked.changes[0].after.generation)
        self.assertEqual(
            ((ResourceReservationCounter("instance-a", 1), ResourceReservationCounter("instance-a", 2)),),
            tuple((change.before, change.after) for change in revoked.counter_changes),
        )
        quarantined_snapshot = replace(with_reservation, resource_reservations=(revoked.changes[0].after,))
        quarantine_conflicts = (
            ("same instance", "instance-a", "attempt-2"),
            ("same attempt requirement", "instance-b", "attempt-1"),
        )
        for name, instance_id, attempt in quarantine_conflicts:
            with self.subTest(quarantine=name), self.assertRaisesRegex(DecisionError, "RESOURCE_INSTANCE_RESERVED"):
                assign_resource(
                    quarantined_snapshot,
                    reservation_id="quarantine-conflict",
                    resource_id="workspace",
                    instance_id=instance_id,
                    attempt=attempt,
                    generation=2 if instance_id == "instance-a" else 1,
                )
        reallocated = reallocate_resource(
            with_reservation,
            "reservation-a",
            replacement_id="reservation-b",
            instance_id="instance-b",
            generation=1,
        )
        self.assertEqual(
            (ReservationState.RELEASED, ReservationState.ACTIVE),
            tuple(change.after.state for change in reallocated.changes),
        )
        self.assertEqual(
            ((ResourceReservationCounter("instance-b", 0), ResourceReservationCounter("instance-b", 1)),),
            tuple((change.before, change.after) for change in reallocated.counter_changes),
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
            resource_reservation_counters=(ResourceReservationCounter("instance-1", 2),),
            resource_reservations=(reservation,),
            resource_use_leases=(
                ResourceUseLease("use-1", "reservation-1", "attempt-lease", 3, 5, UseLeaseState.ACTIVE),
            ),
        )
        token = ResourceToken("checkout", "host-a", "use-1", 5)
        validation_cases = (
            ("duplicate requirement", snapshot, ("checkout", "checkout"), (token,), "RESOURCE_REQUIREMENT_INVALID"),
            ("missing token", snapshot, ("checkout",), (), "RESOURCE_RESERVATION_STALE"),
            (
                "missing reservation",
                replace(snapshot, resource_reservations=()),
                ("checkout",),
                (token,),
                "RESOURCE_RESERVATION_STALE",
            ),
            (
                "wrong host",
                snapshot,
                ("checkout",),
                (replace(token, host_id="host-b"),),
                "RESOURCE_INSTANCE_REQUIRED",
            ),
            (
                "inactive use lease",
                replace(
                    snapshot,
                    resource_use_leases=(replace(snapshot.resource_use_leases[0], state=UseLeaseState.EXPIRED),),
                ),
                ("checkout",),
                (token,),
                "RESOURCE_USE_LEASE_STALE",
            ),
        )
        for name, candidate, requirements, tokens, code in validation_cases:
            with self.subTest(name=name), self.assertRaises(DecisionError) as raised:
                validate_mutation_resources(candidate, "attempt-1", requirements, tokens)
            self.assertEqual(DecisionErrorCode(code), raised.exception.code)

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
                        resource_reservation_counters=(
                            *snapshot.resource_reservation_counters,
                            ResourceReservationCounter("instance-2", 0),
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
            self.assertEqual(DecisionErrorCode(code), raised.exception.code)


if __name__ == "__main__":
    unittest.main()
