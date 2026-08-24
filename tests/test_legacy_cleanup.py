import json
import shutil
import sqlite3
import unittest
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier, Lock, get_ident
from unittest.mock import patch

from charlie_pinboard.adapters.sqlite.database import OpenMode, StorageError, open_database, write_transaction
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.stored_state import TransitionHistoryActionKind
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.legacy.authority import AuthorityError, resolve_authority
from charlie_pinboard.legacy.legacy_cleanup import (
    CleanupReceipt,
    _CleanupRepairContract,
    _decode_correction_receipt,
    _decode_receipt,
    _existing_receipt,
    _validate_orphan_correction_receipt,
    cleanup_legacy,
)
from charlie_pinboard.legacy.legacy_import import (
    CUTOVER_TOMBSTONE,
    LegacyImportError,
    archive_legacy,
    cutover_ledger,
    import_ledger,
)
from tests.test_legacy_import import NOW
from tests.test_legacy_import import _fixture as _import_fixture


def _fixture() -> tuple[Path, Path]:
    project, work = _import_fixture()
    attempt = work / "v2" / "attempts" / "work-a-1" / "attempt.md"
    attempt.write_text(attempt.read_text().replace('lease_status: "active"', 'lease_status: "released"'))
    return project, work


def _repair_contract(original: CleanupReceipt) -> _CleanupRepairContract:
    return _CleanupRepairContract(
        original.cutover_id,
        original.artifact_selector,
        original.artifact_sha256,
        original.committed_revision,
        ("inbox",),
    )


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

    def test_cutover_replay_ignores_regenerated_platform_metadata(self) -> None:
        project, work = _fixture()
        (work / ".DS_Store").write_bytes(b"frozen platform metadata")
        receipt = cutover_ledger(project, work, NOW)
        (work / ".DS_Store").write_bytes(b"regenerated root metadata")
        (work / "legacy-v1" / ".DS_Store").write_bytes(b"regenerated archive metadata")

        replay = cutover_ledger(project, work, NOW)

        self.assertEqual(receipt, replay)
        self.assertEqual(CUTOVER_TOMBSTONE, (work / "authority.json").read_bytes())

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

    def test_cleanup_removes_unmanifested_empty_legacy_root_and_reopens_sqlite_cli(self) -> None:
        project, work = _fixture()
        (work / "inbox").mkdir()
        imported = cutover_ledger(project, work, NOW)

        receipt = cleanup_legacy(work, imported.cutover_id, NOW)

        self.assertFalse((work / "inbox").exists())
        self.assertIn("inbox", json.loads(receipt.receipt_bytes)["absent_selectors"])
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--project-root", str(project), "--work-root", str(work), "overview", "--json"])
        self.assertEqual(0, result)
        self.assertEqual("sqlite-v1", json.loads(output.getvalue())["authority"])

    def test_cleanup_repairs_one_incomplete_historical_receipt_without_replacing_it(self) -> None:
        project, work = _fixture()
        (work / "inbox").mkdir()
        imported = cutover_ledger(project, work, NOW)
        with (
            patch(
                "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
                ("queue.md",),
                create=True,
            ),
            patch("charlie_pinboard.legacy.legacy_cleanup._runtime_sha256", return_value="1" * 64),
            patch("charlie_pinboard.legacy.legacy_cleanup.__version__", "0.1.0+historical"),
        ):
            original = cleanup_legacy(work, imported.cutover_id, NOW)
        original_bytes = (work / original.artifact_selector).read_bytes()

        with patch(
            "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
            _repair_contract(original),
        ):
            repaired = cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=1))
            replay = cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=2))
        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()

        self.assertEqual(original.artifact_selector, repaired.artifact_selector)
        self.assertEqual(original.artifact_sha256, repaired.artifact_sha256)
        self.assertEqual(original_bytes, (work / original.artifact_selector).read_bytes())
        self.assertFalse((work / "inbox").exists())
        self.assertIsNotNone(repaired.correction)
        assert repaired.correction is not None
        self.assertEqual(("inbox",), repaired.correction.removed_selectors)
        self.assertEqual(2, repaired.correction.database_revision_before_receipt)
        self.assertEqual(3, repaired.correction.committed_revision)
        self.assertEqual(repaired, replay)
        repair_rows = [
            row for row in state.history.receipts if row.action_id == f"legacy-cleanup-repair:{imported.cutover_id}"
        ]
        self.assertEqual(1, len(repair_rows))
        self.assertEqual(3, state.lifecycle.project.revision)
        correction_reference = next(
            reference
            for reference in state.artifacts.references
            if reference.selector == repaired.correction.artifact_selector
        )
        self.assertEqual(repaired.correction.artifact_sha256, correction_reference.content_sha256)
        self.assertEqual(
            repaired.correction.receipt_bytes,
            (work / repaired.correction.artifact_selector).read_bytes(),
        )
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--project-root", str(project), "--work-root", str(work), "validate", "--json"])
        self.assertEqual(0, result)
        self.assertTrue(json.loads(output.getvalue())["valid"])
        cleanup_output = StringIO()
        with (
            patch(
                "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
                _repair_contract(original),
            ),
            redirect_stdout(cleanup_output),
        ):
            cleanup_result = main(
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
        self.assertEqual(0, cleanup_result)
        self.assertEqual(
            repaired.correction.artifact_selector,
            json.loads(cleanup_output.getvalue())["correction"]["artifact_selector"],
        )
        plain_output = StringIO()
        with (
            patch(
                "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
                _repair_contract(original),
            ),
            redirect_stdout(plain_output),
        ):
            plain_result = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "legacy-cleanup",
                    "--expected-cutover-id",
                    imported.cutover_id,
                ]
            )
        self.assertEqual(0, plain_result)
        self.assertIn(f"correction={repaired.correction.artifact_selector}", plain_output.getvalue())

    def test_cleanup_rejects_unmanifested_file_symlink_or_nonempty_root(self) -> None:
        for residue in ("file", "symlink", "nonempty"):
            with self.subTest(residue=residue):
                project, work = _fixture()
                imported = cutover_ledger(project, work, NOW)
                inbox = work / "inbox"
                if residue == "file":
                    inbox.write_text("unexpected")
                elif residue == "symlink":
                    target = work / "unrelated-empty-directory"
                    target.mkdir()
                    inbox.symlink_to(target, target_is_directory=True)
                else:
                    inbox.mkdir()
                    (inbox / "unexpected.json").write_text("{}")

                with self.assertRaises(LegacyImportError) as raised:
                    cleanup_legacy(work, imported.cutover_id, NOW)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
                self.assertTrue((work / "legacy-v2").is_dir())
                self.assertTrue((work / "authority.json").is_file())

    def test_cleanup_and_repair_reject_active_sqlite_authority(self) -> None:
        for operation, table in (
            ("cleanup", "coordination_lease"),
            ("cleanup", "attempt_leases"),
            ("repair", "coordination_lease"),
            ("repair", "attempt_leases"),
        ):
            with self.subTest(operation=operation, table=table):
                _project, work = _fixture()
                (work / "inbox").mkdir()
                imported = cutover_ledger(_project, work, NOW)
                original: CleanupReceipt | None = None
                if operation == "repair":
                    with patch(
                        "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
                        ("queue.md",),
                        create=True,
                    ):
                        original = cleanup_legacy(work, imported.cutover_id, NOW)
                connection = sqlite3.connect(work / "state.sqlite3")
                try:
                    connection.execute(f"UPDATE {table} SET status = 'active'")
                    connection.commit()
                finally:
                    connection.close()

                repair_patch = (
                    patch(
                        "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
                        _repair_contract(original),
                    )
                    if original is not None
                    else nullcontext()
                )
                with repair_patch, self.assertRaises(LegacyImportError) as raised:
                    cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=1))

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
                if operation == "cleanup":
                    self.assertTrue((work / "legacy-v2").is_dir())
                    self.assertTrue((work / "authority.json").is_file())
                else:
                    self.assertTrue((work / "inbox").is_dir())

    def test_cleanup_repair_rejects_a_non_bitwig_receipt(self) -> None:
        _project, work = _fixture()
        (work / "inbox").mkdir()
        imported = cutover_ledger(_project, work, NOW)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
            ("queue.md",),
            create=True,
        ):
            original = cleanup_legacy(work, imported.cutover_id, NOW)

        with self.assertRaises(LegacyImportError) as raised:
            cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=1))

        self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
        self.assertTrue((work / "inbox").is_dir())
        self.assertEqual(original.receipt_bytes, (work / original.artifact_selector).read_bytes())

    def test_concurrent_incomplete_receipt_repairs_commit_one_correction(self) -> None:
        project, work = _fixture()
        (work / "inbox").mkdir()
        imported = cutover_ledger(project, work, NOW)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
            ("queue.md",),
            create=True,
        ):
            cleanup_legacy(work, imported.cutover_id, NOW)
        barrier = Barrier(2)

        def run_repair(offset: int) -> CleanupReceipt:
            barrier.wait()
            return cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=offset))

        original = _existing_receipt(work, imported.cutover_id)
        assert original is not None
        with (
            patch(
                "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
                _repair_contract(original),
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            receipts = tuple(executor.map(run_repair, (1, 2)))

        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        self.assertEqual(receipts[0], receipts[1])
        self.assertIsNotNone(receipts[0].correction)
        repair_rows = [
            row for row in state.history.receipts if row.action_id == f"legacy-cleanup-repair:{imported.cutover_id}"
        ]
        self.assertEqual(1, len(repair_rows))
        self.assertEqual(3, state.lifecycle.project.revision)

    def test_cleanup_selects_receipt_revision_after_ordinary_writer_is_serialized(self) -> None:
        _project, work = _fixture()
        imported = cutover_ledger(_project, work, NOW)
        database_path = work / "state.sqlite3"

        @contextmanager
        def write_after_ordinary_revision(connection: sqlite3.Connection) -> Generator[None]:
            ordinary = open_database(database_path, OpenMode.READ_WRITE)
            try:
                with write_transaction(ordinary):
                    ordinary.execute(
                        "UPDATE project_meta SET revision = revision + 1, updated_at = ? WHERE singleton = 1",
                        ((NOW + timedelta(seconds=1)).isoformat(),),
                    )
            finally:
                ordinary.close()
            with write_transaction(connection):
                yield

        with patch("charlie_pinboard.legacy.legacy_cleanup.write_transaction", write_after_ordinary_revision):
            receipt = cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=2))

        state = SQLiteWorkStore(database_path).snapshot()
        self.assertEqual(2, receipt.database_revision_before_receipt)
        self.assertEqual(3, receipt.committed_revision)
        self.assertEqual(3, state.lifecycle.project.revision)

    def test_concurrent_cleanup_callers_serialize_to_one_receipt(self) -> None:
        _project, work = _fixture()
        imported = cutover_ledger(_project, work, NOW)
        barrier = Barrier(2)
        counter_lock = Lock()
        callers: set[int] = set()

        def synchronize_first_receipt_lookup(work_root: Path, cutover_id: str) -> CleanupReceipt | None:
            receipt = _existing_receipt(work_root, cutover_id)
            with counter_lock:
                caller_id = get_ident()
                first_lookup = caller_id not in callers
                callers.add(caller_id)
            if first_lookup:
                self.assertIsNone(receipt)
                barrier.wait()
            return receipt

        def run_cleanup(offset: int) -> CleanupReceipt:
            return cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=offset))

        with (
            patch(
                "charlie_pinboard.legacy.legacy_cleanup._existing_receipt",
                synchronize_first_receipt_lookup,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            receipts = tuple(executor.map(run_cleanup, (1, 2)))

        state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        cleanup_rows = [
            row for row in state.history.receipts if row.action_kind == TransitionHistoryActionKind.LEGACY_CLEANUP
        ]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(1, len(cleanup_rows))
        self.assertEqual(2, state.lifecycle.project.revision)

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

    def test_cleanup_repair_resumes_an_unreferenced_correction_artifact(self) -> None:
        def incomplete_cleanup() -> tuple[Path, CleanupReceipt]:
            project, work = _fixture()
            (work / "inbox").mkdir()
            imported = cutover_ledger(project, work, NOW)
            with (
                patch(
                    "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
                    ("queue.md",),
                    create=True,
                ),
                patch("charlie_pinboard.legacy.legacy_cleanup._runtime_sha256", return_value="1" * 64),
                patch("charlie_pinboard.legacy.legacy_cleanup.__version__", "0.1.0+historical"),
            ):
                original = cleanup_legacy(work, imported.cutover_id, NOW)
            return work, original

        comparison_work, comparison_original = incomplete_cleanup()
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
            _repair_contract(comparison_original),
        ):
            comparison = cleanup_legacy(
                comparison_work,
                comparison_original.cutover_id,
                NOW + timedelta(seconds=1),
            )
        assert comparison.correction is not None
        expected_receipt = comparison.correction.receipt_bytes
        work, original = incomplete_cleanup()
        cutover_id = original.cutover_id
        orphan = work / "artifacts" / "evidence" / f"legacy-cleanup-repair-{cutover_id}" / "1.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(expected_receipt)

        retry_time = NOW + timedelta(seconds=2)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
            _repair_contract(original),
        ):
            receipt = cleanup_legacy(work, cutover_id, retry_time)

        self.assertIsNotNone(receipt.correction)
        assert receipt.correction is not None
        self.assertEqual(receipt.correction.receipt_bytes, orphan.read_bytes())
        self.assertNotEqual(expected_receipt, receipt.correction.receipt_bytes)
        self.assertEqual(retry_time, receipt.correction.verified_clean_at)

    def test_cleanup_receipt_boundaries_reject_invalid_json_times_and_noncanonical_orphans(self) -> None:
        project, work = _fixture()
        imported = cutover_ledger(project, work, NOW)
        receipt = cleanup_legacy(work, imported.cutover_id, NOW)
        for invalid in ("not-a-time", "2026-08-24T09:00:00"):
            with self.subTest(receipt_time=invalid):
                payload = json.loads(receipt.receipt_bytes)
                payload["verified_clean_at"] = invalid
                data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                with self.assertRaises(LegacyImportError):
                    _decode_receipt(data)
        with self.assertRaises(LegacyImportError):
            _decode_receipt(b"{")

        repair_project, repair_work = _fixture()
        (repair_work / "inbox").mkdir()
        repair_import = cutover_ledger(repair_project, repair_work, NOW)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
            ("queue.md",),
            create=True,
        ):
            original = cleanup_legacy(repair_work, repair_import.cutover_id, NOW)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT",
            _repair_contract(original),
        ):
            repaired = cleanup_legacy(repair_work, repair_import.cutover_id, NOW + timedelta(seconds=1))
        assert repaired.correction is not None
        correction_record, _verified = _decode_correction_receipt(repaired.correction.receipt_bytes)
        for invalid in ("not-a-time", "2026-08-24T09:00:00"):
            with self.subTest(correction_time=invalid):
                payload = json.loads(repaired.correction.receipt_bytes)
                payload["verified_clean_at"] = invalid
                data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                with self.assertRaises(LegacyImportError):
                    _decode_correction_receipt(data)
        with self.assertRaises(LegacyImportError):
            _decode_correction_receipt(b"{")
        with self.assertRaises(LegacyImportError):
            _validate_orphan_correction_receipt(b"{", correction_record)
        noncanonical = json.dumps(json.loads(repaired.correction.receipt_bytes), indent=2).encode() + b"\n"
        with self.assertRaises(LegacyImportError):
            _validate_orphan_correction_receipt(noncanonical, correction_record)

    def test_cleanup_repair_retry_preserves_the_exact_removed_selector_after_interruption(self) -> None:
        _project, work = _fixture()
        (work / "inbox").mkdir()
        imported = cutover_ledger(_project, work, NOW)
        with patch(
            "charlie_pinboard.legacy.legacy_cleanup.INACTIVE_ROOT_SELECTORS",
            ("queue.md",),
            create=True,
        ):
            original = cleanup_legacy(work, imported.cutover_id, NOW)

        def fail_after_selector_removal(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated publication interruption")

        contract = _repair_contract(original)
        with (
            patch("charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT", contract),
            patch(
                "charlie_pinboard.legacy.legacy_cleanup.ArtifactRepository.publish",
                fail_after_selector_removal,
            ),
            self.assertRaises(StorageError),
        ):
            cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=1))

        self.assertFalse((work / "inbox").exists())
        self.assertEqual(2, SQLiteWorkStore(work / "state.sqlite3").snapshot().lifecycle.project.revision)
        with patch("charlie_pinboard.legacy.legacy_cleanup._BITWIG_REPAIR_CONTRACT", contract):
            repaired = cleanup_legacy(work, imported.cutover_id, NOW + timedelta(seconds=2))

        assert repaired.correction is not None
        self.assertEqual(("inbox",), repaired.correction.removed_selectors)

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
                    (work / "inbox").mkdir()
                    (work / "authority.json").write_text("wrong marker")
                elif condition == "ambiguous-retired-tree":
                    (work / "inbox").mkdir()
                    (work / f".retired-legacy-v2-{cutover_id}").mkdir()

                with self.assertRaises(LegacyImportError) as raised:
                    cleanup_legacy(work, cutover_id, now)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
                if condition in {"wrong-marker", "ambiguous-retired-tree"}:
                    self.assertTrue((work / "inbox").is_dir())

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
