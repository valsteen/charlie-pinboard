import unittest

from charlie_pinboard.legacy.actions import ActionError, AuthorizationKind, actions_for

from .support import create_state


class AvailableActionsTest(unittest.TestCase):
    def test_idle_coordinator_can_activate_each_ready_item(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | ready | — | — | — | design | activate | Ready. |",
                "| optional-check | deferred | safe-to-defer | — | — | finding | none | Later. |",
            ]
        )

        actions = actions_for(work, project, role="coordinator")

        self.assertIn("activate:reveal-core", {action.action_id for action in actions})
        self.assertNotIn("activate:optional-check", {action.action_id for action in actions})
        self.assertTrue(all(action.expected_revision for action in actions))
        self.assertTrue(all(action.coordinator_generation == 1 for action in actions))

    def test_active_coordinator_sees_attempt_actions_and_disjoint_activation(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                "| mapping-create | ready | — | — | — | design | activate | Ready. |",
            ],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertTrue(
            {
                "continue:reveal-core-1",
                "dispatch:reveal-core-1",
                "pause:reveal-core-1",
                "block:reveal-core-1",
                "complete:reveal-core-1",
                "activate:mapping-create",
            }.issubset(action_ids)
        )

    def test_worker_sees_only_actions_for_active_attempt(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )

        action_ids = {action.action_id for action in actions_for(work, project, role="worker")}

        self.assertEqual(
            {
                "continue:reveal-core-1",
                "report-blocker:reveal-core-1",
                "submit-review:reveal-core-1",
            },
            action_ids,
        )

    def test_v1_action_authorization_matches_the_requested_role(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )

        for role, expected in (
            ("observer", AuthorizationKind.OBSERVER),
            ("worker", AuthorizationKind.ATTEMPT),
            ("coordinator", AuthorizationKind.COORDINATOR),
        ):
            with self.subTest(role=role):
                actions = actions_for(work, project, role)
                self.assertTrue(actions)
                self.assertEqual({expected}, {action.authorization for action in actions})

    def test_blocked_item_is_not_resumable_while_dependency_is_live(self) -> None:
        project, work = create_state(
            [
                "| foundation | ready | — | — | — | finding | activate | Ready. |",
                "| reveal-core | blocked | — | foundation | — | design | none | Wait. |",
            ]
        )

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertNotIn("resume:reveal-core", action_ids)

    def test_paused_attempt_is_resumable(self) -> None:
        project, work = create_state(["| reveal-core | paused | — | — | reveal-core-1 | design | resume | Paused. |"])
        attempt_dir = work / "attempts" / "reveal-core-1"
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
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

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertIn("resume:reveal-core", action_ids)

    def test_intake_has_closed_admission_choices(self) -> None:
        project, work = create_state(
            ["| reveal-core | intake | — | — | — | proposal:finding-1 | review-intake | Review. |"]
        )

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertTrue(
            {
                "mark-ready:reveal-core",
                "block-item:reveal-core",
                "defer:reveal-core",
            }.issubset(action_ids)
        )

    def test_actions_reject_invalid_roles_and_invalid_ledgers(self) -> None:
        project, work = create_state([])
        with self.assertRaisesRegex(ActionError, "ROLE_INVALID"):
            actions_for(work, project, role="invented")

        (work / "current.md").unlink()
        with self.assertRaisesRegex(ActionError, "WORK_STATE_INVALID"):
            actions_for(work, project, role="coordinator")

    def test_coordinator_actions_work_without_optional_inbox_directory(self) -> None:
        project, work = create_state([])
        (work / "inbox").rmdir()

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertEqual({"transfer-coordinator:ledger"}, action_ids)


if __name__ == "__main__":
    unittest.main()
