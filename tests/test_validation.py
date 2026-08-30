import contextlib
import io
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from msgspec.structs import replace as struct_replace

from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.registration import initialize_work_state
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import NewArtifact
from pinboard.application.validation import validate_work_state
from pinboard.interfaces.cli import main
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes
from tests.support import SQLITE_NOW, complete_sqlite_state
from tests.work_brief_support import work_a_brief


def _malformed_brief(_project: Path) -> bytes:
    return b"{}\n"


def _mismatched_brief(project: Path) -> bytes:
    return canonical_work_brief_bytes(struct_replace(work_a_brief(project), branch="codex/different"))


class SQLiteValidationTest(unittest.TestCase):
    def run_git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

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
        missing = Path(tempfile.mkdtemp()).resolve() / ".codex" / "pinboard"
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

    def test_initialization_reconciles_owned_publication_residue(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        first = initialize_work_state(project, now=SQLITE_NOW)
        store = SQLiteWorkStore(first.database_path)
        store.initialize_state(complete_sqlite_state())
        before = store.snapshot()
        staging = first.database_path.with_name(f".{first.database_path.name}.pinboard-stage")
        staging.hardlink_to(first.database_path)
        staging_journal = staging.with_name(f"{staging.name}-journal")
        staging_journal.write_bytes(b"owned publication residue")

        resumed = initialize_work_state(project, now=SQLITE_NOW)

        self.assertTrue(resumed.resumed)
        self.assertEqual(first.database_path, resumed.database_path)
        self.assertEqual(before, SQLiteWorkStore(resumed.database_path).snapshot())
        self.assertTrue(resumed.database_path.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(staging_journal.exists())

    def test_initialization_rejects_conflicting_publication_residue_without_mutation(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        receipt = initialize_work_state(project, now=SQLITE_NOW)
        staging = receipt.database_path.with_name(f".{receipt.database_path.name}.pinboard-stage")
        staging.write_bytes(b"different file")
        database_before = receipt.database_path.read_bytes()
        staging_before = staging.read_bytes()

        with self.assertRaises(StorageError) as raised:
            initialize_work_state(project, now=SQLITE_NOW)

        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, raised.exception.code)
        self.assertEqual(database_before, receipt.database_path.read_bytes())
        self.assertEqual(staging_before, staging.read_bytes())

    def test_initialization_rejects_malformed_database_before_residue_cleanup(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        receipt = initialize_work_state(project, now=SQLITE_NOW)
        staging = receipt.database_path.with_name(f".{receipt.database_path.name}.pinboard-stage")
        staging.hardlink_to(receipt.database_path)
        receipt.database_path.write_bytes(b"malformed database")
        database_before = receipt.database_path.read_bytes()

        with self.assertRaises(StorageError) as raised:
            initialize_work_state(project, now=SQLITE_NOW)

        self.assertEqual(StorageErrorCode.INVALID_STATE, raised.exception.code)
        self.assertEqual(database_before, receipt.database_path.read_bytes())
        self.assertEqual(database_before, staging.read_bytes())

    def test_default_initialization_uses_private_root_and_exact_local_git_exclusion(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        self.run_git(project, "init", "-b", "main")
        gitignore = project / ".gitignore"
        gitignore.write_text("*.user-cache\n", encoding="utf-8")
        config = project / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-5"\n', encoding="utf-8")
        self.run_git(project, "add", ".gitignore", ".codex/config.toml")
        self.run_git(
            project,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        exclude = project / ".git" / "info" / "exclude"
        original_exclude = exclude.read_bytes()
        original_gitignore = gitignore.read_bytes()

        first = initialize_work_state(project, now=SQLITE_NOW)
        second = initialize_work_state(project, now=SQLITE_NOW)

        self.assertEqual(project / ".codex" / "pinboard", first.work_root)
        self.assertEqual(first.work_root, second.work_root)
        self.assertEqual(original_exclude + b"/.codex/pinboard/\n", exclude.read_bytes())
        self.assertEqual(1, exclude.read_text(encoding="utf-8").splitlines().count("/.codex/pinboard/"))
        self.assertEqual(original_gitignore, gitignore.read_bytes())
        self.assertEqual("", self.run_git(project, "status", "--short", "--untracked-files=all"))

    def test_explicit_work_root_preserves_its_path_without_changing_git_excludes(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        self.run_git(project, "init", "-b", "main")
        exclude = project / ".git" / "info" / "exclude"
        original_exclude = exclude.read_bytes()
        destination = Path(tempfile.mkdtemp()).resolve() / "selected-work-root"

        receipt = initialize_work_state(project, destination, now=SQLITE_NOW)

        self.assertEqual(destination, receipt.work_root)
        self.assertEqual(original_exclude, exclude.read_bytes())

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
                    NewArtifact(stored_state.ArtifactKind.BRIEF, "work-a-1", 1, ".json", content),
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
