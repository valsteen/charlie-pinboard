import json
import tempfile
import unittest
from pathlib import Path

from charlie_pinboard.application.registration import InitializationError
from charlie_pinboard.legacy.actions import actions_for
from charlie_pinboard.legacy.registration import (
    RegistrationError,
    initialize_sqlite_work_state,
    initialize_work_state,
    transfer_coordinator,
)
from charlie_pinboard.legacy.validate import validate_work_state


class RegistrationTest(unittest.TestCase):
    def test_initializes_current_sqlite_state_locally_and_at_an_external_root(self) -> None:
        for external in (False, True):
            project = Path(tempfile.mkdtemp()).resolve()
            work = project.parent / f"{project.name}-work" if external else None

            receipt = initialize_sqlite_work_state(project, work)
            resumed = initialize_sqlite_work_state(project, work)

            self.assertEqual(0, receipt.project_revision)
            self.assertFalse(receipt.resumed)
            self.assertTrue(resumed.resumed)
            self.assertTrue(receipt.database_path.is_file())
            self.assertTrue((receipt.work_root / "artifacts").is_dir())
            self.assertTrue((receipt.work_root / "views" / "queue.md").is_file())
            self.assertFalse((receipt.work_root / "authority.json").exists())
            self.assertFalse((receipt.work_root / "queue.md").exists())

    def test_current_initialization_rejects_legacy_state_and_unverified_external_parent(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        work.mkdir(parents=True)
        (work / "queue.md").write_text("legacy", encoding="utf-8")

        with self.assertRaises(InitializationError) as legacy:
            initialize_sqlite_work_state(project)
        self.assertEqual("MIGRATION_REQUIRED", legacy.exception.code)

        other = Path(tempfile.mkdtemp()).resolve()
        missing_parent = other / "missing" / "work"
        with self.assertRaises(InitializationError) as invalid_root:
            initialize_sqlite_work_state(project, missing_parent)
        self.assertEqual("STORAGE_IO_ERROR", invalid_root.exception.code)

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

    def test_initializes_explicit_shadow_root_without_claiming_canonical_path(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        shadow = project / ".codex" / "work-shadow"

        initialized = initialize_work_state(project, "coordinator-task", "local-host", shadow)

        self.assertEqual(shadow.resolve(), initialized)
        self.assertFalse((project / ".codex" / "work").exists())
        self.assertTrue(validate_work_state(shadow, project).valid)

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
        self.assertNotEqual(
            old_action.expected_revision, actions_for(work, project, "coordinator")[0].expected_revision
        )

    def test_transfer_rejects_wrong_generation_without_change(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = initialize_work_state(project, "coordinator-task", "local-host")
        before = (work / "coordinator.json").read_bytes()

        with self.assertRaisesRegex(RegistrationError, "COORDINATOR_OWNERSHIP_CONFLICT"):
            transfer_coordinator(work, project, 2, "replacement-task", "replacement-host")

        self.assertEqual(before, (work / "coordinator.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
