import json
import unittest
from copy import replace
from pathlib import Path

from repo_work.actions import actions_for
from repo_work.markdown import parse_current, parse_queue
from repo_work.model import WorkState
from repo_work.transition import TransitionError
from repo_work.validate import validate_work_state

from .support import JsonObject, apply_action, create_proposal, create_state


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
