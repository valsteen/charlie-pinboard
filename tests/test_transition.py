import json
import tempfile
import unittest
from copy import replace
from pathlib import Path

from repo_work.actions import actions_for
from repo_work.leases import (
    LeaseError,
    acquire_attempt,
    acquire_coordination,
    renew_attempt,
    require_attempt,
    revoke_attempt,
    revoke_coordination,
)
from repo_work.markdown import parse_attempt, parse_current, parse_header, parse_item, parse_queue
from repo_work.migration import migrate_to_v2
from repo_work.model import WorkState
from repo_work.resources import ResourceError, claim_resource, declare_resource, renew_resource, require_resource
from repo_work.revisions import subject_revision
from repo_work.transition import TransitionError
from repo_work.validate import validate_work_state

from .support import JsonObject, JsonValue, apply_action, create_proposal, create_state


def proposal(proposal_id: str = "finding-1") -> JsonObject:
    return {
        "schema": "repo-work/v1",
        "proposal_id": proposal_id,
        "created_at": "2026-08-16T12:30:00Z",
        "source_task_id": "investigation-task",
        "user_label": "Reveal ownership",
        "trigger": "A generic operation has a feature-specific owner.",
        "evidence": ["client/src/mappings.ts#reveal"],
        "why_it_matters": "The owner widens changes.",
        "relation": {"kind": "independent", "item": None},
        "effect": "One provider owns Reveal.",
        "unlock": "Two consumers share it.",
        "urgency_evidence": "Current objective.",
        "freshness_assumptions": ["Ownership is unchanged."],
    }


class TransitionTest(unittest.TestCase):
    def snapshot(self, work: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(work)): path.read_bytes()
            for path in work.rglob("*")
            if path.is_file() and path.name != ".transition.lock"
        }

    def test_activate_updates_ledger_pointer_and_attempt(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "activate:reveal-core"
        )

        apply_action(
            work,
            project,
            action,
            {
                "attempt": "reveal-core-1",
                "branch": "codex/reveal-core",
                "base_revision": "abc123",
                "owner": "worker-task",
            },
        )

        self.assertEqual(WorkState.ACTIVE, parse_queue(work / "queue.md").items[0].state)
        self.assertEqual("reveal-core", parse_current(work / "current.md").focus_item)
        self.assertTrue((work / "attempts" / "reveal-core-1" / "attempt.md").is_file())
        self.assertTrue(validate_work_state(work, project).valid)

    def test_transition_writer_rejects_an_outside_attempts_symlink(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = next(
            candidate
            for candidate in actions_for(work, project, role="coordinator")
            if candidate.action_id == "activate:reveal-core"
        )
        outside = Path(tempfile.mkdtemp()) / "outside-attempts"
        (work / "attempts").replace(outside)
        (work / "attempts").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(TransitionError, "CHANGE_PATH_INVALID"):
            apply_action(
                work,
                project,
                action,
                {
                    "attempt": "reveal-core-1",
                    "branch": "codex/reveal-core",
                    "base_revision": "abc123",
                    "owner": "worker-task",
                },
            )

        self.assertEqual([], list(outside.iterdir()))

    def test_v2_coordination_revocation_fences_an_issued_graph_action(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        migrate_to_v2(work, project)
        lease = acquire_coordination(work, "task-a", "host", 60)
        action = next(
            candidate
            for candidate in actions_for(
                work, project, "coordinator", lease_id=lease.lease_id, generation=lease.generation
            )
            if candidate.action_id == "activate:reveal-core"
        )
        revoke_coordination(work)

        with self.assertRaisesRegex(TransitionError, "LEASE_FENCED"):
            apply_action(
                work,
                project,
                action,
                {"attempt": "reveal-core-1", "branch": "codex/reveal", "base_revision": "abc", "owner": "task-a"},
            )

    def test_v2_public_coordination_strings_round_trip_without_invalidating_authority(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        migrate_to_v2(work, project)

        lease = acquire_coordination(work, "true", "false", 60, lease_id="null")

        self.assertEqual(("true", "false", "null"), (lease.task_id, lease.host_id, lease.lease_id))
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_public_proposal_activation_and_current_strings_round_trip(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        migrate_to_v2(work, project)
        coordination = acquire_coordination(work, "coordinator", "host", 300)
        create_proposal(work, project, proposal())
        accept = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "accept-proposal:finding-1"
        )

        apply_action(
            work,
            project,
            accept,
            {"item": "true", "state": "ready", "next_action": "~", "depends_on": []},
        )

        root = work / "v2"
        accepted = parse_item(root / "items" / "true.md")
        self.assertEqual("true", accepted.item)
        self.assertIsNotNone(accepted.queue_item)
        if accepted.queue_item is None:
            self.fail("Accepted schema-v2 item has no queue fields.")
        self.assertEqual("~", accepted.queue_item.next_action)
        self.assertTrue(validate_work_state(work, project).valid)

        activate = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "activate:true"
        )
        apply_action(
            work,
            project,
            activate,
            {"attempt": "true", "branch": "true", "base_revision": "false", "owner": "null"},
        )

        current = parse_current(root / "current.md")
        attempt = parse_attempt(root / "attempts" / "true" / "attempt.md")
        self.assertEqual(("true", "true"), (current.focus_item, current.focus_attempt))
        self.assertEqual(("true", "false", "null"), (attempt.branch, attempt.base_revision, attempt.provenance))
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_public_queue_workflows_preserve_null_and_none_as_data(self) -> None:
        project, work = create_state(
            [
                "| activation-target-null | ready | — | — | — | design | activate | Ready. |",
                "| activation-target-none | ready | — | — | — | design | activate | Ready. |",
            ]
        )
        migrate_to_v2(work, project)
        coordination = acquire_coordination(work, "coordinator", "host", 300)

        def accept_proposal(
            proposal_id: str,
            item_id: str,
            next_action: str,
            depends_on: list[JsonValue],
        ) -> None:
            create_proposal(work, project, proposal(proposal_id))
            action = next(
                candidate
                for candidate in actions_for(
                    work,
                    project,
                    "coordinator",
                    lease_id=coordination.lease_id,
                    generation=coordination.generation,
                )
                if candidate.action_id == f"accept-proposal:{proposal_id}"
            )
            apply_action(
                work,
                project,
                action,
                {
                    "item": item_id,
                    "state": "blocked" if depends_on else "ready",
                    "next_action": next_action,
                    "depends_on": depends_on,
                },
            )
            self.assertTrue(validate_work_state(work, project).valid)

        accept_proposal("finding-null", "null", "none", [])
        accept_proposal("finding-none", "none", "null", [])
        accept_proposal("finding-dependent", "dependent", "null", ["null", "none"])

        root = work / "v2"
        queue = parse_queue(root / "queue.md").by_id()
        self.assertEqual("none", queue["null"].next_action)
        self.assertEqual("null", queue["none"].next_action)
        self.assertEqual(("null", "none"), queue["dependent"].depends_on)
        self.assertEqual("null", queue["dependent"].next_action)

        def activate_target(item_id: str, attempt_id: str) -> None:
            activate = next(
                candidate
                for candidate in actions_for(
                    work,
                    project,
                    "coordinator",
                    lease_id=coordination.lease_id,
                    generation=coordination.generation,
                )
                if candidate.action_id == f"activate:{item_id}"
            )
            apply_action(
                work,
                project,
                activate,
                {"attempt": attempt_id, "branch": "null", "base_revision": "none", "owner": "none"},
            )
            self.assertTrue(validate_work_state(work, project).valid)

        activate_target("activation-target-null", "null")
        activate_target("activation-target-none", "none")

        queue = parse_queue(root / "queue.md").by_id()
        current = parse_current(root / "current.md")
        null_attempt = parse_attempt(root / "attempts" / "null" / "attempt.md")
        none_attempt = parse_attempt(root / "attempts" / "none" / "attempt.md")
        self.assertEqual("null", queue["activation-target-null"].attempt)
        self.assertEqual("none", queue["activation-target-none"].attempt)
        self.assertEqual(("activation-target-none", "none"), (current.focus_item, current.focus_attempt))
        self.assertEqual(
            (("null", "none", "none"), ("null", "none", "none")),
            (
                (null_attempt.branch, null_attempt.base_revision, null_attempt.provenance),
                (none_attempt.branch, none_attempt.base_revision, none_attempt.provenance),
            ),
        )
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_attempt_action_survives_unrelated_edit_but_not_revocation(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                "| unrelated | ready | — | — | — | design | activate | Ready. |",
            ],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project)
        root = work / "v2"
        lease = acquire_attempt(work, "reveal-core-1", "task-a", "host", 60)
        action = next(
            candidate
            for candidate in actions_for(work, project, "worker", lease_id=lease.lease_id, generation=lease.generation)
            if candidate.action_id == "submit-review:reveal-core-1"
        )
        unrelated = root / "items" / "unrelated.md"
        unrelated.write_text(unrelated.read_text(encoding="utf-8") + "\nUnrelated note.\n", encoding="utf-8")

        apply_action(work, project, action, {})

        self.assertEqual(WorkState.REVIEW, parse_queue(root / "queue.md").by_id()["reveal-core"].state)
        self.assertTrue(validate_work_state(work, project).valid)

        coordination = acquire_coordination(work, "reviewer", "host", 60)
        revoke_attempt(work, "reveal-core-1", coordination.lease_id, coordination.generation)
        with self.assertRaisesRegex(TransitionError, "LEASE_FENCED"):
            apply_action(work, project, action, {})

    def test_resource_sensitive_action_carries_and_revalidates_exact_claim_tokens(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project)
        root = work / "v2"
        coordination = acquire_coordination(work, "coordinator", "host", 300)
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
        )
        attempt = acquire_attempt(work, "reveal-core-1", "worker", "host", 300)
        claim = claim_resource(
            work,
            "bitwig-live",
            "reveal-core-1",
            "worker",
            "host",
            300,
            attempt.lease_id,
            attempt.generation,
        )
        item_path = root / "items" / "reveal-core.md"
        item_path.write_text(
            item_path.read_text(encoding="utf-8").replace("resources: —", "resources: bitwig-live"),
            encoding="utf-8",
        )
        action = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "worker",
                lease_id=attempt.lease_id,
                generation=attempt.generation,
            )
            if candidate.action_id == "submit-review:reveal-core-1"
        )
        self.assertEqual(
            [("bitwig-live", "host", claim.lease_id, claim.generation)],
            [(token.resource_id, token.host_id, token.lease_id, token.generation) for token in action.resource_claims],
        )
        stale = replace(action, resource_claims=(replace(action.resource_claims[0], generation=999),))
        with self.assertRaisesRegex(TransitionError, "LEASE_FENCED"):
            apply_action(work, project, stale, {})
        self.assertEqual(WorkState.ACTIVE, parse_queue(root / "queue.md").items[0].state)

        apply_action(work, project, action, {})
        self.assertEqual(WorkState.REVIEW, parse_queue(root / "queue.md").items[0].state)

    def test_reserved_resource_is_fresh_and_exactly_authorized(self) -> None:
        for resource_id in ("null", "none"):
            with self.subTest(resource_id=resource_id):
                project, work = create_state(
                    ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
                    focus_item="reveal-core",
                    focus_attempt="reveal-core-1",
                    create_active_attempt=True,
                )
                migrate_to_v2(work, project)
                root = work / "v2"
                coordination = acquire_coordination(work, "coordinator", "host", 300)
                declare_resource(
                    work,
                    resource_id,
                    "Reserved resource",
                    coordination.lease_id,
                    coordination.generation,
                    scope="host-local",
                )
                self.assertTrue(validate_work_state(work, project).valid)
                attempt = acquire_attempt(work, "reveal-core-1", "worker", "host", 300)
                self.assertTrue(validate_work_state(work, project).valid)
                claim = claim_resource(
                    work,
                    resource_id,
                    "reveal-core-1",
                    "worker",
                    "host",
                    300,
                    attempt.lease_id,
                    attempt.generation,
                )
                self.assertTrue(validate_work_state(work, project).valid)
                item_path = root / "items" / "reveal-core.md"
                item_path.write_text(
                    item_path.read_text(encoding="utf-8").replace("resources: —", f'resources: "{resource_id}"'),
                    encoding="utf-8",
                )
                self.assertTrue(validate_work_state(work, project).valid)
                action = next(
                    candidate
                    for candidate in actions_for(
                        work,
                        project,
                        "worker",
                        lease_id=attempt.lease_id,
                        generation=attempt.generation,
                    )
                    if candidate.action_id == "submit-review:reveal-core-1"
                )
                self.assertEqual(
                    [(resource_id, "host", claim.lease_id, claim.generation)],
                    [
                        (token.resource_id, token.host_id, token.lease_id, token.generation)
                        for token in action.resource_claims
                    ],
                )

                substitute = "none" if resource_id == "null" else "null"
                tampered_actions = (
                    replace(action, resource_claims=()),
                    replace(action, resource_claims=(replace(action.resource_claims[0], resource_id=substitute),)),
                    replace(action, resource_claims=(replace(action.resource_claims[0], generation=999),)),
                )
                for tampered in tampered_actions:
                    with self.subTest(resource_claims=tampered.resource_claims):
                        before = self.snapshot(root)
                        with self.assertRaisesRegex(
                            TransitionError,
                            "RESOURCE_CLAIM_REQUIRED|ACTION_NOT_AVAILABLE|LEASE_FENCED",
                        ):
                            apply_action(work, project, tampered, {})
                        self.assertEqual(before, self.snapshot(root))

                before_revision = subject_revision(root, "reveal-core")
                resource_path = root / "resources" / f"{resource_id}.md"
                resource_path.write_text(
                    resource_path.read_text(encoding="utf-8") + "\nChanged declaration.\n", encoding="utf-8"
                )
                self.assertNotEqual(before_revision, subject_revision(root, "reveal-core"))
                before = self.snapshot(root)
                with self.assertRaisesRegex(TransitionError, "SUBJECT_REVISION_STALE|ACTION_NOT_AVAILABLE"):
                    apply_action(work, project, replace(action, resource_claims=()), {})
                self.assertEqual(before, self.snapshot(root))

    def test_two_disjoint_attempts_can_be_active_and_one_can_pause_independently(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | ready | — | — | — | design | activate | Ready. |",
                "| mapping-create | ready | — | — | — | design | activate | Ready. |",
            ]
        )
        payloads: dict[str, JsonObject] = {
            "reveal-core": {
                "attempt": "reveal-core-1",
                "branch": "codex/reveal-core",
                "base_revision": "abc123",
                "owner": "worker-one",
            },
            "mapping-create": {
                "attempt": "mapping-create-1",
                "branch": "codex/mapping-create",
                "base_revision": "abc123",
                "owner": "worker-two",
            },
        }
        for item in ("reveal-core", "mapping-create"):
            action = next(
                candidate
                for candidate in actions_for(work, project, role="coordinator")
                if candidate.action_id == f"activate:{item}"
            )
            apply_action(work, project, action, payloads[item])

        active = [item for item in parse_queue(work / "queue.md").items if item.state == WorkState.ACTIVE]
        self.assertEqual({"reveal-core", "mapping-create"}, {item.item for item in active})
        self.assertEqual("mapping-create-1", parse_current(work / "current.md").focus_attempt)
        self.assertTrue(validate_work_state(work, project).valid)

        pause = next(
            candidate
            for candidate in actions_for(work, project, role="coordinator")
            if candidate.action_id == "pause:reveal-core-1"
        )
        apply_action(work, project, pause, {"reason": "A prerequisite needs attention."})

        self.assertEqual("mapping-create-1", parse_current(work / "current.md").focus_attempt)
        self.assertTrue(validate_work_state(work, project).valid)

    def test_stale_action_changes_no_state(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = actions_for(work, project, role="coordinator")[0]
        before = self.snapshot(work)
        (work / "current.md").write_text(
            (work / "current.md").read_text(encoding="utf-8") + "\nChanged elsewhere.\n",
            encoding="utf-8",
        )
        changed = self.snapshot(work)

        with self.assertRaisesRegex(TransitionError, "STATE_REVISION_STALE"):
            apply_action(
                work,
                project,
                action,
                {
                    "attempt": "reveal-core-1",
                    "branch": "codex/reveal-core",
                    "base_revision": "abc123",
                    "owner": "worker-task",
                },
            )

        self.assertNotEqual(before, changed)
        self.assertEqual(changed, self.snapshot(work))

    def test_wrong_coordinator_generation_changes_no_state(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = actions_for(work, project, role="coordinator")[0]
        action = replace(action, coordinator_generation=2)
        before = self.snapshot(work)

        with self.assertRaisesRegex(TransitionError, "COORDINATOR_OWNERSHIP_CONFLICT"):
            apply_action(work, project, action, {})

        self.assertEqual(before, self.snapshot(work))

    def test_pause_preserves_attempt_and_clears_active_pointer(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "pause:reveal-core-1"
        )

        apply_action(work, project, action, {"reason": "A prerequisite needs attention."})

        self.assertEqual(WorkState.PAUSED, parse_queue(work / "queue.md").items[0].state)
        self.assertIsNone(parse_current(work / "current.md").focus_item)
        self.assertIn(
            "state: paused",
            (work / "attempts" / "reveal-core-1" / "attempt.md").read_text(encoding="utf-8"),
        )
        self.assertTrue(validate_work_state(work, project).valid)

    def test_complete_removes_live_item_and_preserves_history(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "complete:reveal-core-1"
        )

        apply_action(work, project, action, {"evidence": "accepted review"})

        self.assertEqual((), parse_queue(work / "queue.md").items)
        self.assertFalse((work / "items" / "reveal-core.md").exists())
        self.assertTrue((work / "history" / "items" / "reveal-core.md").is_file())
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_close_done_fences_a_paused_attempt_and_satisfies_a_dependent(self) -> None:
        project, work = create_state(
            [
                "| prerequisite | active | — | — | prerequisite-1 | design | continue | Active. |",
                "| dependent | blocked | — | prerequisite | — | design | none | Waiting. |",
            ],
            focus_item="prerequisite",
            focus_attempt="prerequisite-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project)
        root = work / "v2"
        attempt = acquire_attempt(work, "prerequisite-1", "worker", "host", 300)
        coordination = acquire_coordination(work, "coordinator", "host", 300)
        pause = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "pause:prerequisite-1"
        )
        apply_action(work, project, pause, {"reason": "The user is making a terminal decision."})
        close = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "close:prerequisite"
        )

        apply_action(work, project, close, {"outcome": "done", "reason": "Decision complete."})

        self.assertEqual("done", parse_header(root / "history" / "items" / "prerequisite.md")["state"])
        attempt_header = parse_header(root / "attempts" / "prerequisite-1" / "attempt.md")
        self.assertEqual("done", attempt_header["state"])
        self.assertEqual("revoked", attempt_header["lease_status"])
        self.assertEqual(str(attempt.generation + 1), attempt_header["lease_generation"])
        actions = actions_for(
            work,
            project,
            "coordinator",
            lease_id=coordination.lease_id,
            generation=coordination.generation,
        )
        self.assertIn("resume:dependent", {candidate.action_id for candidate in actions})
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_completion_records_done_from_active_or_review_and_unblocks_dependents(self) -> None:
        for submit_review in (False, True):
            with self.subTest(submit_review=submit_review):
                project, work = create_state(
                    [
                        "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                        "| dependent | blocked | — | reveal-core | — | design | none | Waiting. |",
                    ],
                    focus_item="reveal-core",
                    focus_attempt="reveal-core-1",
                    create_active_attempt=True,
                )
                migrate_to_v2(work, project)
                root = work / "v2"
                if submit_review:
                    attempt_lease = acquire_attempt(work, "reveal-core-1", "worker", "host", 60)
                    submit = next(
                        candidate
                        for candidate in actions_for(
                            work,
                            project,
                            "worker",
                            lease_id=attempt_lease.lease_id,
                            generation=attempt_lease.generation,
                        )
                        if candidate.action_id == "submit-review:reveal-core-1"
                    )
                    apply_action(work, project, submit, {})

                coordination = acquire_coordination(work, "reviewer", "host", 60)
                complete = next(
                    candidate
                    for candidate in actions_for(
                        work,
                        project,
                        "coordinator",
                        lease_id=coordination.lease_id,
                        generation=coordination.generation,
                    )
                    if candidate.action_id == "complete:reveal-core-1"
                )
                apply_action(work, project, complete, {"evidence": "accepted review"})

                history = root / "history" / "items" / "reveal-core.md"
                self.assertEqual("done", parse_header(history)["state"])
                self.assertFalse((root / "items" / "reveal-core.md").exists())
                self.assertIn(
                    "resume:dependent",
                    {
                        candidate.action_id
                        for candidate in actions_for(
                            work,
                            project,
                            "coordinator",
                            lease_id=coordination.lease_id,
                            generation=coordination.generation,
                        )
                    },
                )
                self.assertTrue(validate_work_state(work, project).valid)

    def test_v2_completion_fences_attempt_and_resource_leases_and_allows_a_live_replacement(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                "| next-live | ready | — | — | — | design | activate | Ready. |",
            ],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project)
        root = work / "v2"
        coordination = acquire_coordination(work, "coordinator", "host", 300)
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
        )
        activate = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "activate:next-live"
        )
        apply_action(
            work,
            project,
            activate,
            {"attempt": "next-live-1", "branch": "codex/next-live", "base_revision": "abc", "owner": "next"},
        )
        completed_attempt = acquire_attempt(work, "reveal-core-1", "worker", "host", 300)
        completed_claim = claim_resource(
            work,
            "bitwig-live",
            "reveal-core-1",
            "worker",
            "host",
            300,
            completed_attempt.lease_id,
            completed_attempt.generation,
        )
        complete = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "complete:reveal-core-1"
        )

        apply_action(work, project, complete, {"evidence": "accepted review"})

        completed_header = parse_header(root / "attempts" / "reveal-core-1" / "attempt.md")
        self.assertEqual("done", completed_header["state"])
        self.assertEqual("revoked", completed_header["lease_status"])
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            require_attempt(
                root,
                "reveal-core-1",
                completed_attempt.lease_id,
                completed_attempt.generation,
            )
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            renew_attempt(
                work,
                "reveal-core-1",
                completed_attempt.lease_id,
                completed_attempt.generation,
                300,
            )
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_LEASE_REQUIRED"):
            acquire_attempt(work, "reveal-core-1", "stale-worker", "host", 300)
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            require_resource(
                root,
                "bitwig-live",
                "host",
                completed_claim.lease_id,
                completed_claim.generation,
            )
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            renew_resource(
                work,
                "bitwig-live",
                "host",
                completed_claim.lease_id,
                completed_claim.generation,
                300,
            )

        replacement_attempt = acquire_attempt(work, "next-live-1", "next-worker", "host", 300)
        replacement_claim = claim_resource(
            work,
            "bitwig-live",
            "next-live-1",
            "next-worker",
            "host",
            300,
            replacement_attempt.lease_id,
            replacement_attempt.generation,
        )
        self.assertEqual("next-live-1", replacement_claim.attempt_id)
        self.assertGreater(replacement_claim.generation, completed_claim.generation)

    def test_resume_reactivates_preserved_attempt(self) -> None:
        project, work = create_state(["| reveal-core | paused | — | — | reveal-core-1 | design | resume | Paused. |"])
        attempt_dir = work / "attempts" / "reveal-core-1"
        attempt_dir.mkdir()
        attempt_path = attempt_dir / "attempt.md"
        attempt_path.write_text(
            """---
kind: work-attempt
schema: repo-work/v1
attempt: reveal-core-1
item: reveal-core
state: paused
branch: codex/reveal-core
base_revision: abc123
owner: worker-task
updated: "2026-08-16"
---
""",
            encoding="utf-8",
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "resume:reveal-core"
        )

        apply_action(work, project, action, {})

        self.assertEqual(WorkState.ACTIVE, parse_queue(work / "queue.md").items[0].state)
        self.assertEqual("reveal-core-1", parse_current(work / "current.md").focus_attempt)
        self.assertIn("state: active", attempt_path.read_text(encoding="utf-8"))
        self.assertTrue(validate_work_state(work, project).valid)

    def test_reopen_returns_deferred_item_to_intake(self) -> None:
        project, work = create_state(
            ["| optional-check | deferred | safe-to-defer | — | — | finding | none | Reopen on evidence. |"]
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "reopen:optional-check"
        )

        apply_action(work, project, action, {"evidence": "The recorded failure recurred."})

        item = parse_queue(work / "queue.md").items[0]
        self.assertEqual(WorkState.INTAKE, item.state)
        self.assertIsNone(item.timing)
        self.assertTrue(validate_work_state(work, project).valid)

    def test_mark_ready_admits_intake_for_selection(self) -> None:
        project, work = create_state(
            ["| reveal-core | intake | — | — | — | proposal:finding-1 | review-intake | Review. |"]
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "mark-ready:reveal-core"
        )

        apply_action(work, project, action, {"reason": "Evidence and scope are sufficient."})

        item = parse_queue(work / "queue.md").items[0]
        self.assertEqual(WorkState.READY, item.state)
        self.assertEqual("activate", item.next_action)
        self.assertTrue(validate_work_state(work, project).valid)

    def test_block_active_attempt_records_dependencies_and_reason(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                "| prerequisite | ready | — | — | — | finding | activate | Ready. |",
            ],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "block:reveal-core-1"
        )

        apply_action(work, project, action, {"reason": "Needs foundation.", "depends_on": ["prerequisite"]})

        item = parse_queue(work / "queue.md").by_id()["reveal-core"]
        self.assertEqual(WorkState.BLOCKED, item.state)
        self.assertEqual(("prerequisite",), item.depends_on)
        self.assertIsNone(parse_current(work / "current.md").focus_item)

    def test_block_intake_then_defer_without_attempt(self) -> None:
        project, work = create_state(["| reveal-core | intake | — | — | — | finding | review-intake | Review. |"])
        block = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "block-item:reveal-core"
        )
        apply_action(work, project, block, {"reason": "Needs evidence.", "depends_on": []})
        defer = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "defer:reveal-core"
        )

        apply_action(
            work,
            project,
            defer,
            {"timing": "safe-to-defer", "reopen_condition": "Evidence arrives."},
        )

        item = parse_queue(work / "queue.md").by_id()["reveal-core"]
        self.assertEqual(WorkState.DEFERRED, item.state)
        self.assertEqual("safe-to-defer", item.timing)

    def test_resume_blocked_item_without_attempt_returns_it_to_ready(self) -> None:
        project, work = create_state(["| reveal-core | blocked | — | — | — | finding | none | Unblocked. |"])
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "resume:reveal-core"
        )

        apply_action(work, project, action, {})

        self.assertEqual(WorkState.READY, parse_queue(work / "queue.md").items[0].state)

    def test_merge_proposal_appends_evidence_and_archives_inbox(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        create_proposal(work, project, proposal())
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "merge-proposal:finding-1"
        )

        apply_action(work, project, action, {"target": "reveal-core"})

        self.assertIn("Intake evidence", (work / "items" / "reveal-core.md").read_text(encoding="utf-8"))
        self.assertFalse((work / "inbox" / "finding-1.json").exists())
        self.assertTrue((work / "history" / "proposals" / "finding-1.json").is_file())

    def test_return_and_reject_proposals_record_closed_dispositions(self) -> None:
        project, work = create_state([])
        for proposal_id, kind in (("finding-return", "return-proposal"), ("finding-reject", "reject-proposal")):
            create_proposal(work, project, proposal(proposal_id))
            action = next(
                candidate
                for candidate in actions_for(work, project, "coordinator")
                if candidate.action_id == f"{kind}:{proposal_id}"
            )

            apply_action(work, project, action, {"reason": "The evidence does not support admission."})

            self.assertFalse((work / "inbox" / f"{proposal_id}.json").exists())
            history_path = work / "history" / "proposals" / f"{proposal_id}.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual("returned" if kind == "return-proposal" else "rejected", history["disposition"])
            self.assertEqual("The evidence does not support admission.", history["coordinator_reason"])
            self.assertEqual(proposal_id, history["proposal"]["proposal_id"])

    def test_transfer_coordinator_is_a_revision_checked_action(self) -> None:
        project, work = create_state([])
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "transfer-coordinator:ledger"
        )

        apply_action(work, project, action, {"task_id": "replacement", "host_id": "local"})

        self.assertEqual(2, actions_for(work, project, "coordinator")[0].coordinator_generation)

    def test_invalid_timing_is_rejected_before_mutation(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "defer:reveal-core"
        )
        before = self.snapshot(work)

        with self.assertRaisesRegex(TransitionError, "TRANSITION_INPUT_INVALID"):
            apply_action(work, project, action, {"timing": "eventually", "reopen_condition": "Later."})

        self.assertEqual(before, self.snapshot(work))

    def test_duplicate_attempt_is_rejected_before_mutation(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        attempt = work / "attempts" / "reveal-core-1"
        attempt.mkdir()
        (attempt / "attempt.md").write_text("occupied", encoding="utf-8")
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "activate:reveal-core"
        )

        with self.assertRaisesRegex(TransitionError, "ATTEMPT_ALREADY_EXISTS"):
            apply_action(
                work,
                project,
                action,
                {
                    "attempt": "reveal-core-1",
                    "branch": "codex/reveal-core",
                    "base_revision": "abc123",
                    "owner": "worker",
                },
            )

    def test_proposal_revision_and_presence_tokens_are_checked(self) -> None:
        project, work = create_state([])
        create_proposal(work, project, proposal())
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "reject-proposal:finding-1"
        )
        proposal_path = work / "inbox" / "finding-1.json"
        proposal_path.write_text(proposal_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaisesRegex(TransitionError, "PROPOSAL_REVISION_STALE"):
            apply_action(work, project, action, {"reason": "stale"})

        fresh = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "reject-proposal:finding-1"
        )
        proposal_path.unlink()
        with self.assertRaisesRegex(TransitionError, "PROPOSAL_NOT_FOUND"):
            apply_action(work, project, fresh, {"reason": "missing"})

    def test_proposal_handler_rejects_duplicate_targets_and_existing_history(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        create_proposal(work, project, proposal())
        accept = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "accept-proposal:finding-1"
        )
        with self.assertRaisesRegex(TransitionError, "ITEM_ALREADY_EXISTS"):
            apply_action(
                work,
                project,
                accept,
                {
                    "item": "reveal-core",
                    "state": "intake",
                    "timing": None,
                    "depends_on": [],
                    "next_action": "review-intake",
                },
            )

        merge = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "merge-proposal:finding-1"
        )
        with self.assertRaisesRegex(TransitionError, "ITEM_NOT_FOUND"):
            apply_action(work, project, merge, {"target": "missing-item"})

        history = work / "history" / "proposals" / "finding-1.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(TransitionError, "PROPOSAL_HISTORY_EXISTS"):
            apply_action(work, project, merge, {"target": "reveal-core"})

    def test_completion_refuses_to_overwrite_existing_history(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        history = work / "history" / "items" / "reveal-core.md"
        history.write_text(
            "---\nkind: work-history\nschema: repo-work/v1\nitem: reveal-core\nstate: done\n---\n",
            encoding="utf-8",
        )
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "complete:reveal-core-1"
        )

        with self.assertRaisesRegex(TransitionError, "HISTORY_RECORD_EXISTS"):
            apply_action(work, project, action, {"evidence": "duplicate"})


if __name__ == "__main__":
    unittest.main()
