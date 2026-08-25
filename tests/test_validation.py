import tempfile
import unittest
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.registration import initialize_work_state
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.registration import InitializationError
from charlie_pinboard.application.validation import validate_work_state
from tests.support import SQLITE_NOW, complete_sqlite_state


class SQLiteValidationTest(unittest.TestCase):
    def test_fresh_current_state_is_valid_and_stale_views_are_warnings(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        receipt = initialize_work_state(project, now=SQLITE_NOW)

        report = validate_work_state(receipt.work_root)
        self.assertTrue(report.valid, report.render())
        self.assertEqual("OK WORK_STATE_VALID", report.render())

        view = receipt.work_root / "views" / "queue.md"
        view.write_text("stale\n", encoding="utf-8")
        stale = validate_work_state(receipt.work_root)
        self.assertTrue(stale.valid)
        self.assertIn("VIEW_REFRESH_REQUIRED", stale.render())
        self.assertIn("pinboard views rebuild", stale.render())

    def test_missing_database_and_missing_accepted_artifacts_are_errors(self) -> None:
        missing = Path(tempfile.mkdtemp()).resolve() / ".codex" / "work"
        report = validate_work_state(missing)
        self.assertFalse(report.valid)
        self.assertIn("STORAGE_IO_ERROR", report.render())

        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        SQLiteWorkStore(roots.database_path).initialize_state(complete_sqlite_state())
        invalid = validate_work_state(roots.work_root)
        self.assertFalse(invalid.valid)
        self.assertIn("STORAGE_INVARIANT_VIOLATION", invalid.render())

    def test_initialization_resumes_current_state_and_refuses_conflicting_state_files(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        first = initialize_work_state(project, now=SQLITE_NOW)
        second = initialize_work_state(project, now=SQLITE_NOW)
        self.assertFalse(first.resumed)
        self.assertTrue(second.resumed)
        self.assertEqual(first.database_path, second.database_path)

        blocked = Path(tempfile.mkdtemp()).resolve()
        work = blocked / ".codex" / "work"
        work.mkdir(parents=True)
        (work / "authority.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(InitializationError, "WORK_STATE_CONFLICT"):
            initialize_work_state(blocked, now=SQLITE_NOW)


if __name__ == "__main__":
    unittest.main()
