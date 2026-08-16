from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repo_work.actions import actions_for
from repo_work.registration import RegistrationError, initialize_work_state, transfer_coordinator
from repo_work.validate import validate_work_state


class RegistrationTest(unittest.TestCase):
    def test_initializes_empty_valid_ledger_with_exact_coordinator(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()

        work = initialize_work_state(project, "coordinator-task", "local-host")

        coordinator = json.loads((work / "coordinator.json").read_text(encoding="utf-8"))
        self.assertEqual("coordinator-task", coordinator["task_id"])
        self.assertEqual(1, coordinator["generation"])
        self.assertTrue(validate_work_state(work, project).valid)

    def test_refuses_to_initialize_over_existing_state(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = initialize_work_state(project, "coordinator-task", "local-host")
        before = (work / "queue.md").read_bytes()

        with self.assertRaisesRegex(RegistrationError, "WORK_STATE_ALREADY_EXISTS"):
            initialize_work_state(project, "replacement-task", "other-host")

        self.assertEqual(before, (work / "queue.md").read_bytes())

    def test_transfer_increments_generation_and_stales_old_actions(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = initialize_work_state(project, "coordinator-task", "local-host")
        old_action = next(
            action
            for action in actions_for(work, project, "coordinator")
            if action.action_id == "transfer-coordinator:ledger"
        )

        transfer_coordinator(work, project, 1, "replacement-task", "replacement-host")

        coordinator = json.loads((work / "coordinator.json").read_text(encoding="utf-8"))
        self.assertEqual("replacement-task", coordinator["task_id"])
        self.assertEqual(2, coordinator["generation"])
        self.assertNotEqual(old_action.expected_revision, actions_for(work, project, "coordinator")[0].expected_revision)

    def test_transfer_rejects_wrong_generation_without_change(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = initialize_work_state(project, "coordinator-task", "local-host")
        before = (work / "coordinator.json").read_bytes()

        with self.assertRaisesRegex(RegistrationError, "COORDINATOR_OWNERSHIP_CONFLICT"):
            transfer_coordinator(work, project, 2, "replacement-task", "replacement-host")

        self.assertEqual(before, (work / "coordinator.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
