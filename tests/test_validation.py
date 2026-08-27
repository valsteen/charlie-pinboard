import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from msgspec.structs import replace as struct_replace

from charlie_pinboard.adapters.files.artifacts import write_revision
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.registration import initialize_work_state
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.application.validation import validate_work_state
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.interfaces.work_briefs import canonical_work_brief_bytes
from tests.support import SQLITE_NOW, complete_sqlite_state
from tests.work_brief_support import work_a_brief


def _malformed_brief(_project: Path) -> bytes:
    return b"{}\n"


def _mismatched_brief(project: Path) -> bytes:
    return canonical_work_brief_bytes(struct_replace(work_a_brief(project), branch="codex/different"))


class SQLiteValidationTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

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

    def test_initialization_resumes_current_state(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        first = initialize_work_state(project, now=SQLITE_NOW)
        second = initialize_work_state(project, now=SQLITE_NOW)
        self.assertFalse(first.resumed)
        self.assertTrue(second.resumed)
        self.assertEqual(first.database_path, second.database_path)

    def test_validation_rejects_malformed_and_mismatched_live_v2_briefs(self) -> None:
        for name, content_factory in (
            ("malformed", _malformed_brief),
            ("mismatched", _mismatched_brief),
        ):
            with self.subTest(name=name):
                project = Path(tempfile.mkdtemp()).resolve()
                roots = resolve_durable_roots(project)
                initialize_database(roots, SQLITE_NOW)
                content = content_factory(project)
                published = write_revision(
                    roots,
                    NewArtifact(ArtifactKind.BRIEF, "work-a-1", 1, ".json", content),
                )
                state = complete_sqlite_state()
                reference = replace(
                    state.artifact_references[0],
                    key=published.key,
                    revision=published.revision,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
                SQLiteWorkStore(roots.database_path).initialize_state(
                    replace(state, artifact_references=(reference, *state.artifact_references[1:]))
                )

                result, stdout, stderr = self.run_cli(
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(roots.work_root),
                    "validate",
                )

                self.assertEqual(10, result, stderr)
                self.assertIn("WORK_BRIEF_INVALID", stdout)


if __name__ == "__main__":
    unittest.main()
