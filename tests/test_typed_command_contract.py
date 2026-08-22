import unittest
from dataclasses import replace
from datetime import timedelta
from typing import override

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Role,
    available_actions,
    decide,
    rediscover_action,
)
from charlie_pinboard.domain.errors import DecisionError, DecisionErrorCode
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
    MutationIntentId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ActivateInput,
    ArtifactRecord,
    CanonicalJson,
    CommandAttemptAuthority,
    EmptyInput,
    LedgerSnapshot,
    LegacyActivateInput,
    MutationIntentState,
    ReservationState,
    ResourceIntentCapability,
    SubmitReviewInput,
    UseLeaseGenerationKind,
    UseLeaseState,
    WorkItem,
    WorkState,
)
from charlie_pinboard.domain.resource_decisions import (
    AbandonmentForm,
    AbandonMutationIntentInput,
    AdvanceResourceObservationInput,
    FencedIntentDisposition,
    ObservedResource,
    PreserveResourceStateInput,
    ReconcileInterruptedObservationInput,
    RegisterMutationIntentInput,
    ResolveFencedIntentInput,
    ResolverEvidenceDecision,
    ResourceIntentDecision,
    abandon_mutation_intent,
    advance_resource_observation,
    preserve_resource_state,
    reconcile_interrupted_observation,
    register_mutation_intent,
    resolve_fenced_resource_intent,
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


def _stored_action(snapshot: LedgerSnapshot) -> Action:
    return next(
        candidate
        for candidate in available_actions(snapshot, _worker_actor())
        if candidate.kind == ActionKind.SUBMIT_REVIEW
    )


def _observation(snapshot: LedgerSnapshot, *, digest: str = SQLITE_DIGEST) -> ObservedResource:
    instance = next(value for value in snapshot.resource_instances if value.instance_id == "workspace-on-host")
    locator = next(value for value in snapshot.resource_observations if value.instance_id == instance.instance_id)
    definition = next(value for value in snapshot.resource_definitions if value.resource_id == instance.resource_id)
    return ObservedResource(
        instance.instance_id,
        instance.host_id,
        definition.kind,
        instance.discovery_fingerprint,
        locator.locator_schema,
        locator.locator,
        digest,
        SQLITE_NOW + timedelta(seconds=1),
    )


class TypedTransitionContractTest(unittest.TestCase):
    def test_submit_review_candidate_is_required_typed_and_preserved(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state)
        submit = _stored_action(snapshot)

        with self.assertRaises(DecisionError) as missing:
            decide(snapshot, submit, EmptyInput(), SQLITE_NOW)
        self.assertEqual(DecisionErrorCode.TRANSITION_INPUT_INVALID, missing.exception.code)

        candidate = CandidateId("candidate-that-is-not-a-subject-revision")
        parsed = parse_transition_input("submit-review", b'{"candidate":"candidate-that-is-not-a-subject-revision"}')
        self.assertEqual(SubmitReviewInput(candidate), parsed)
        decision = decide(snapshot, submit, parsed, SQLITE_NOW)
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
        variants = (
            EmptyInput(),
            ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(99)),
            ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(2)),
        )
        for value in variants:
            with self.subTest(value=value), self.assertRaises(DecisionError) as rejected:
                decide(snapshot, activation, value, SQLITE_NOW)
            self.assertEqual(DecisionErrorCode.TRANSITION_INPUT_INVALID, rejected.exception.code)

        accepted = decide(
            snapshot,
            activation,
            ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(1)),
            SQLITE_NOW,
        )
        assert accepted.attempt_change is not None
        self.assertEqual(ArtifactRefId(1), accepted.attempt_change.brief_artifact_ref_id)

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
            with self.subTest(supplied=supplied), self.assertRaises(DecisionError) as stale:
                rediscover_action(snapshot, _worker_actor(), supplied)
            self.assertEqual(DecisionErrorCode.ACTION_NOT_AVAILABLE, stale.exception.code)

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
        state = replace(state, resources=replace(state.resources, mutation_intents=()))
        self.snapshot = project_decision_snapshot(state)
        self.action = _stored_action(self.snapshot)
        authority = self.action.command_authority
        assert authority is not None
        self.authority: CommandAttemptAuthority = authority
        self.capability = self.action.resource_capabilities[0]
        self.recovery_authority = replace(
            self.authority,
            task_id=TaskId("recovering-worker"),
            lease_id=LeaseId("attempt-lease-recovery"),
            generation=self.authority.generation + 1,
        )
        self.registration = register_mutation_intent(
            self.snapshot,
            RegisterMutationIntentInput(
                self.capability,
                MutationIntentId("intent-current"),
                "mutation-policy/v1",
                CanonicalJson(b'{"paths":["src"]}'),
                "policy-digest",
                SQLITE_NOW,
            ),
        )
        self.intent = self.registration.intent_change.after
        self.with_intent = replace(self.snapshot, mutation_intents=(self.intent,))
        self.intent_capability = ResourceIntentCapability(
            self.capability,
            self.intent.intent_id,
            self.intent.policy_digest,
            MutationIntentState.PLANNED,
        )

    def _fenced_snapshot(self) -> LedgerSnapshot:
        recovery = replace(self.with_intent, command_attempt_authorities=(self.recovery_authority,))
        old_use = replace(self.snapshot.mutation_use_leases[-1], state=UseLeaseState.REVOKED)
        fence = replace(
            old_use,
            lease_id=LeaseId("use-fence-current"),
            generation=old_use.generation + 1,
            generation_kind=UseLeaseGenerationKind.FENCE,
        )
        reservation = replace(
            self.snapshot.mutation_reservations[0],
            state=ReservationState.REVOKED_PENDING_RECOVERY,
        )
        counter = replace(self.snapshot.resource_reservation_counters[-1], generation_high_water=2)
        return replace(
            recovery,
            mutation_reservations=(reservation,),
            mutation_use_leases=(old_use, fence),
            resource_reservation_counters=(counter,),
        )

    def test_register_and_advance_require_exact_live_authority_and_evidence(self) -> None:
        self.assertIsNone(self.registration.intent_change.before)
        self.assertEqual(MutationIntentState.PLANNED, self.intent.state)
        with self.assertRaises(DecisionError) as stale:
            register_mutation_intent(
                self.snapshot,
                RegisterMutationIntentInput(
                    replace(
                        self.capability,
                        locator_observation_generation=self.capability.locator_observation_generation + 1,
                    ),
                    MutationIntentId("intent-stale"),
                    "mutation-policy/v1",
                    CanonicalJson(b"{}"),
                    "policy-digest",
                    SQLITE_NOW,
                ),
            )
        self.assertEqual(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, stale.exception.code)

        advanced = advance_resource_observation(
            self.with_intent,
            AdvanceResourceObservationInput(
                self.intent_capability,
                _observation(self.with_intent, digest="after-digest"),
                "change-evidence/v1",
                CanonicalJson(b'{"accepted":true}'),
                "evidence-digest",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(MutationIntentState.ACCEPTED, advanced.intent_change.after.state)
        assert advanced.observation_change is not None
        self.assertEqual(3, advanced.observation_change.after.generation)
        self.assertEqual("after-digest", advanced.observation_change.after.digest)
        self.assertEqual(1, len(advanced.use_lease_changes))

    def test_resource_input_rejection_matrix_is_exact_and_non_mutating(self) -> None:
        registration_values = (
            RegisterMutationIntentInput(
                self.capability,
                MutationIntentId("intent-empty-policy"),
                "",
                CanonicalJson(b""),
                "",
                SQLITE_NOW,
            ),
            RegisterMutationIntentInput(
                self.capability,
                self.intent.intent_id,
                self.intent.policy_schema,
                self.intent.policy,
                self.intent.policy_digest,
                SQLITE_NOW,
            ),
        )
        registration_snapshots = (self.snapshot, self.with_intent)
        expected_registration_codes = (
            DecisionErrorCode.TRANSITION_INPUT_INVALID,
            DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
        )
        for candidate_snapshot, candidate_input, expected_code in zip(
            registration_snapshots,
            registration_values,
            expected_registration_codes,
            strict=True,
        ):
            with self.subTest(candidate_input=candidate_input), self.assertRaises(DecisionError) as rejected:
                register_mutation_intent(candidate_snapshot, candidate_input)
            self.assertEqual(expected_code, rejected.exception.code)

        advance_values = (
            replace(
                AdvanceResourceObservationInput(
                    self.intent_capability,
                    _observation(self.with_intent, digest="changed"),
                    "change-evidence/v1",
                    CanonicalJson(b"{}"),
                    "evidence-digest",
                    ResolverEvidenceDecision.ACCEPTED,
                    SQLITE_NOW + timedelta(seconds=2),
                ),
                resolver_decision=ResolverEvidenceDecision.RECOVERY_REQUIRED,
            ),
            AdvanceResourceObservationInput(
                self.intent_capability,
                _observation(self.with_intent, digest="changed"),
                "",
                CanonicalJson(b""),
                "",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
            AdvanceResourceObservationInput(
                self.intent_capability,
                replace(_observation(self.with_intent, digest="changed"), discovery_fingerprint="other"),
                "change-evidence/v1",
                CanonicalJson(b"{}"),
                "evidence-digest",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
            AdvanceResourceObservationInput(
                replace(self.intent_capability, policy_digest="other-policy"),
                _observation(self.with_intent, digest="changed"),
                "change-evidence/v1",
                CanonicalJson(b"{}"),
                "evidence-digest",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
            AdvanceResourceObservationInput(
                replace(
                    self.intent_capability,
                    resource=replace(
                        self.capability,
                        locator_observation_generation=self.capability.locator_observation_generation + 1,
                    ),
                ),
                _observation(self.with_intent, digest="changed"),
                "change-evidence/v1",
                CanonicalJson(b"{}"),
                "evidence-digest",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        for candidate in advance_values:
            with self.subTest(candidate=candidate), self.assertRaises(DecisionError):
                advance_resource_observation(self.with_intent, candidate)

        stale_state_variants = (
            replace(self.snapshot, mutation_use_leases=()),
            replace(self.snapshot, command_attempt_authorities=()),
            replace(
                self.snapshot,
                mutation_reservations=(
                    replace(self.snapshot.mutation_reservations[0], state=ReservationState.REVOKED),
                ),
            ),
            replace(
                self.snapshot,
                mutation_use_leases=(replace(self.snapshot.mutation_use_leases[-1], expires_at=SQLITE_NOW),),
            ),
            replace(self.snapshot, host_epoch=self.snapshot.host_epoch + 1),
        )
        for candidate_snapshot in stale_state_variants:
            with self.subTest(candidate_snapshot=candidate_snapshot), self.assertRaises(DecisionError):
                register_mutation_intent(
                    candidate_snapshot,
                    RegisterMutationIntentInput(
                        self.capability,
                        MutationIntentId("intent-stale-boundary"),
                        "mutation-policy/v1",
                        CanonicalJson(b"{}"),
                        "policy-digest",
                        SQLITE_NOW,
                    ),
                )

    def test_abandonment_and_interruption_reconciliation_are_policy_neutral(self) -> None:
        abandoned = abandon_mutation_intent(
            self.with_intent,
            AbandonMutationIntentInput(
                self.intent_capability,
                self.authority,
                _observation(self.with_intent),
                AbandonmentForm.LIVE_OWNER,
                "No mutation began.",
                SQLITE_NOW + timedelta(seconds=1),
            ),
        )
        self.assertEqual(MutationIntentState.ABANDONED, abandoned.intent_change.after.state)

        interrupted_use = replace(
            self.snapshot.mutation_use_leases[-1],
            state=UseLeaseState.EXPIRED,
        )
        interrupted = replace(
            self.with_intent,
            command_attempt_authorities=(self.recovery_authority,),
            mutation_use_leases=(interrupted_use,),
        )
        clean = abandon_mutation_intent(
            interrupted,
            AbandonMutationIntentInput(
                self.intent_capability,
                self.recovery_authority,
                _observation(interrupted),
                AbandonmentForm.CLEAN_INTERRUPTION,
                "The interrupted dispatch made no changes.",
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(MutationIntentState.ABANDONED, clean.intent_change.after.state)

        starting_observation = next(
            candidate
            for candidate in interrupted.resource_observations
            if candidate.instance_id == self.capability.instance_id
        )
        drifted_observation = replace(
            starting_observation,
            generation=starting_observation.generation + 1,
            digest="drifted-start",
        )
        recovery_variants = (
            (
                "observation-drift",
                replace(
                    interrupted,
                    resource_observations=tuple(
                        drifted_observation if candidate.instance_id == drifted_observation.instance_id else candidate
                        for candidate in interrupted.resource_observations
                    ),
                ),
                "drifted-start",
                DecisionErrorCode.RESOURCE_USE_LEASE_STALE,
            ),
            (
                "reservation-revoked-pending-recovery",
                replace(
                    interrupted,
                    mutation_reservations=(
                        replace(
                            interrupted.mutation_reservations[0],
                            state=ReservationState.REVOKED_PENDING_RECOVERY,
                        ),
                    ),
                ),
                SQLITE_DIGEST,
                DecisionErrorCode.RESOURCE_RESERVATION_STALE,
            ),
        )
        for name, candidate_snapshot, current_digest, expected_code in recovery_variants:
            with (
                self.subTest(name=name, operation="clean-abandon"),
                self.assertRaises(DecisionError) as rejected,
            ):
                abandon_mutation_intent(
                    candidate_snapshot,
                    AbandonMutationIntentInput(
                        self.intent_capability,
                        self.recovery_authority,
                        _observation(candidate_snapshot, digest=current_digest),
                        AbandonmentForm.CLEAN_INTERRUPTION,
                        "The interrupted dispatch made no changes.",
                        SQLITE_NOW + timedelta(seconds=2),
                    ),
                )
            self.assertEqual(expected_code, rejected.exception.code)
            with (
                self.subTest(name=name, operation="reconcile"),
                self.assertRaises(DecisionError) as rejected,
            ):
                reconcile_interrupted_observation(
                    candidate_snapshot,
                    ReconcileInterruptedObservationInput(
                        self.intent_capability,
                        self.recovery_authority,
                        _observation(candidate_snapshot, digest="interrupted-output"),
                        "change-evidence/v1",
                        CanonicalJson(b"{}"),
                        "evidence-digest",
                        ResolverEvidenceDecision.ACCEPTED,
                        SQLITE_NOW + timedelta(seconds=2),
                    ),
                )
            self.assertEqual(expected_code, rejected.exception.code)

        with self.assertRaises(DecisionError) as unsupported:
            reconcile_interrupted_observation(
                interrupted,
                ReconcileInterruptedObservationInput(
                    self.intent_capability,
                    self.recovery_authority,
                    _observation(interrupted, digest="interrupted-output"),
                    "change-evidence/v1",
                    CanonicalJson(b"{}"),
                    "evidence-digest",
                    ResolverEvidenceDecision.POST_INTERRUPTION_PROOF_UNSUPPORTED,
                    SQLITE_NOW + timedelta(seconds=2),
                ),
            )
        self.assertEqual(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, unsupported.exception.code)

        reconciled = reconcile_interrupted_observation(
            interrupted,
            ReconcileInterruptedObservationInput(
                self.intent_capability,
                self.recovery_authority,
                _observation(interrupted, digest="interrupted-output"),
                "change-evidence/v1",
                CanonicalJson(b"{}"),
                "evidence-digest",
                ResolverEvidenceDecision.ACCEPTED,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(MutationIntentState.RECONCILED, reconciled.intent_change.after.state)

    def test_explicit_preserve_and_fenced_resolution_never_recreate_authority(self) -> None:
        coordination = self.snapshot.coordination_authority
        assert coordination is not None
        recovery = replace(self.with_intent, command_attempt_authorities=(self.recovery_authority,))
        preserved = preserve_resource_state(
            recovery,
            PreserveResourceStateInput(
                self.intent_capability,
                coordination,
                self.recovery_authority,
                _observation(recovery, digest="human-kept-output"),
                LeaseId("use-fence-new"),
                "Keep the inspected changes.",
                None,
                None,
                None,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(MutationIntentState.HUMAN_PRESERVED, preserved.intent_change.after.state)
        self.assertEqual(2, len(preserved.use_lease_changes))
        self.assertTrue(all(change.after.state.value == "revoked" for change in preserved.use_lease_changes))

        for use_state in (UseLeaseState.EXPIRED, UseLeaseState.RELEASED):
            interrupted_use = replace(self.snapshot.mutation_use_leases[-1], state=use_state)
            interrupted = replace(recovery, mutation_use_leases=(interrupted_use,))
            with self.subTest(use_state=use_state):
                preserved_interruption = preserve_resource_state(
                    interrupted,
                    PreserveResourceStateInput(
                        self.intent_capability,
                        coordination,
                        self.recovery_authority,
                        _observation(interrupted, digest="human-kept-output"),
                        LeaseId(f"use-fence-{use_state.value}"),
                        "Keep the inspected changes.",
                        None,
                        None,
                        None,
                        SQLITE_NOW + timedelta(seconds=2),
                    ),
                )
                self.assertEqual(1, len(preserved_interruption.use_lease_changes))
                fence_change = preserved_interruption.use_lease_changes[0]
                self.assertIsNone(fence_change.before)
                self.assertEqual(interrupted_use.generation + 1, fence_change.after.generation)
                self.assertEqual(UseLeaseGenerationKind.FENCE, fence_change.after.generation_kind)
                self.assertEqual(UseLeaseState.REVOKED, fence_change.after.state)

        fenced = self._fenced_snapshot()
        resolved = resolve_fenced_resource_intent(
            fenced,
            ResolveFencedIntentInput(
                self.intent_capability,
                coordination,
                self.recovery_authority,
                _observation(fenced),
                2,
                FencedIntentDisposition.UNCHANGED,
                "The fenced dispatch was unused.",
                None,
                None,
                None,
                None,
                SQLITE_NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(MutationIntentState.ABANDONED, resolved.intent_change.after.state)
        assert resolved.reservation_change is not None
        self.assertEqual("revoked", resolved.reservation_change.after.state.value)
        self.assertEqual((), resolved.use_lease_changes)

    def test_changed_fenced_state_uses_only_supported_reconcile_or_human_preserve(self) -> None:
        fenced = self._fenced_snapshot()
        coordination = fenced.coordination_authority
        assert coordination is not None
        changed = _observation(fenced, digest="fenced-output")

        def resolve(
            disposition: FencedIntentDisposition,
            resolver_decision: ResolverEvidenceDecision | None,
            *,
            reason: str = "Preserve the inspected output.",
            evidence: bool = True,
        ) -> ResourceIntentDecision:
            return resolve_fenced_resource_intent(
                fenced,
                ResolveFencedIntentInput(
                    self.intent_capability,
                    coordination,
                    self.recovery_authority,
                    changed,
                    2,
                    disposition,
                    reason,
                    "change-evidence/v1" if evidence else None,
                    CanonicalJson(b"{}") if evidence else None,
                    "evidence-digest" if evidence else None,
                    resolver_decision,
                    SQLITE_NOW + timedelta(seconds=2),
                ),
            )

        reconciled = resolve(FencedIntentDisposition.RECONCILE, ResolverEvidenceDecision.ACCEPTED)
        preserved = resolve(FencedIntentDisposition.HUMAN_PRESERVE, None, evidence=False)
        self.assertEqual(MutationIntentState.RECONCILED, reconciled.intent_change.after.state)
        self.assertEqual(MutationIntentState.HUMAN_PRESERVED, preserved.intent_change.after.state)
        self.assertEqual((), reconciled.use_lease_changes)
        self.assertEqual((), preserved.use_lease_changes)

        invalid = (
            (FencedIntentDisposition.RECONCILE, ResolverEvidenceDecision.RECOVERY_REQUIRED, True, "reason"),
            (FencedIntentDisposition.RECONCILE, ResolverEvidenceDecision.ACCEPTED, False, "reason"),
            (FencedIntentDisposition.HUMAN_PRESERVE, None, False, ""),
            (FencedIntentDisposition.UNCHANGED, None, False, "reason"),
        )
        for disposition, resolver_decision, evidence, reason in invalid:
            with self.subTest(disposition=disposition), self.assertRaises(DecisionError):
                resolve(disposition, resolver_decision, evidence=evidence, reason=reason)

        with self.assertRaises(DecisionError):
            preserve_resource_state(
                replace(fenced, coordination_authority=coordination),
                PreserveResourceStateInput(
                    self.intent_capability,
                    replace(coordination, generation=coordination.generation + 1),
                    self.recovery_authority,
                    changed,
                    LeaseId("new-fence"),
                    "Preserve.",
                    None,
                    None,
                    None,
                    SQLITE_NOW + timedelta(seconds=2),
                ),
            )


if __name__ == "__main__":
    unittest.main()
