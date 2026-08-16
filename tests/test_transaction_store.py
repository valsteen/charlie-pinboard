import json
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from repo_work.actions import actions_for
from repo_work.diagnostics import Diagnostic, Severity
from repo_work.transaction_store import (
    AtomicCommitError,
    ChangeSet,
    FileChange,
    commit_change_set,
    journal_path_for,
    recover_pending_commit,
    validate_change_set,
    write_change,
)
from repo_work.validate import ValidationReport

from .support import create_state


def snapshot(work: Path) -> dict[str, bytes]:
    return {str(path.relative_to(work)): path.read_bytes() for path in work.rglob("*") if path.is_file()}


class TransactionStoreTest(unittest.TestCase):
    def test_change_set_rejects_escape_and_duplicate_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "CHANGE_PATH_INVALID"):
            ChangeSet.of(FileChange(PurePosixPath("../outside"), b"value"))
        with self.assertRaisesRegex(ValueError, "CHANGE_PATH_DUPLICATE"):
            ChangeSet.of(write_change("queue.md", "one"), write_change("queue.md", "two"))

    def test_prospective_invalid_state_is_rejected_before_mutation(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        before = snapshot(work)
        changes = ChangeSet.of(FileChange(PurePosixPath("items/reveal-core.md"), None))

        with self.assertRaisesRegex(AtomicCommitError, "TRANSITION_POSTCONDITION_FAILED"):
            validate_change_set(work, project, changes)

        self.assertEqual(before, snapshot(work))

    def test_failed_commit_postcondition_rolls_back_original_bytes(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        before = snapshot(work)
        changes = ChangeSet.of(write_change("current.md", (work / "current.md").read_text(encoding="utf-8")))
        invalid = ValidationReport((Diagnostic("INJECTED", Severity.ERROR, work / "queue.md", "postcondition failed"),))

        with (
            patch("repo_work.validate.validate_work_state_during_commit", return_value=invalid),
            self.assertRaisesRegex(AtomicCommitError, "TRANSITION_POSTCONDITION_FAILED"),
        ):
            commit_change_set(work, project, changes)

        self.assertEqual(before, snapshot(work))
        self.assertFalse(journal_path_for(work).exists())

    def test_invalid_or_existing_journal_stops_recovery_and_commit(self) -> None:
        project, work = create_state([])
        journal = journal_path_for(work)
        journal.mkdir()
        (journal / "manifest.json").write_text(json.dumps({"schema": "wrong", "originals": []}), encoding="utf-8")

        with self.assertRaisesRegex(AtomicCommitError, "COMMIT_JOURNAL_INVALID"):
            recover_pending_commit(work)
        with self.assertRaisesRegex(AtomicCommitError, "COMMIT_RECOVERY_REQUIRED"):
            commit_change_set(work, project, ChangeSet.of(write_change("current.md", "value")))

    def test_invalid_journal_entries_are_typed_rejections(self) -> None:
        _, work = create_state([])
        cases: tuple[object, ...] = (
            "not-an-object",
            {"path": "../outside", "existed": True, "data": ""},
            {"path": "queue.md", "existed": "yes", "data": ""},
            {"path": "queue.md", "existed": True, "data": "not-base64!"},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                journal = journal_path_for(work)
                journal.mkdir(exist_ok=True)
                (journal / "manifest.json").write_text(
                    json.dumps({"schema": "repo-work-journal/v1", "originals": [entry]}), encoding="utf-8"
                )
                with self.assertRaisesRegex(AtomicCommitError, "COMMIT_JOURNAL_INVALID"):
                    recover_pending_commit(work)
                (journal / "manifest.json").unlink()
                journal.rmdir()

    def test_no_pending_journal_is_a_noop(self) -> None:
        project, work = create_state([])

        self.assertFalse(recover_pending_commit(work))
        self.assertTrue(actions_for(work, project, "coordinator"))
