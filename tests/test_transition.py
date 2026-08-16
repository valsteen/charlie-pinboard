from __future__ import annotations

import unittest

from repo_work.actions import actions_for
from repo_work.markdown import parse_current, parse_queue
from repo_work.model import WorkState
from repo_work.transition import TransitionError, apply_action
from repo_work.validate import validate_work_state
from tests.support import create_state


class TransitionTest(unittest.TestCase):
    def snapshot(self, work) -> dict[str, bytes]:
        return {
            str(path.relative_to(work)): path.read_bytes()
            for path in work.rglob("*")
            if path.is_file() and path.name != ".transition.lock"
        }

    def test_activate_updates_ledger_pointer_and_attempt(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
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
        self.assertEqual("reveal-core", parse_current(work / "current.md").active_item)
        self.assertTrue((work / "attempts" / "reveal-core-1" / "attempt.md").is_file())
        self.assertTrue(validate_work_state(work, project).valid)

    def test_stale_action_changes_no_state(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
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
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
        action = actions_for(work, project, role="coordinator")[0]
        object.__setattr__(action, "coordinator_generation", 2)
        before = self.snapshot(work)

        with self.assertRaisesRegex(TransitionError, "COORDINATOR_OWNERSHIP_CONFLICT"):
            apply_action(work, project, action, {})

        self.assertEqual(before, self.snapshot(work))

    def test_pause_preserves_attempt_and_clears_active_pointer(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            active_item="reveal-core",
            active_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "pause:reveal-core-1"
        )

        apply_action(work, project, action, {"reason": "A prerequisite needs attention."})

        self.assertEqual(WorkState.PAUSED, parse_queue(work / "queue.md").items[0].state)
        self.assertIsNone(parse_current(work / "current.md").active_item)
        self.assertIn(
            "state: paused",
            (work / "attempts" / "reveal-core-1" / "attempt.md").read_text(encoding="utf-8"),
        )
        self.assertTrue(validate_work_state(work, project).valid)

    def test_complete_removes_live_item_and_preserves_history(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            active_item="reveal-core",
            active_attempt="reveal-core-1",
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
        project, work = create_state(
            ["| reveal-core | paused | — | — | reveal-core-1 | design | resume | Paused. |"]
        )
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
        self.assertEqual("reveal-core-1", parse_current(work / "current.md").active_attempt)
        self.assertIn("state: active", attempt_path.read_text(encoding="utf-8"))
        self.assertTrue(validate_work_state(work, project).valid)

    def test_reopen_returns_deferred_item_to_intake(self) -> None:
        project, work = create_state(
            [
                "| optional-check | deferred | safe-to-defer | — | — | finding | none | Reopen on evidence. |"
            ]
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


if __name__ == "__main__":
    unittest.main()
