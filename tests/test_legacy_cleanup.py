import json
import shutil
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO

from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.stored_state import TransitionHistoryActionKind
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.legacy.authority import AuthorityError, resolve_authority
from charlie_pinboard.legacy.legacy_cleanup import cleanup_legacy
from charlie_pinboard.legacy.legacy_import import (
    CUTOVER_TOMBSTONE,
    LegacyImportError,
    archive_legacy,
    cutover_ledger,
    import_ledger,
)
from tests.test_legacy_import import NOW, _fixture


class LegacyCleanupTest(unittest.TestCase):
    def test_cutover_publishes_exact_tombstone_and_archives_legacy_selectors(self) -> None:
        project, work = _fixture()

        receipt = cutover_ledger(project, work, NOW)
        replay = cutover_ledger(project, work, NOW)

        self.assertEqual(receipt, replay)
        self.assertEqual(CUTOVER_TOMBSTONE, (work / "authority.json").read_bytes())
        self.assertTrue((work / "state.sqlite3").is_file())
        self.assertTrue((work / "legacy-v2").is_dir())
        self.assertFalse((work / "v2").exists())
        self.assertEqual(b"inactive v1 queue\n", (work / "legacy-v1" / "queue.md").read_bytes())
        with self.assertRaises(AuthorityError):
            resolve_authority(work)
        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        self.assertEqual(receipt.cutover_id, state.history.receipts[0].action_id.removeprefix("legacy-import:"))
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--project-root", str(project), "--work-root", str(work), "overview", "--json"])
        self.assertEqual(0, result)

    def test_cleanup_removes_marker_last_and_commits_one_idempotent_receipt(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)

        first = cleanup_legacy(work, imported.cutover_id, NOW)
        second = cleanup_legacy(work, imported.cutover_id, NOW)
        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()

        self.assertEqual(first, second)
        self.assertFalse((work / "authority.json").exists())
        self.assertFalse((work / "legacy-v2").exists())
        self.assertFalse((work / "legacy-v1").exists())
        cleanup_rows = [
            row for row in state.history.receipts if row.action_kind == TransitionHistoryActionKind.LEGACY_CLEANUP
        ]
        self.assertEqual(1, len(cleanup_rows))
        self.assertEqual(2, cleanup_rows[0].project_revision)
        artifact = next(
            reference for reference in state.artifacts.references if reference.selector == first.artifact_selector
        )
        self.assertEqual(first.artifact_sha256, artifact.content_sha256)
        self.assertEqual(first.receipt_bytes, (work / first.artifact_selector).read_bytes())

    def test_temporary_cleanup_cli_requires_the_exact_cutover_id(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        output = StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "legacy-cleanup",
                    "--expected-cutover-id",
                    imported.cutover_id,
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual(imported.cutover_id, json.loads(output.getvalue())["cutover_id"])

    def test_cleanup_resumes_retired_tree_and_unreferenced_receipt_artifact(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        comparison_project, comparison_work = _fixture()
        comparison_imported = cutover_ledger(comparison_project, comparison_work, NOW)
        self.assertEqual(imported.cutover_id, comparison_imported.cutover_id)
        expected_receipt = cleanup_legacy(comparison_work, comparison_imported.cutover_id, NOW).receipt_bytes
        retired = work / f".retired-legacy-v2-{imported.cutover_id}"
        (work / "legacy-v2").replace(retired)
        orphan = work / "artifacts" / "evidence" / f"legacy-cleanup-{imported.cutover_id}" / "1.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(expected_receipt)

        retry_time = NOW + timedelta(seconds=1)
        receipt = cleanup_legacy(work, imported.cutover_id, retry_time)

        self.assertFalse(retired.exists())
        self.assertEqual(receipt.receipt_bytes, orphan.read_bytes())
        self.assertNotEqual(expected_receipt, receipt.receipt_bytes)
        self.assertEqual(retry_time, receipt.verified_clean_at)

    def test_cleanup_refuses_missing_mapped_artifact_before_legacy_deletion(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        requirement = next(
            reference for reference in state.artifacts.references if reference.kind.value == "requirements"
        )
        (work / requirement.selector).unlink()

        with self.assertRaises(LegacyImportError) as raised:
            cleanup_legacy(work, imported.cutover_id, NOW)

        self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
        self.assertTrue((work / "legacy-v2").is_dir())
        self.assertEqual(CUTOVER_TOMBSTONE, (work / "authority.json").read_bytes())

    def test_cleanup_replay_revalidates_absence_and_input_contract(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        cleanup_legacy(work, imported.cutover_id, NOW)
        (work / "queue.md").write_text("reintroduced")

        with self.assertRaises(LegacyImportError):
            cleanup_legacy(work, imported.cutover_id, NOW)
        with self.assertRaises(LegacyImportError):
            cleanup_legacy(work, "invalid", NOW)

    def test_cutover_rejects_changed_source_before_authority_replacement(self) -> None:
        for change in ("changed-bytes", "new-selector"):
            with self.subTest(change=change):
                project, work = _fixture()
                import_ledger(project, work, work / "state.sqlite3", NOW)
                if change == "changed-bytes":
                    queue = work / "v2" / "queue.md"
                    queue.write_bytes(queue.read_bytes() + b"changed\n")
                else:
                    (work / "v2" / "new.md").write_text("new selector")

                with self.assertRaises(LegacyImportError) as raised:
                    cutover_ledger(project, work, NOW)

                self.assertEqual("LEGACY_SOURCE_INVALID", raised.exception.code)
                self.assertNotEqual(CUTOVER_TOMBSTONE, (work / "authority.json").read_bytes())

    def test_archival_requires_exact_tombstone_and_unambiguous_tree(self) -> None:
        for condition in ("wrong-marker", "both-v2-trees"):
            with self.subTest(condition=condition):
                project, work = _fixture()
                receipt = import_ledger(project, work, work / "state.sqlite3", NOW)
                if condition == "both-v2-trees":
                    (work / "authority.json").write_bytes(CUTOVER_TOMBSTONE)
                    (work / "legacy-v2").mkdir()

                with self.assertRaises(LegacyImportError) as raised:
                    archive_legacy(work, receipt)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)

    def test_cleanup_rejects_unsupported_preconditions(self) -> None:
        for condition in ("unknown-cutover", "naive-time", "wrong-marker", "ambiguous-retired-tree"):
            with self.subTest(condition=condition):
                project, work = _fixture()
                imported = cutover_ledger(project, work, NOW)
                cutover_id = imported.cutover_id
                now = NOW
                if condition == "unknown-cutover":
                    cutover_id = "0" * 64
                elif condition == "naive-time":
                    now = NOW.replace(tzinfo=None)
                elif condition == "wrong-marker":
                    (work / "authority.json").write_text("wrong marker")
                elif condition == "ambiguous-retired-tree":
                    (work / f".retired-legacy-v2-{cutover_id}").mkdir()

                with self.assertRaises(LegacyImportError) as raised:
                    cleanup_legacy(work, cutover_id, now)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)

    def test_cleanup_rejects_unrecoverable_receipt_artifact_collision(self) -> None:
        for collision_kind in ("directory", "arbitrary-file"):
            with self.subTest(collision_kind=collision_kind):
                project, work = _fixture()
                imported = cutover_ledger(project, work, NOW)
                collision = work / "artifacts" / "evidence" / f"legacy-cleanup-{imported.cutover_id}" / "1.json"
                if collision_kind == "directory":
                    collision.mkdir(parents=True)
                else:
                    collision.parent.mkdir(parents=True)
                    collision.write_bytes(b"not a cleanup receipt")

                with self.assertRaises(LegacyImportError) as raised:
                    cleanup_legacy(work, imported.cutover_id, NOW)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)

    def test_archival_resume_revalidates_frozen_source_after_tombstone(self) -> None:
        project, work = _fixture()
        receipt = import_ledger(project, work, work / "state.sqlite3", NOW)
        (work / "authority.json").write_bytes(CUTOVER_TOMBSTONE)
        queue = work / "v2" / "queue.md"
        queue.write_bytes(queue.read_bytes() + b"changed after tombstone\n")

        with self.assertRaises(LegacyImportError) as raised:
            archive_legacy(work, receipt)

        self.assertEqual("LEGACY_SOURCE_INVALID", raised.exception.code)
        self.assertTrue((work / "v2").is_dir())

    def test_cleanup_resumes_after_physical_removal_before_receipt(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        shutil.rmtree(work / "legacy-v2")
        shutil.rmtree(work / "legacy-v1")
        (work / "authority.json").unlink()

        receipt = cleanup_legacy(work, imported.cutover_id, NOW)

        self.assertEqual(2, receipt.committed_revision)

    def test_temporary_cli_cutover_and_plain_cleanup_paths_are_explicit(self) -> None:
        project, work = _fixture()
        cutover_output = StringIO()
        with redirect_stdout(cutover_output):
            cutover_result = main(
                ["--project-root", str(project), "--work-root", str(work), "legacy-import", "cutover"]
            )
        cutover_id = cutover_output.getvalue().split("cutover_id=", 1)[1].split(" ", 1)[0]
        cleanup_output = StringIO()
        with redirect_stdout(cleanup_output):
            cleanup_result = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "legacy-cleanup",
                    "--expected-cutover-id",
                    cutover_id,
                ]
            )

        self.assertEqual(0, cutover_result)
        self.assertEqual(0, cleanup_result)
        self.assertIn("OK LEGACY_CLEANUP", cleanup_output.getvalue())


if __name__ == "__main__":
    unittest.main()
