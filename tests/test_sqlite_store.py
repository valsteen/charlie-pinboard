import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from charlie_pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from charlie_pinboard.adapters.files.file_io import (
    atomic_replace,
    create_immutable,
    resolve_durable_roots,
)
from charlie_pinboard.adapters.sqlite.database import (
    backup_database,
    initialize_database,
    open_database,
    read_operation,
    schema_bytes,
    write_transaction,
)
from charlie_pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from charlie_pinboard.adapters.sqlite.models import OpenMode
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.mutations import project_transition_mutation
from charlie_pinboard.application.stored_state import (
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
)
from charlie_pinboard.domain.authority_models import AttemptLeaseStatus
from charlie_pinboard.domain.decision_models import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
)
from charlie_pinboard.domain.decisions import available_actions as available_actions_outcome
from charlie_pinboard.domain.decisions import bind_transition as bind_transition_outcome
from charlie_pinboard.domain.decisions import decide as decision_outcome
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
)
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.work_models import (
    AttemptState,
    EvidenceInput,
    ReasonInput,
    SubmitReviewInput,
    TransitionInput,
)
from tests.domain_support import expect_success
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def bind_transition(action: Action, value: TransitionInput) -> TransitionCommand:
    return expect_success(bind_transition_outcome(action, value))


def decide(snapshot: LedgerSnapshot, command: TransitionCommand, now: datetime) -> Decision:
    return expect_success(decision_outcome(snapshot, command, now))


class SQLiteStoreTest(unittest.TestCase):
    def _store(self, *, populated: bool = True) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if populated:
            state = complete_sqlite_state()
            store.initialize_state(state)
        return roots.database_path, store

    def _assert_state_rejected(self, state_name: str, state: StoredWorkState) -> None:
        path, store = self._store(populated=False)
        self.assertTrue(path.exists())
        with self.subTest(state_name=state_name), self.assertRaises(StorageError) as raised:
            store.initialize_state(state)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, raised.exception.code, state_name)
        self.assertEqual(0, store.snapshot().lifecycle.project.revision)

    def test_schema_identity_initialization_backup_and_reopen_contract(self) -> None:
        path, store = self._store()
        self.assertEqual(complete_sqlite_state(), store.snapshot())
        connection = open_database(path, OpenMode.READ_ONLY)
        try:
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            connection.close()

        backup = path.with_name("state-backup.sqlite3")
        backup_database(path, backup)
        self.assertEqual(store.snapshot(), SQLiteWorkStore(backup).snapshot())

        for field, value, expected in (
            ("application", "charlie-board", StorageErrorCode.INVALID_STATE),
            ("schema_version", 0, StorageErrorCode.SCHEMA_UNSUPPORTED),
            ("schema_version", 2, StorageErrorCode.SCHEMA_UNSUPPORTED),
        ):
            tampered, _ = self._store(populated=False)
            connection = sqlite3.connect(tampered)
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(f"UPDATE project_meta SET {field} = ?", (value,))
                connection.commit()
            finally:
                connection.close()
            with self.subTest(field=field, value=value), self.assertRaises(StorageError) as raised:
                open_database(tampered, OpenMode.READ_WRITE)
            self.assertEqual(expected, raised.exception.code)

        malformed, _ = self._store(populated=False)
        connection = sqlite3.connect(malformed)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO current_focus (singleton, item_id, attempt_id, next_action, subject_revision)
                VALUES (1, 'missing', 'missing-1', 'continue', 1)
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as malformed_error:
            open_database(malformed, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, malformed_error.exception.code)

    def test_unsupported_wal_schema_is_rejected_without_mutation(self) -> None:
        newer_wal, _ = self._store(populated=False)
        connection = sqlite3.connect(newer_wal)
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE project_meta SET schema_version = 2")
            connection.commit()
        finally:
            connection.close()
        before_rejection = tuple(
            (candidate.name, candidate.read_bytes())
            for candidate in sorted(newer_wal.parent.iterdir())
            if candidate.is_file()
        )

        with self.assertRaises(StorageError) as newer_error:
            open_database(newer_wal, OpenMode.READ_WRITE)

        self.assertEqual(StorageErrorCode.SCHEMA_UNSUPPORTED, newer_error.exception.code)
        self.assertEqual(
            before_rejection,
            tuple(
                (candidate.name, candidate.read_bytes())
                for candidate in sorted(newer_wal.parent.iterdir())
                if candidate.is_file()
            ),
        )
        mode_probe = sqlite3.connect(newer_wal)
        try:
            self.assertEqual("wal", mode_probe.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            mode_probe.close()

    def test_storage_error_contract_is_stable_across_real_failures(self) -> None:
        path, _store = self._store(populated=False)

        blocker = sqlite3.connect(path, isolation_level=None)
        contender = open_database(path, OpenMode.READ_WRITE)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            contender.execute("PRAGMA busy_timeout = 1")
            with self.assertRaises(StorageError) as busy_error, write_transaction(contender):
                self.fail("a competing immediate writer must not enter the transaction")
            self.assertEqual(StorageErrorCode.BUSY, busy_error.exception.code)
            self.assertTrue(busy_error.exception.retryable)
        finally:
            blocker.rollback()
            blocker.close()
            contender.close()

        read_only = open_database(path, OpenMode.READ_ONLY)
        try:
            with self.assertRaises(StorageError) as io_error, write_transaction(read_only):
                read_only.execute("UPDATE project_meta SET revision = revision + 1")
            self.assertEqual(StorageErrorCode.IO_ERROR, io_error.exception.code)
        finally:
            read_only.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with self.assertRaises(StorageError) as invariant_error, write_transaction(connection):
                connection.execute("INSERT INTO current_focus VALUES (1, 'missing', 'missing-1', 'continue', 1)")
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, invariant_error.exception.code)
            self.assertEqual(0, connection.execute("SELECT revision FROM project_meta").fetchone()[0])

            storage_error = StorageError(StorageErrorCode.STALE_WRITE, "injected stale write")
            with self.assertRaises(StorageError) as preserved, write_transaction(connection):
                raise storage_error
            self.assertIs(storage_error, preserved.exception)

            with self.assertRaises(StorageError) as operation_error, write_transaction(connection):
                connection.execute("UPDATE project_meta SET revision = 9")
                raise RuntimeError("injected application failure")
            self.assertEqual(StorageErrorCode.OPERATION_FAILED, operation_error.exception.code)
            self.assertEqual(0, connection.execute("SELECT revision FROM project_meta").fetchone()[0])
        finally:
            connection.close()

        closed = open_database(path, OpenMode.READ_ONLY)
        closed.close()
        with self.assertRaises(StorageError) as read_error, read_operation(closed):
            closed.execute("SELECT 1")
        self.assertEqual(StorageErrorCode.INVALID_STATE, read_error.exception.code)

    def test_database_open_rejects_missing_and_malformed_identity(self) -> None:
        path, _store = self._store(populated=False)
        roots = resolve_durable_roots(path.parents[2])
        with self.assertRaises(StorageError) as existing_database_error:
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, existing_database_error.exception.code)

        missing_database = path.with_name("missing.sqlite3")
        with self.assertRaises(StorageError) as missing_database_error:
            open_database(missing_database, OpenMode.READ_ONLY)
        self.assertEqual(StorageErrorCode.IO_ERROR, missing_database_error.exception.code)

        no_metadata = path.with_name("no-metadata.sqlite3")
        sqlite3.connect(no_metadata).close()
        with self.assertRaises(StorageError) as no_metadata_error:
            open_database(no_metadata, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, no_metadata_error.exception.code)

        incomplete_schema = path.with_name("incomplete-schema.sqlite3")
        connection = sqlite3.connect(incomplete_schema)
        try:
            connection.execute(
                "CREATE TABLE project_meta (singleton INTEGER, application TEXT, schema_version INTEGER)"
            )
            connection.execute("INSERT INTO project_meta VALUES (1, 'charlie-pinboard', 1)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as incomplete_schema_error:
            open_database(incomplete_schema, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, incomplete_schema_error.exception.code)

        invalid_types = path.with_name("invalid-types.sqlite3")
        invalid_types_connection = sqlite3.connect(invalid_types)
        try:
            invalid_types_connection.execute("CREATE TABLE project_meta (application, schema_version)")
            invalid_types_connection.execute("INSERT INTO project_meta VALUES (7, 'sqlite-v1')")
            invalid_types_connection.commit()
        finally:
            invalid_types_connection.close()
        with self.assertRaises(StorageError) as invalid_types_error:
            open_database(invalid_types, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_types_error.exception.code)

    def test_schema_initialization_and_backup_failures_are_stable(self) -> None:
        path, _store = self._store(populated=False)
        malformed = path.with_name("malformed.sqlite3")
        malformed_connection = sqlite3.connect(malformed)
        try:
            malformed_connection.execute("CREATE TABLE project_meta (application, schema_version)")
            malformed_connection.executemany(
                "INSERT INTO project_meta VALUES (?, ?)",
                (("charlie-pinboard", 1), ("charlie-pinboard", 1)),
            )
            malformed_connection.commit()
        finally:
            malformed_connection.close()
        with self.assertRaises(StorageError) as metadata_error:
            open_database(malformed, OpenMode.READ_WRITE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, metadata_error.exception.code)

        with (
            patch("pathlib.Path.read_bytes", side_effect=OSError("injected schema read failure")),
            self.assertRaises(StorageError) as schema_error,
        ):
            schema_bytes()
        self.assertEqual(StorageErrorCode.IO_ERROR, schema_error.exception.code)

        interrupted_project = Path(tempfile.mkdtemp()).resolve()
        interrupted_roots = resolve_durable_roots(interrupted_project)
        with (
            patch("charlie_pinboard.adapters.sqlite.database.schema_bytes", return_value=b"\xff"),
            self.assertRaises(StorageError) as initialize_error,
        ):
            initialize_database(interrupted_roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, initialize_error.exception.code)
        self.assertFalse(interrupted_roots.database_path.exists())

        existing_backup = path.with_name("existing.sqlite3")
        existing_backup.touch()
        with self.assertRaises(StorageError) as existing_error:
            backup_database(path, existing_backup)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, existing_error.exception.code)

        missing_parent_backup = path.parent / "missing-parent" / "backup.sqlite3"
        with self.assertRaises(StorageError) as backup_error:
            backup_database(path, missing_parent_backup)
        self.assertEqual(StorageErrorCode.IO_ERROR, backup_error.exception.code)
        self.assertFalse(missing_parent_backup.exists())

    def test_database_publication_failures_leave_no_accepted_file(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        with (
            patch(
                "charlie_pinboard.adapters.sqlite.database._sync_database",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "injected database synchronization failure"),
            ),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, synchronization_error.exception.code)
        self.assertFalse(roots.database_path.exists())

        initialize_database(roots, SQLITE_NOW)
        backup = roots.database_path.with_name("interrupted-backup.sqlite3")
        with (
            patch("charlie_pinboard.adapters.sqlite.database.os.open", side_effect=OSError("injected open failure")),
            self.assertRaises(StorageError) as backup_error,
        ):
            backup_database(roots.database_path, backup)
        self.assertEqual(StorageErrorCode.IO_ERROR, backup_error.exception.code)
        self.assertFalse(backup.exists())

    def test_database_cleanup_failures_preserve_typed_errors_and_resume(self) -> None:
        prepublication_project = Path(tempfile.mkdtemp()).resolve()
        prepublication_roots = resolve_durable_roots(prepublication_project)
        with (
            patch("charlie_pinboard.adapters.sqlite.database.schema_bytes", return_value=b"\xff"),
            patch(
                "charlie_pinboard.adapters.sqlite.database.Path.unlink",
                side_effect=OSError("injected pre-publication cleanup failure"),
            ),
            self.assertRaises(StorageError) as prepublication_error,
        ):
            initialize_database(prepublication_roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, prepublication_error.exception.code)
        self.assertFalse(prepublication_roots.database_path.exists())
        initialize_database(prepublication_roots, SQLITE_NOW)

        synchronization_project = Path(tempfile.mkdtemp()).resolve()
        synchronization_roots = resolve_durable_roots(synchronization_project)
        synchronization_failure = StorageError(
            StorageErrorCode.IO_ERROR,
            "injected final database synchronization failure",
        )
        with (
            patch(
                "charlie_pinboard.adapters.sqlite.database._sync_database",
                side_effect=(None, synchronization_failure),
            ),
            patch(
                "charlie_pinboard.adapters.sqlite.database.Path.unlink",
                side_effect=OSError("injected published database cleanup failure"),
            ),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(synchronization_roots, SQLITE_NOW)
        self.assertIs(synchronization_failure, synchronization_error.exception)
        initialize_database(synchronization_roots, SQLITE_NOW)

        backup = synchronization_roots.database_path.with_name("cleanup-retry.sqlite3")
        with (
            patch("charlie_pinboard.adapters.sqlite.database.os.open", side_effect=OSError("injected backup failure")),
            patch(
                "charlie_pinboard.adapters.sqlite.database.Path.unlink",
                side_effect=OSError("injected backup cleanup failure"),
            ),
            self.assertRaises(StorageError) as backup_error,
        ):
            backup_database(synchronization_roots.database_path, backup)
        self.assertEqual(StorageErrorCode.IO_ERROR, backup_error.exception.code)
        self.assertFalse(backup.exists())
        backup_database(synchronization_roots.database_path, backup)
        self.assertEqual(
            SQLiteWorkStore(synchronization_roots.database_path).snapshot(),
            SQLiteWorkStore(backup).snapshot(),
        )

    def test_typed_row_boundary_rejects_malformed_current_state(self) -> None:
        cases = (
            (
                "required timestamp",
                "UPDATE project_meta SET created_at = 'not-a-time'",
                False,
            ),
            (
                "optional timestamp",
                """
                UPDATE attempts SET state = 'review', candidate_revision = 'candidate-a',
                    candidate_recorded_at = 'not-a-time' WHERE attempt_id = 'work-a-1'
                """,
                False,
            ),
            (
                "stored history input JSON",
                "UPDATE transition_history SET input_json = '{' WHERE history_id = 1",
                False,
            ),
            (
                "stored history outcome JSON",
                "UPDATE transition_history SET outcome_json = '{' WHERE history_id = 1",
                False,
            ),
            (
                "attempt generation exceeds its counter",
                "UPDATE attempt_lease_counters SET generation_high_water = 2 WHERE attempt_id = 'work-a-1'",
                False,
            ),
        )
        for name, statement, ignore_constraints in cases:
            path, store = self._store()
            connection = sqlite3.connect(path)
            try:
                if ignore_constraints:
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(statement, (SQLITE_DIGEST, SQLITE_DIGEST) if "?" in statement else ())
                connection.commit()
            finally:
                connection.close()
            with self.subTest(name=name), self.assertRaises(StorageError) as raised:
                store.snapshot()
            self.assertEqual(StorageErrorCode.INVALID_STATE, raised.exception.code)

    def test_durable_root_and_single_file_interruption_contract(self) -> None:
        for failure in range(1, 4):
            project = Path(tempfile.mkdtemp()).resolve()
            roots = resolve_durable_roots(project)
            calls = 0

            def interrupt(_path: Path, *, failure_at: int = failure) -> None:
                nonlocal calls
                calls += 1
                if calls == failure_at:
                    raise FileIOError(
                        FileIOErrorCode.DIRECTORY_SYNC_FAILED,
                        "injected directory synchronization stop",
                    )

            with (
                self.subTest(failure=failure),
                patch("charlie_pinboard.adapters.files.file_io._sync_directory", side_effect=interrupt),
                self.assertRaises(StorageError) as raised,
            ):
                initialize_database(roots, SQLITE_NOW)
            self.assertEqual(StorageErrorCode.IO_ERROR, raised.exception.code)
            self.assertFalse(roots.database_path.exists())
            initialize_database(roots, SQLITE_NOW)
            self.assertTrue(roots.artifacts_root.is_dir())

        external_parent = Path(tempfile.mkdtemp()).resolve()
        external = resolve_durable_roots(external_parent, external_parent / "external-work")
        initialize_database(external, SQLITE_NOW)
        self.assertTrue(external.database_path.exists())
        self.assertTrue(external.artifacts_root.is_dir())

        (external_parent / ".codex").mkdir()
        explicit_legacy = resolve_durable_roots(external_parent, external_parent / ".codex" / "work")
        self.assertEqual(external_parent / ".codex" / "work", explicit_legacy.work_root)

        immutable = external.artifacts_root / "evidence.md"
        created = create_immutable(immutable, b"accepted evidence")
        self.assertEqual((17, b"accepted evidence"), (created.size, immutable.read_bytes()))
        with self.assertRaises(FileIOError):
            create_immutable(immutable, b"replacement")
        replaced = atomic_replace(external.artifacts_root / "view.md", b"revision one")
        self.assertEqual((12, b"revision one"), (replaced.size, (external.artifacts_root / "view.md").read_bytes()))
        atomic_replace(external.artifacts_root / "view.md", b"revision two")
        self.assertEqual(b"revision two", (external.artifacts_root / "view.md").read_bytes())

        immutable_staging = external.artifacts_root / "staged-evidence.md"
        with (
            patch("charlie_pinboard.adapters.files.file_io.secrets.token_hex", return_value="immutable-token"),
            patch("charlie_pinboard.adapters.files.file_io.os.link", wraps=os.link) as linked,
        ):
            create_immutable(immutable_staging, b"staged evidence")
        self.assertEqual(".pinboard-stage-immutable-token", Path(linked.call_args.args[0]).name)

        original_replace = Path.replace

        def replace_staged(source: Path, target: Path) -> Path:
            return original_replace(source, target)

        replace_staging = external.artifacts_root / "staged-view.md"
        with (
            patch("charlie_pinboard.adapters.files.file_io.secrets.token_hex", return_value="replace-token"),
            patch(
                "charlie_pinboard.adapters.files.file_io.Path.replace",
                autospec=True,
                side_effect=replace_staged,
            ) as replaced_path,
        ):
            atomic_replace(replace_staging, b"staged view")
        self.assertEqual(".pinboard-stage-replace-token", Path(replaced_path.call_args.args[0]).name)

    def test_file_boundary_translates_root_and_publication_failures(self) -> None:
        missing_project = Path(tempfile.mkdtemp()).resolve() / "missing-project"
        with self.assertRaises(FileIOError):
            resolve_durable_roots(missing_project)

        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        with (
            patch("charlie_pinboard.adapters.files.file_io.Path.mkdir", side_effect=OSError("injected mkdir failure")),
            self.assertRaises(StorageError) as creation_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, creation_error.exception.code)
        self.assertFalse(roots.database_path.exists())

        with (
            patch("charlie_pinboard.adapters.files.file_io.os.fsync", side_effect=OSError("injected sync failure")),
            self.assertRaises(StorageError) as synchronization_error,
        ):
            initialize_database(roots, SQLITE_NOW)
        self.assertEqual(StorageErrorCode.IO_ERROR, synchronization_error.exception.code)
        self.assertFalse(roots.database_path.exists())

        initialize_database(roots, SQLITE_NOW)
        missing_parent_file = roots.artifacts_root / "missing" / "evidence.md"
        with self.assertRaises(FileIOError):
            create_immutable(missing_parent_file, b"evidence")

        publication = roots.artifacts_root / "publication.md"
        with (
            patch("charlie_pinboard.adapters.files.file_io.os.link", side_effect=OSError("injected link failure")),
            self.assertRaises(FileIOError),
        ):
            create_immutable(publication, b"evidence")
        self.assertFalse(publication.exists())

        cleanup_tolerant = roots.artifacts_root / "cleanup-tolerant.md"
        with patch(
            "charlie_pinboard.adapters.files.file_io.Path.unlink",
            side_effect=OSError("injected staging cleanup failure"),
        ):
            created = create_immutable(cleanup_tolerant, b"durable evidence")
        self.assertEqual((16, b"durable evidence"), (created.size, cleanup_tolerant.read_bytes()))

    def test_complete_stored_state_and_relational_contract_matrix(self) -> None:
        path, store = self._store()
        state = complete_sqlite_state()
        self.assertEqual(state, store.snapshot())
        connection = sqlite3.connect(path)
        try:
            table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(16, table_count)

        wrong_kind = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                item_artifacts=(
                    replace(
                        state.lifecycle.item_artifacts[0], artifact_ref_id=state.artifact_references[0].artifact_ref_id
                    ),
                ),
            ),
        )
        review_items = list(state.lifecycle.work_items)
        review_items[1] = replace(review_items[1], state=StoredWorkItemState.REVIEW)
        review_attempt = replace(state.lifecycle.attempts[0], state=AttemptState.REVIEW)
        review_without_candidate = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(review_items), attempts=(review_attempt,)),
        )
        mismatched_focus = replace(state, focus=replace(state.focus, item_id=ItemId("work-c")))
        for name, candidate in (
            ("artifact kind compatibility", wrong_kind),
            ("review candidate", review_without_candidate),
            ("focus ownership", mismatched_focus),
        ):
            self._assert_state_rejected(name, candidate)

        collected_anchors = replace(
            state,
            authority=replace(state.authority, attempt_generations=(), attempt_leases=()),
        )
        _path, collected_store = self._store(populated=False)
        collected_store.initialize_state(collected_anchors)
        self.assertEqual(collected_anchors, collected_store.snapshot())

        portable = path.with_name("portable.sqlite3")
        backup_database(path, portable)
        connection = open_database(portable, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                for table in (
                    "attempt_leases",
                    "attempt_lease_generations",
                    "attempt_lease_counters",
                    "coordination_lease",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("UPDATE project_meta SET host_epoch = host_epoch + 1, revision = revision + 1")
        finally:
            connection.close()
        portable_state = SQLiteWorkStore(portable).snapshot()
        self.assertEqual(state.lifecycle.attempts, portable_state.lifecycle.attempts)
        self.assertEqual((), portable_state.authority.attempt_leases)

    def test_candidate_and_relational_acceptance_matrix(self) -> None:
        state = complete_sqlite_state()
        same_identity_other_kind = replace(
            state.artifact_references[2],
            artifact_ref_id=ArtifactRefId(4),
            key=state.artifact_references[1].key,
            selector="artifacts/evidence/same-key.md",
        )
        accepted_relational_state = replace(
            state,
            artifact_references=(*state.artifact_references, same_identity_other_kind),
        )
        _accepted_path, accepted_store = self._store(populated=False)
        accepted_store.initialize_state(accepted_relational_state)
        self.assertEqual(accepted_relational_state, accepted_store.snapshot())

        for item_state, attempt_state, candidate, accepted in (
            (StoredWorkItemState.ACTIVE, AttemptState.ACTIVE, "candidate-a", False),
            (StoredWorkItemState.PAUSED, AttemptState.PAUSED, "candidate-a", False),
            (StoredWorkItemState.BLOCKED, AttemptState.BLOCKED, "candidate-a", False),
            (StoredWorkItemState.REVIEW, AttemptState.REVIEW, None, False),
            (StoredWorkItemState.REVIEW, AttemptState.REVIEW, "candidate-a", True),
            (StoredWorkItemState.DONE, AttemptState.DONE, None, True),
            (StoredWorkItemState.DONE, AttemptState.DONE, "candidate-a", True),
        ):
            items = list(state.lifecycle.work_items)
            items[1] = replace(
                items[1],
                state=item_state,
                outcome_evidence="accepted completion" if item_state == StoredWorkItemState.DONE else None,
                queue_position=None if item_state == StoredWorkItemState.DONE else items[1].queue_position,
            )
            if item_state == StoredWorkItemState.DONE:
                items = [
                    replace(value, queue_position=value.queue_position - 1)
                    if value.queue_position is not None and value.queue_position > 2
                    else value
                    for value in items
                ]
            attempt = replace(
                state.lifecycle.attempts[0],
                state=attempt_state,
                candidate_revision=candidate,
                candidate_recorded_at=None if candidate is None else SQLITE_NOW,
            )
            candidate_state = replace(
                state,
                lifecycle=replace(state.lifecycle, work_items=tuple(items), attempts=(attempt,)),
            )
            with self.subTest(item_state=item_state, candidate=candidate, accepted=accepted):
                if accepted:
                    _candidate_path, candidate_store = self._store(populated=False)
                    candidate_store.initialize_state(candidate_state)
                    self.assertEqual(candidate_state, candidate_store.snapshot())
                else:
                    self._assert_state_rejected(f"{item_state.value}/{candidate}", candidate_state)

    def test_domain_decision_commit_staleness_and_failure_rollback(self) -> None:
        _path, store = self._store()
        initial = store.snapshot()
        snapshot = project_decision_snapshot(initial)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.PAUSE)
        decision = decide(
            snapshot,
            bind_transition(action, ReasonInput("Pause at the checkpoint boundary.")),
            SQLITE_NOW,
        )
        mutation = project_transition_mutation(initial, decision)

        with store.write() as transaction:
            self.assertEqual(initial, transaction.snapshot())
            receipt = transaction.commit(mutation)
        committed = store.snapshot()
        self.assertEqual(ActionKind.PAUSE.value, receipt.outcome)
        self.assertEqual(13, committed.lifecycle.project.revision)
        self.assertEqual(StoredWorkItemState.PAUSED, committed.lifecycle.work_items[1].state)
        self.assertEqual(AttemptState.PAUSED, committed.lifecycle.attempts[0].state)
        self.assertEqual(TransitionHistoryActionKind.PAUSE, committed.transition_receipts[-1].action_kind)

        with self.assertRaises(StorageError) as stale, store.write() as transaction:
            transaction.commit(mutation)
        self.assertEqual(StorageErrorCode.STALE_WRITE, stale.exception.code)
        self.assertEqual(committed, store.snapshot())

        stale_subject_decision = replace(
            decision,
            action=replace(decision.action, expected_revision="", subject_revision="stale-subject"),
        )
        with self.assertRaises(StorageError) as stale_subject, store.write() as transaction:
            transaction.commit(replace(mutation, decision=stale_subject_decision))
        self.assertEqual(StorageErrorCode.STALE_WRITE, stale_subject.exception.code)
        self.assertEqual(committed, store.snapshot())

        failed_path, failed_store = self._store()
        failed_initial = failed_store.snapshot()
        failed_snapshot = project_decision_snapshot(failed_initial)
        failed_action = next(
            value
            for value in available_actions(
                failed_snapshot,
                ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, failed_snapshot.generation),
            )
            if value.kind == ActionKind.PAUSE
        )
        failed_decision = decide(
            failed_snapshot,
            bind_transition(failed_action, ReasonInput("This write is interrupted.")),
            SQLITE_NOW,
        )
        failed_mutation = project_transition_mutation(failed_initial, failed_decision)
        connection = open_database(failed_path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute(
                    """
                    CREATE TRIGGER reject_test_history BEFORE INSERT ON transition_history
                    BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
                    """
                )
        finally:
            connection.close()
        with self.assertRaises(StorageError), failed_store.write() as transaction:
            transaction.commit(failed_mutation)
        cleanup = sqlite3.connect(failed_path)
        try:
            cleanup.execute("DROP TRIGGER reject_test_history")
            cleanup.commit()
        finally:
            cleanup.close()
        self.assertEqual(failed_initial, failed_store.snapshot())

    def test_direct_completion_commits_one_domain_decision_atomically(self) -> None:
        _path, store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.COMPLETE)
        decision = decide(
            snapshot,
            bind_transition(action, EvidenceInput("accepted direct completion")),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            receipt = transaction.commit(project_transition_mutation(before, decision))

        completed = store.snapshot()
        item = next(value for value in completed.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = completed.lifecycle.attempts[0]
        self.assertEqual(("complete", "accepted direct completion"), (receipt.outcome, receipt.evidence))
        self.assertEqual((StoredWorkItemState.DONE, "accepted direct completion"), (item.state, item.outcome_evidence))
        self.assertEqual(
            (AttemptState.DONE, None, None), (attempt.state, attempt.candidate_revision, attempt.candidate_recorded_at)
        )
        self.assertEqual(4, completed.authority.attempt_counters[0].generation_high_water)
        self.assertEqual(AttemptLeaseStatus.REVOKED, completed.authority.attempt_leases[0].state)
        self.assertIsNone(completed.focus.item_id)
        self.assertIsNone(completed.focus.attempt_id)

    def test_review_submission_commits_exact_caller_supplied_candidate(self) -> None:
        _path, store = self._store()
        before = store.snapshot()
        snapshot = project_decision_snapshot(before)
        actor = ActorAuthority(
            Role.WORKER,
            AuthorizationKind.ATTEMPT,
            3,
            LeaseId("attempt-lease-a"),
            (AttemptId("work-a-1"),),
            False,
        )
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.SUBMIT_REVIEW)
        candidate = CandidateId("candidate-from-caller")
        decision = decide(
            snapshot,
            bind_transition(action, SubmitReviewInput(candidate)),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(before, decision))

        committed = store.snapshot()
        attempt = committed.lifecycle.attempts[0]
        self.assertEqual((AttemptState.REVIEW, candidate), (attempt.state, attempt.candidate_revision))
        self.assertEqual(SQLITE_NOW + timedelta(seconds=1), attempt.candidate_recorded_at)
        self.assertIn(b'"candidate":"candidate-from-caller"', committed.transition_receipts[-1].outcome_payload)

    def test_review_return_clears_candidate_and_fences_mutation_authority(self) -> None:
        state = complete_sqlite_state()
        items = list(state.lifecycle.work_items)
        items[1] = replace(items[1], state=StoredWorkItemState.REVIEW)
        attempt = replace(
            state.lifecycle.attempts[0],
            state=AttemptState.REVIEW,
            candidate_revision="candidate-a",
            candidate_recorded_at=SQLITE_NOW,
        )
        review_state = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(items), attempts=(attempt,)),
        )
        _path, store = self._store(populated=False)
        store.initialize_state(review_state)
        snapshot = project_decision_snapshot(store.snapshot())
        coordination = review_state.authority.coordination
        assert coordination is not None
        actor = ActorAuthority(
            Role.COORDINATOR,
            AuthorizationKind.COORDINATION,
            coordination.generation,
            coordination.lease_id,
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == ActionKind.RETURN_FOR_CORRECTION
        )
        decision = decide(
            snapshot,
            bind_transition(action, ReasonInput("Address the review findings.")),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            transaction.commit(project_transition_mutation(review_state, decision))

        returned = store.snapshot()
        returned_attempt = returned.lifecycle.attempts[0]
        self.assertEqual(
            (AttemptState.ACTIVE, None, None),
            (
                returned_attempt.state,
                returned_attempt.candidate_revision,
                returned_attempt.candidate_recorded_at,
            ),
        )
        self.assertEqual(4, returned.authority.attempt_counters[0].generation_high_water)
        self.assertEqual("revoked", returned.authority.attempt_leases[0].state.value)


if __name__ == "__main__":
    unittest.main()
