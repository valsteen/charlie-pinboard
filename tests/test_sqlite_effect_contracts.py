import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import lifecycle, proposals
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.artifacts import accept_checkpoint_artifact
from pinboard.adapters.sqlite.authority import (
    validate_attempt_authority,
    write_attempt_authority,
    write_coordination_authority,
)
from pinboard.adapters.sqlite.database import initialize_database, open_database, write_transaction
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import EvidenceArtifactRef
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_models import AttemptAuthorityDecision, AttemptLeaseAuthority, AttemptLeaseStatus
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId, ProposalId
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class SQLiteEffectContractTest(unittest.TestCase):
    def _store(self) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        return roots.database_path, store

    def test_checkpoint_artifact_identity_is_exact(self) -> None:
        path, store = self._store()
        state = store.snapshot()
        existing = next(
            value for value in state.artifact_references if value.kind == stored_state.ArtifactKind.EVIDENCE
        )
        published = EvidenceArtifactRef(
            existing.key,
            existing.revision,
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            self.assertEqual(
                existing.artifact_ref_id,
                accept_checkpoint_artifact(
                    connection,
                    state,
                    published,
                    existing.artifact_ref_id,
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                ),
            )
            with self.assertRaises(StorageError) as conflicting:
                accept_checkpoint_artifact(
                    connection,
                    state,
                    replace(published, content_sha256="0" * 64),
                    ArtifactRefId(int(existing.artifact_ref_id) + 1),
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, conflicting.exception.code)
        finally:
            connection.close()

    def test_complete_state_rejects_missing_project_invalid_queue_and_reinitialization(self) -> None:
        path, store = self._store()
        before = store.snapshot()

        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN")
            raw.execute("DELETE FROM project_meta")
            with self.assertRaises(StorageError) as missing_project:
                sqlite_state.read_state(raw)
            self.assertEqual(StorageErrorCode.INVALID_STATE, missing_project.exception.code)
            raw.rollback()
        finally:
            raw.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute("UPDATE work_items SET queue_position = 99 WHERE queue_position = 1")
                with self.assertRaises(StorageError) as invalid_queue:
                    sqlite_state.read_state(connection)
                self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_queue.exception.code)
                connection.rollback()
        finally:
            connection.close()

        with self.assertRaises(StorageError) as occupied:
            initialize_store(store, before)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, occupied.exception.code)
        self.assertEqual(before, store.snapshot())

    def test_lifecycle_lookup_and_cas_failures_preserve_the_ledger(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        with self.assertRaises(StorageError) as missing_item:
            lifecycle.item(before, ItemId("missing"))
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, missing_item.exception.code)
        with self.assertRaises(StorageError) as missing_attempt:
            lifecycle.attempt(before, AttemptId("missing-1"))
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, missing_attempt.exception.code)

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale_focus = lifecycle.update_focus(
                connection,
                replace(before.focus, subject_revision=-1),
                replace(before.focus, subject_revision=before.focus.subject_revision + 1),
            )
        finally:
            connection.close()
        self.assertIsInstance(stale_focus, DecisionFailure)
        self.assertEqual(before, store.snapshot())

        for effect, argument in ((lifecycle.compact_queue, 1), (lifecycle.make_queue_space, 1)):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute(
                    """
                    CREATE TEMP TRIGGER arrange_real_queue_staleness
                    BEFORE UPDATE OF queue_position ON work_items
                    BEGIN
                        SELECT RAISE(IGNORE);
                    END
                    """
                )
                with write_transaction(connection):
                    result = effect(connection, before, argument)
            finally:
                connection.close()
            self.assertIsInstance(result, DecisionFailure)
            self.assertEqual(before, store.snapshot())

    def test_proposal_decode_and_stale_disposition_contracts_are_explicit(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        proposal_id = before.proposals.proposals[0].proposal_id
        for statement, parameters in (
            (
                "UPDATE proposals SET relation_kind = 'follow-up', relation_item_id = NULL WHERE proposal_id = ?",
                (proposal_id,),
            ),
            (
                """
                UPDATE proposals
                SET disposition = 'accepted', disposition_target_item_id = NULL,
                    disposition_recorded_at = ?
                WHERE proposal_id = ?
                """,
                (SQLITE_NOW.isoformat(), proposal_id),
            ),
        ):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                with write_transaction(connection):
                    connection.execute(statement, parameters)
                    with self.assertRaises(StorageError) as malformed:
                        sqlite_state.read_state(connection)
                    self.assertEqual(StorageErrorCode.INVALID_STATE, malformed.exception.code)
                    connection.rollback()
            finally:
                connection.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale = proposals.set_proposal_disposition(
                connection,
                ProposalId("missing-proposal"),
                work_models.ReturnedProposalDisposition("No longer relevant.", SQLITE_NOW),
                before.lifecycle.project.revision + 1,
            )
        finally:
            connection.close()
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before, store.snapshot())

    def test_authority_validation_and_coordination_insert_staleness_are_distinct(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        without_counter = replace(
            before,
            authority=replace(before.authority, attempt_counters=()),
        )
        with self.assertRaises(StorageError) as invalid_generation:
            validate_attempt_authority(without_counter, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_generation.exception.code)

        mismatched_lease = replace(
            before,
            authority=replace(
                before.authority,
                attempt_leases=(
                    replace(
                        before.authority.attempt_leases[0],
                        generation=before.authority.attempt_leases[0].generation - 1,
                    ),
                ),
            ),
        )
        with self.assertRaises(StorageError) as invalid_lease:
            validate_attempt_authority(mismatched_lease, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_lease.exception.code)

        coordination = project_decision_snapshot(before, SQLITE_NOW).coordination_lease
        assert coordination is not None
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale = write_coordination_authority(
                connection,
                None,
                replace(coordination, expires_at=coordination.expires_at + timedelta(minutes=1)),
                "The coordination authority already exists.",
            )
        finally:
            connection.close()
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before, store.snapshot())

    def test_expected_insert_key_conflicts_are_stale_but_other_constraints_are_exceptional(self) -> None:
        path, store = self._store()
        before = store.snapshot()
        attempt = before.lifecycle.attempts[0]
        duplicate_attempt = decision_models.ActivationChange(
            ItemId("work-c"),
            work_models.WorkState.READY,
            attempt.attempt_id,
            attempt.brief_artifact_ref_id,
            "duplicate-attempt",
            "base",
            "worker",
        )
        unrelated_live_attempt_conflict = replace(
            duplicate_attempt,
            item=attempt.item_id,
            attempt=AttemptId("other-live-attempt"),
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                stale_attempt = lifecycle.insert_attempt(
                    connection,
                    before,
                    duplicate_attempt,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertIsInstance(stale_attempt, DecisionFailure)

            with self.assertRaises(StorageError) as unrelated_unique, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    before,
                    unrelated_live_attempt_conflict,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_unique.exception.code)

            with self.assertRaises(StorageError) as unrelated_foreign_key, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    before,
                    replace(
                        duplicate_attempt,
                        item=ItemId("missing-item"),
                        attempt=AttemptId("missing-item-attempt"),
                    ),
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_foreign_key.exception.code)

            coordination = project_decision_snapshot(before, SQLITE_NOW).coordination_lease
            assert coordination is not None
            with self.assertRaises(StorageError) as unrelated_check, write_transaction(connection):
                connection.execute("DELETE FROM coordination_lease")
                write_coordination_authority(
                    connection,
                    None,
                    replace(coordination, generation=0),
                    "The coordination authority already exists.",
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_check.exception.code)

            retained = before.authority.attempt_leases[0]
            command = project_decision_snapshot(before, SQLITE_NOW).command_attempt_authorities[0]
            current = AttemptLeaseAuthority(
                command.host_epoch,
                command.attempt,
                command.item,
                command.task_id,
                command.host_id,
                command.lease_id,
                command.generation,
                retained.acquired_at,
                command.expires_at,
                AttemptLeaseStatus.ACTIVE,
            )
            duplicate_current = AttemptAuthorityDecision(
                command.attempt,
                command.generation,
                command.generation,
                None,
                current,
            )
            with write_transaction(connection):
                stale_current = write_attempt_authority(connection, duplicate_current)
            self.assertIsInstance(stale_current, DecisionFailure)

            missing_attempt = AttemptId("missing-attempt")
            invalid_counter = AttemptAuthorityDecision(
                missing_attempt,
                0,
                -1,
                None,
                replace(current, attempt=missing_attempt),
            )
            with self.assertRaises(StorageError) as unrelated_counter_check, write_transaction(connection):
                write_attempt_authority(connection, invalid_counter)
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_counter_check.exception.code)
        finally:
            connection.close()
        self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
