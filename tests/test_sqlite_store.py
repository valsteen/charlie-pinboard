import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

from charlie_pinboard.adapters.files.file_io import (
    FileIOError,
    atomic_replace,
    create_immutable,
    resolve_durable_roots,
)
from charlie_pinboard.adapters.sqlite.database import (
    OpenMode,
    StorageError,
    StorageErrorCode,
    backup_database,
    initialize_database,
    open_database,
    read_operation,
    schema_bytes,
    write_transaction,
)
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.stored_state import (
    ItemScopeRevision,
    MutationIntentState,
    PlanningObligationState,
    ResourceInstanceState,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
)
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
)
from charlie_pinboard.domain.decisions import (
    available_actions as available_actions_outcome,
)
from charlie_pinboard.domain.decisions import (
    bind_transition as bind_transition_outcome,
)
from charlie_pinboard.domain.decisions import (
    decide as decision_outcome,
)
from charlie_pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    ItemId,
    LeaseId,
    MutationIntentId,
    ReservationId,
)
from charlie_pinboard.domain.model import (
    AttemptState,
    EvidenceInput,
    LedgerSnapshot,
    PlanningDisposition,
    ReasonInput,
    SubmitReviewInput,
    TransitionInput,
    UseLeaseState,
)
from charlie_pinboard.domain.resource_decisions import (
    ResourceDecision,
    ResourceUseLeaseChange,
    reallocate_resource,
    revoke_resource,
)
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    return cast(tuple[Action, ...], available_actions_outcome(snapshot, actor))


def bind_transition(action: Action, value: TransitionInput) -> TransitionCommand:
    return cast(TransitionCommand, bind_transition_outcome(action, value))


def decide(snapshot: LedgerSnapshot, command: TransitionCommand, now: datetime) -> Decision:
    return cast(Decision, decision_outcome(snapshot, command, now))


class SQLiteStoreTest(unittest.TestCase):
    def _store(self, *, populated: bool = True) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if populated:
            store.initialize_state(complete_sqlite_state())
        return roots.database_path, store

    def _assert_state_rejected(self, state_name: str, state: StoredWorkState) -> None:
        path, store = self._store(populated=False)
        self.assertTrue(path.exists())
        with self.subTest(state_name=state_name), self.assertRaises(StorageError) as raised:
            store.initialize_state(state)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, raised.exception.code, state_name)
        self.assertEqual(0, store.snapshot().lifecycle.project.revision)

    def test_schema_identity_initialization_backup_and_reopen_contract(self) -> None:
        accepted = Path(".codex/topics/sqlite-storage/schema-v1.sql").read_bytes()
        self.assertEqual(accepted, schema_bytes())

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
            ("schema_version", 0, StorageErrorCode.MIGRATION_REQUIRED),
            ("schema_version", 2, StorageErrorCode.SCHEMA_TOO_NEW),
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

        self.assertEqual(StorageErrorCode.SCHEMA_TOO_NEW, newer_error.exception.code)
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
                "required canonical JSON",
                "UPDATE resource_instance_locators SET locator_json = '{' WHERE instance_id = 'workspace-on-host'",
                False,
            ),
            (
                "optional canonical JSON",
                """
                UPDATE resource_mutation_intents SET status = 'accepted', resolved_at = '2026-08-22T14:00:00+00:00',
                    result_observation_generation = 3, result_observation_digest = ?,
                    evidence_schema = 'mutation-evidence/v1', evidence_json = '{', evidence_digest = ?
                WHERE intent_id = 'intent-a'
                """,
                False,
            ),
            (
                "closed vocabulary",
                "UPDATE resource_instances SET status = 'unknown' WHERE instance_id = 'workspace-on-host'",
                True,
            ),
            (
                "declared relational literal",
                "UPDATE resources SET scope = 'host-local' WHERE resource_id = 'workspace'",
                True,
            ),
            (
                "attempt generation exceeds its counter",
                "UPDATE attempt_lease_counters SET generation_high_water = 2 WHERE attempt_id = 'work-a-1'",
                False,
            ),
            (
                "reservation generation exceeds its counter",
                "UPDATE resource_reservation_counters SET generation_high_water = 0 WHERE instance_id = 'workspace-on-host'",
                False,
            ),
            (
                "active use lease host epoch",
                "UPDATE resource_use_leases SET host_epoch = 99 WHERE reservation_id = 'reservation-a' AND generation = 3",
                False,
            ),
            (
                "active use lease instance revision",
                "UPDATE resource_use_leases SET instance_subject_revision = 99 WHERE reservation_id = 'reservation-a' AND generation = 3",
                False,
            ),
            (
                "active use lease locator observation",
                "UPDATE resource_use_leases SET observation_generation = 99 WHERE reservation_id = 'reservation-a' AND generation = 3",
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
                    raise FileIOError("injected directory synchronization stop")

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

        immutable = external.artifacts_root / "evidence.md"
        created = create_immutable(immutable, b"accepted evidence")
        self.assertEqual((17, b"accepted evidence"), (created.size, immutable.read_bytes()))
        with self.assertRaises(FileIOError):
            create_immutable(immutable, b"replacement")
        replaced = atomic_replace(external.artifacts_root / "view.md", b"revision one")
        self.assertEqual((12, b"revision one"), (replaced.size, (external.artifacts_root / "view.md").read_bytes()))
        atomic_replace(external.artifacts_root / "view.md", b"revision two")
        self.assertEqual(b"revision two", (external.artifacts_root / "view.md").read_bytes())

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
        self.assertEqual(27, table_count)

        native_items = list(state.lifecycle.work_items)
        native_items[1] = replace(native_items[1], source=None)
        wrong_origin = replace(state, lifecycle=replace(state.lifecycle, work_items=tuple(native_items)))

        wrong_kind = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                item_artifacts=(
                    replace(
                        state.lifecycle.item_artifacts[0], artifact_ref_id=state.artifacts.references[0].artifact_ref_id
                    ),
                ),
            ),
        )
        review_items = list(state.lifecycle.work_items)
        review_items[1] = replace(review_items[1], state=StoredWorkItemState.REVIEW)
        review_attempt = replace(state.lifecycle.attempts[0], state=AttemptState.REVIEW)
        native_review_without_candidate = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(review_items), attempts=(review_attempt,)),
        )
        mismatched_focus = replace(state, focus=replace(state.focus, item_id=ItemId("work-c")))
        missing_target = replace(
            state,
            planning=replace(state.planning, obligations=(), replacements=()),
        )
        fence_intent = replace(
            state,
            resources=replace(
                state.resources,
                mutation_intents=(
                    replace(
                        state.resources.mutation_intents[0],
                        resource_use_generation=2,
                        resource_use_lease_id=state.resources.use_leases[1].lease_id,
                    ),
                ),
            ),
        )
        crosswired_attempt = replace(
            state,
            resources=replace(
                state.resources,
                mutation_intents=(replace(state.resources.mutation_intents[0], attempt_lease_generation=2),),
            ),
        )
        second_reservation = replace(
            state.resources.reservations[0],
            reservation_id=ReservationId("reservation-b"),
            instance_id=state.resources.instances[0].instance_id,
        )
        conflicting_reservation = replace(
            state,
            resources=replace(
                state.resources,
                reservation_counters=(
                    replace(state.resources.reservation_counters[0], generation_high_water=1),
                    state.resources.reservation_counters[1],
                ),
                reservations=(*state.resources.reservations, second_reservation),
            ),
        )
        for name, candidate in (
            ("native origin completeness", wrong_origin),
            ("artifact kind compatibility", wrong_kind),
            ("native review candidate", native_review_without_candidate),
            ("focus ownership", mismatched_focus),
            ("planning target required", missing_target),
            ("fence cannot authorize intent", fence_intent),
            ("intent attempt authority exact", crosswired_attempt),
            ("one reservation per requirement", conflicting_reservation),
        ):
            self._assert_state_rejected(name, candidate)

        collected_anchors = replace(
            state,
            authority=replace(state.authority, attempt_generations=(), attempt_leases=()),
            resources=replace(state.resources, use_leases=(), mutation_intents=()),
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
                    "resource_mutation_intents",
                    "resource_use_leases",
                    "resource_reservations",
                    "resource_reservation_counters",
                    "resource_instance_locators",
                    "resource_instances",
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
        self.assertEqual(state.resources.definitions, portable_state.resources.definitions)
        self.assertEqual(state.resources.requirements, portable_state.resources.requirements)
        self.assertEqual(state.lifecycle.attempts, portable_state.lifecycle.attempts)
        self.assertEqual((), portable_state.resources.instances)
        self.assertEqual((), portable_state.resources.mutation_intents)
        self.assertEqual((), portable_state.authority.attempt_leases)

    def test_authority_intent_and_planning_rejection_matrix(self) -> None:
        state = complete_sqlite_state()
        valid_older_anchor = replace(
            state.authority.attempt_generations[0],
            generation=2,
            lease_id=LeaseId("attempt-lease-older"),
        )
        crosswired_valid_attempt = replace(
            state,
            authority=replace(
                state.authority,
                attempt_generations=(valid_older_anchor, *state.authority.attempt_generations),
            ),
            resources=replace(
                state.resources,
                mutation_intents=(
                    replace(
                        state.resources.mutation_intents[0],
                        attempt_lease_id=valid_older_anchor.lease_id,
                        attempt_lease_generation=valid_older_anchor.generation,
                    ),
                ),
            ),
        )
        duplicate_planned_intent = replace(
            state,
            resources=replace(
                state.resources,
                mutation_intents=(
                    *state.resources.mutation_intents,
                    replace(state.resources.mutation_intents[0], intent_id=MutationIntentId("intent-b")),
                ),
            ),
        )
        partial_intent_evidence = replace(
            state,
            resources=replace(
                state.resources,
                mutation_intents=(
                    replace(
                        state.resources.mutation_intents[0],
                        state=MutationIntentState.ACCEPTED,
                        resolved_at=SQLITE_NOW,
                        result_observation_generation=3,
                        result_observation_digest=SQLITE_DIGEST,
                        evidence_schema="mutation-evidence/v1",
                    ),
                ),
            ),
        )
        missing_primary_replacement = replace(state, planning=replace(state.planning, replacements=()))

        low_attempt_counter = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(replace(state.authority.attempt_counters[0], generation_high_water=2),),
            ),
        )
        low_reservation_counter = replace(
            state,
            resources=replace(
                state.resources,
                reservation_counters=(
                    state.resources.reservation_counters[0],
                    replace(state.resources.reservation_counters[1], generation_high_water=0),
                ),
            ),
        )
        active_use = state.resources.use_leases[2]
        stale_host_epoch = replace(
            state,
            resources=replace(
                state.resources,
                use_leases=(*state.resources.use_leases[:2], replace(active_use, host_epoch=99)),
            ),
        )
        stale_instance_revision = replace(
            state,
            resources=replace(
                state.resources,
                use_leases=(*state.resources.use_leases[:2], replace(active_use, instance_subject_revision=99)),
            ),
        )
        stale_observation = replace(
            state,
            resources=replace(
                state.resources,
                use_leases=(*state.resources.use_leases[:2], replace(active_use, observation_generation=99)),
            ),
        )
        next_attempt_anchor = replace(
            state.authority.attempt_generations[0],
            generation=4,
            lease_id=LeaseId("attempt-lease-current"),
        )
        superseded_attempt_authority = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(replace(state.authority.attempt_counters[0], generation_high_water=4),),
                attempt_generations=(*state.authority.attempt_generations, next_attempt_anchor),
                attempt_leases=(replace(state.authority.attempt_leases[0], generation=4),),
            ),
        )

        for name, candidate in (
            ("valid attempt anchors cannot be cross-wired", crosswired_valid_attempt),
            ("one planned intent per grant", duplicate_planned_intent),
            ("resolved intent evidence is complete", partial_intent_evidence),
            ("superseded obligation needs a primary replacement", missing_primary_replacement),
            ("attempt counter fences every retained generation", low_attempt_counter),
            ("reservation counter fences every acquisition", low_reservation_counter),
            ("active task use matches the host epoch", stale_host_epoch),
            ("active task use matches the instance revision", stale_instance_revision),
            ("active task use matches the locator observation", stale_observation),
            ("active task use matches current attempt authority", superseded_attempt_authority),
        ):
            self._assert_state_rejected(name, candidate)

    def test_planning_shape_acceptance_matrix(self) -> None:
        state = complete_sqlite_state()
        obligation = state.planning.obligations[0]
        impact = state.planning.impacts[0]
        item_c = ItemId("work-c")

        unresolved = replace(
            state,
            planning=replace(
                state.planning,
                impacts=(replace(impact, primary_target_item_id=item_c),),
                obligations=(
                    StoredPlanningObligation(
                        impact.impact_id,
                        item_c,
                        0,
                        1,
                        SQLITE_DIGEST,
                        PlanningObligationState.UNRESOLVED,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        SQLITE_NOW,
                        None,
                    ),
                ),
                replacements=(),
            ),
        )

        revised_digest = "b" * 64
        revised_items = list(state.lifecycle.work_items)
        revised_items[3] = replace(revised_items[3], scope_revision=2, scope_digest=revised_digest)
        revised = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(revised_items),
                scope_revisions=(
                    *state.lifecycle.scope_revisions,
                    ItemScopeRevision(item_c, 2, revised_digest, 7, SQLITE_NOW),
                ),
            ),
            planning=replace(
                state.planning,
                impacts=(replace(impact, primary_target_item_id=item_c),),
                obligations=(
                    replace(
                        obligation,
                        target_item_id=item_c,
                        state=PlanningObligationState.RESOLVED,
                        disposition=PlanningDisposition.REVISED,
                        evaluated_scope_revision=1,
                        evaluated_scope_digest=SQLITE_DIGEST,
                        resulting_scope_revision=2,
                        resulting_scope_digest=revised_digest,
                        primary_replacement_item_id=None,
                        outcome_evidence=None,
                    ),
                ),
                replacements=(),
            ),
        )

        shaped_items = list(state.lifecycle.work_items)
        shaped_items[3] = replace(
            shaped_items[3],
            state=StoredWorkItemState.SUPERSEDED,
            outcome_evidence="work-c superseded",
        )
        converging_obligation = replace(
            obligation,
            target_item_id=item_c,
            position=1,
            primary_replacement_item_id=ItemId("work-a"),
            outcome_evidence="work-c superseded",
        )
        shaped = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=tuple(shaped_items)),
            planning=replace(
                state.planning,
                obligations=(obligation, converging_obligation),
                replacements=(
                    StoredPlanningReplacement(impact.impact_id, ItemId("work-b"), ItemId("work-c"), 0),
                    StoredPlanningReplacement(impact.impact_id, ItemId("work-b"), ItemId("work-a"), 1),
                    StoredPlanningReplacement(impact.impact_id, item_c, ItemId("work-a"), 0),
                ),
            ),
        )

        for name, candidate in (
            ("unresolved target", unresolved),
            ("revised target scope", revised),
            ("one-to-many and many-to-one supersession", shaped),
        ):
            with self.subTest(name=name):
                _path, store = self._store(populated=False)
                store.initialize_state(candidate)
                self.assertEqual(candidate, store.snapshot())

    def test_candidate_and_relational_acceptance_matrix(self) -> None:
        state = complete_sqlite_state()
        accepted_intent = replace(
            state.resources.mutation_intents[0],
            state=MutationIntentState.ACCEPTED,
            resolved_at=SQLITE_NOW,
            result_observation_generation=3,
            result_observation_digest=SQLITE_DIGEST,
            evidence_schema="mutation-evidence/v1",
            evidence=state.resources.mutation_intents[0].policy,
            evidence_digest=SQLITE_DIGEST,
        )
        same_identity_other_kind = replace(
            state.artifacts.references[2],
            artifact_ref_id=ArtifactRefId(4),
            key=state.artifacts.references[1].key,
            selector="artifacts/evidence/same-key.md",
        )
        accepted_relational_state = replace(
            state,
            artifacts=replace(
                state.artifacts,
                references=(*state.artifacts.references, same_identity_other_kind),
            ),
            resources=replace(state.resources, mutation_intents=(accepted_intent,)),
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
            )
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

        with store.write() as transaction:
            self.assertEqual(initial, transaction.snapshot())
            receipt = transaction.commit(decision)
        committed = store.snapshot()
        self.assertEqual(ActionKind.PAUSE.value, receipt.outcome)
        self.assertEqual(13, committed.lifecycle.project.revision)
        self.assertEqual(StoredWorkItemState.PAUSED, committed.lifecycle.work_items[1].state)
        self.assertEqual(AttemptState.PAUSED, committed.lifecycle.attempts[0].state)
        self.assertEqual(TransitionHistoryActionKind.PAUSE, committed.history.receipts[-1].action_kind)
        self.assertEqual(initial.resources, committed.resources)

        with self.assertRaises(StorageError) as stale, store.write() as transaction:
            transaction.commit(decision)
        self.assertEqual(StorageErrorCode.STALE_WRITE, stale.exception.code)
        self.assertEqual(committed, store.snapshot())

        stale_subject_decision = replace(
            decision,
            action=replace(decision.action, expected_revision="", subject_revision="stale-subject"),
        )
        with self.assertRaises(StorageError) as stale_subject, store.write() as transaction:
            transaction.commit(stale_subject_decision)
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
            transaction.commit(failed_decision)
        cleanup = sqlite3.connect(failed_path)
        try:
            cleanup.execute("DROP TRIGGER reject_test_history")
            cleanup.commit()
        finally:
            cleanup.close()
        self.assertEqual(failed_initial, failed_store.snapshot())

    def test_direct_completion_commits_one_domain_decision_atomically(self) -> None:
        _path, store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
        action = next(value for value in available_actions(snapshot, actor) if value.kind == ActionKind.COMPLETE)
        decision = decide(
            snapshot,
            bind_transition(action, EvidenceInput("accepted direct completion")),
            SQLITE_NOW + timedelta(seconds=1),
        )

        with store.write() as transaction:
            receipt = transaction.commit(decision)

        completed = store.snapshot()
        item = next(value for value in completed.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = completed.lifecycle.attempts[0]
        reservation = completed.resources.reservations[0]
        use_leases = completed.resources.use_leases
        self.assertEqual(("complete", "accepted direct completion"), (receipt.outcome, receipt.evidence))
        self.assertEqual((StoredWorkItemState.DONE, "accepted direct completion"), (item.state, item.outcome_evidence))
        self.assertEqual(
            (AttemptState.DONE, None, None), (attempt.state, attempt.candidate_revision, attempt.candidate_recorded_at)
        )
        self.assertEqual("released", reservation.state.value)
        self.assertEqual(
            ((1, "revoked"), (2, "revoked"), (3, "released")),
            tuple((value.generation, value.state.value) for value in use_leases),
        )
        self.assertEqual(complete_sqlite_state().authority, completed.authority)
        self.assertIsNone(completed.focus.item_id)
        self.assertIsNone(completed.focus.attempt_id)

    def test_review_submission_commits_exact_caller_supplied_candidate(self) -> None:
        _path, store = self._store()
        snapshot = project_decision_snapshot(store.snapshot())
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
            transaction.commit(decision)

        committed = store.snapshot()
        attempt = committed.lifecycle.attempts[0]
        self.assertEqual((AttemptState.REVIEW, candidate), (attempt.state, attempt.candidate_revision))
        self.assertEqual(SQLITE_NOW + timedelta(seconds=1), attempt.candidate_recorded_at)
        self.assertIn(b'"candidate":"candidate-from-caller"', committed.history.receipts[-1].outcome_payload)

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
            transaction.commit(decision)

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
        self.assertEqual(
            ((1, "revoked"), (2, "revoked"), (3, "revoked"), (4, "revoked")),
            tuple((value.generation, value.state.value) for value in returned.resources.use_leases),
        )
        self.assertEqual("active", returned.resources.reservations[0].state.value)
        self.assertEqual(
            ((1, "grant", "revoked"), (2, "fence", "revoked"), (3, "grant", "revoked"), (4, "fence", "revoked")),
            tuple(
                (value.generation, value.generation_kind.value, value.state.value)
                for value in returned.resources.use_leases
            ),
        )

    def test_resource_revocation_and_reallocation_persist_domain_decisions(self) -> None:
        _path, revocation_store = self._store()
        revocation_snapshot = project_decision_snapshot(revocation_store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, revocation_snapshot.generation)
        action = next(
            value for value in available_actions(revocation_snapshot, actor) if value.kind == ActionKind.PAUSE
        )
        base_decision = decide(
            revocation_snapshot,
            bind_transition(action, ReasonInput("Persist the resource decision.")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        revoked = cast(
            ResourceDecision,
            revoke_resource(revocation_snapshot, ReservationId("reservation-a"), unresolved_intent=True),
        )
        active_use = revocation_snapshot.resource_use_leases[-1]
        revoke_decision = replace(
            base_decision,
            item_change=None,
            attempt_change=None,
            reservation_changes=revoked.changes,
            reservation_counter_changes=revoked.counter_changes,
            resource_use_lease_changes=(
                ResourceUseLeaseChange(active_use, replace(active_use, state=UseLeaseState.REVOKED)),
            ),
        )
        with revocation_store.write() as transaction:
            transaction.commit(revoke_decision)
        revoked_state = revocation_store.snapshot()
        self.assertEqual(2, revoked_state.resources.reservation_counters[1].generation_high_water)
        self.assertEqual("revoked-pending-recovery", revoked_state.resources.reservations[0].state.value)
        self.assertEqual((1, 2, 3, 4), tuple(value.generation for value in revoked_state.resources.use_leases))

        state = complete_sqlite_state()
        active_instances = (
            replace(state.resources.instances[0], state=ResourceInstanceState.ACTIVE),
            state.resources.instances[1],
        )
        reallocatable = replace(state, resources=replace(state.resources, instances=active_instances))
        _path, reallocation_store = self._store(populated=False)
        reallocation_store.initialize_state(reallocatable)
        reallocation_snapshot = project_decision_snapshot(reallocation_store.snapshot())
        actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, reallocation_snapshot.generation)
        action = next(
            value for value in available_actions(reallocation_snapshot, actor) if value.kind == ActionKind.PAUSE
        )
        base_decision = decide(
            reallocation_snapshot,
            bind_transition(action, ReasonInput("Persist the resource reallocation.")),
            SQLITE_NOW + timedelta(seconds=1),
        )
        reallocated = cast(
            ResourceDecision,
            reallocate_resource(
                reallocation_snapshot,
                ReservationId("reservation-a"),
                replacement_id=ReservationId("reservation-b"),
                instance_id=active_instances[0].instance_id,
                generation=1,
            ),
        )
        active_use = reallocation_snapshot.resource_use_leases[-1]
        reallocate_decision = replace(
            base_decision,
            item_change=None,
            attempt_change=None,
            reservation_changes=reallocated.changes,
            reservation_counter_changes=reallocated.counter_changes,
            resource_use_lease_changes=(
                ResourceUseLeaseChange(active_use, replace(active_use, state=UseLeaseState.RELEASED)),
            ),
        )
        with reallocation_store.write() as transaction:
            transaction.commit(reallocate_decision)
        reallocated_state = reallocation_store.snapshot()
        self.assertEqual(
            (("reservation-a", "released"), ("reservation-b", "active")),
            tuple((value.reservation_id, value.state.value) for value in reallocated_state.resources.reservations),
        )
        self.assertEqual(1, reallocated_state.resources.reservation_counters[0].generation_high_water)


if __name__ == "__main__":
    unittest.main()
