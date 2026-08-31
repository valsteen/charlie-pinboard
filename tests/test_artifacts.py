import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import verify_reference, write_revision
from pinboard.adapters.files.errors import (
    ArtifactError,
    ArtifactErrorCode,
    FileIOError,
    FileIOErrorCode,
)
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRelationship, NewArtifact
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, reject_table_deletes


class ArtifactPersistenceTest(unittest.TestCase):
    def test_accepting_relationship_preserves_canonical_global_link_order(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "intake-work-review", 1, ".md", b"ready\n"),
        )
        before = store.snapshot()

        with reject_table_deletes("work_items"):
            accepted = expect_success(
                store.accept_artifact_reference(
                    roots.work_root,
                    published,
                    SQLITE_NOW,
                    relationship=ArtifactRelationship(ItemId("intake-work"), work_models.ArtifactRole.EVIDENCE),
                )
            )

        reloaded = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertIn(accepted, reloaded.artifact_references)
        self.assertEqual(before.transition_receipts, reloaded.transition_receipts)
        self.assertEqual(
            ["intake-work", "work-a"],
            [str(value.item_id) for value in reloaded.lifecycle.item_artifacts],
        )

    def test_accepting_transaction_verifies_bytes_and_fresh_reload_contains_reference(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "review-a", 1, ".md", b"ready\n"),
        )

        accepted = expect_success(
            store.accept_artifact_reference(
                roots.work_root,
                published,
                SQLITE_NOW,
                relationship=ArtifactRelationship(ItemId("work-a"), work_models.ArtifactRole.EVIDENCE),
            )
        )

        reloaded = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertIn(accepted, reloaded.artifact_references)
        self.assertEqual(13, reloaded.lifecycle.project.revision)
        self.assertTrue(
            any(link.artifact_ref_id == accepted.artifact_ref_id for link in reloaded.lifecycle.item_artifacts)
        )

    def test_revision_is_published_immutably_and_identical_retry_is_reused(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        artifact = NewArtifact(stored_state.ArtifactKind.BRIEF, "attempt-a", 1, ".json", b"{}\n")

        reference = write_revision(roots, artifact)

        self.assertEqual("artifacts/briefs/attempt-a/1.json", reference.selector)
        self.assertEqual(reference, write_revision(roots, artifact))
        verify_reference(roots.work_root, reference)
        with self.assertRaises(ArtifactError) as collision:
            write_revision(roots, NewArtifact(stored_state.ArtifactKind.BRIEF, "attempt-a", 1, ".json", b"different\n"))
        self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, collision.exception.code)
        self.assertEqual(b"{}\n", (roots.work_root / reference.selector).read_bytes())

    def test_reference_verification_rejects_escape_size_and_digest(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        reference = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "review-a", 1, ".json", b"{}\n"),
        )

        for changed in (
            replace(reference, selector="../outside"),
            replace(reference, size_bytes=reference.size_bytes + 1),
            replace(reference, content_sha256="0" * 64),
        ):
            with self.subTest(reference=changed), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, changed)

    def test_artifact_identity_and_publication_failure_matrix_is_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        invalid = (
            NewArtifact(stored_state.ArtifactKind.BRIEF, "../escape", 1, ".json", b"x"),
            NewArtifact(stored_state.ArtifactKind.BRIEF, "brief", 0, ".json", b"x"),
            NewArtifact(stored_state.ArtifactKind.BRIEF, "brief", 1, "json", b"x"),
        )
        for artifact in invalid:
            with self.subTest(artifact=artifact), self.assertRaises(ArtifactError) as raised:
                write_revision(roots, artifact)
            self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, raised.exception.code)

        published = write_revision(roots, NewArtifact(stored_state.ArtifactKind.BRIEF, "brief", 1, ".json", b"x"))
        for selector in (
            "artifacts/briefs/other/1.json",
            "artifacts/briefs/brief/not-a-revision.json",
            "artifacts/briefs/brief/1.json/extra",
        ):
            with self.subTest(selector=selector), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, replace(published, selector=selector))

        failing_project = Path(tempfile.mkdtemp()).resolve()
        failing_roots = resolve_durable_roots(failing_project)
        with (
            patch(
                "pinboard.adapters.files.artifacts.ensure_directory_chain",
                side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "unavailable"),
            ),
            self.assertRaises(ArtifactError) as io_error,
        ):
            write_revision(failing_roots, NewArtifact(stored_state.ArtifactKind.RESULT, "result", 1, ".md", b"x"))
        self.assertEqual(ArtifactErrorCode.STORAGE_IO_ERROR, io_error.exception.code)

    def test_artifact_acceptance_reuse_and_relationship_failures_do_not_mutate(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "review-reuse", 1, ".md", b"ready\n"),
        )
        accepted = expect_success(
            store.accept_artifact_reference(
                roots.work_root,
                published,
                SQLITE_NOW,
                relationship=ArtifactRelationship(ItemId("work-a"), work_models.ArtifactRole.EVIDENCE),
            )
        )
        self.assertEqual(
            accepted,
            expect_success(store.accept_artifact_reference(roots.work_root, published, SQLITE_NOW)),
        )

        later_link = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "review-linked-later", 1, ".md", b"ready\n"),
        )
        initially_unlinked = expect_success(store.accept_artifact_reference(roots.work_root, later_link, SQLITE_NOW))
        before_link = store.snapshot()
        linked = expect_success(
            store.accept_artifact_reference(
                roots.work_root,
                later_link,
                SQLITE_NOW,
                relationship=ArtifactRelationship(ItemId("work-a"), work_models.ArtifactRole.EVIDENCE),
            )
        )
        after_link = store.snapshot()
        self.assertEqual(initially_unlinked, linked)
        self.assertEqual(before_link.lifecycle.project.revision + 1, after_link.lifecycle.project.revision)
        self.assertTrue(
            any(
                value.item_id == ItemId("work-a")
                and value.artifact_ref_id == linked.artifact_ref_id
                and value.role == work_models.ArtifactRole.EVIDENCE
                for value in after_link.lifecycle.item_artifacts
            )
        )
        for item_id, role in (
            (ItemId("missing"), work_models.ArtifactRole.EVIDENCE),
            (ItemId("work-a"), work_models.ArtifactRole.DESIGN),
        ):
            candidate = write_revision(
                roots,
                NewArtifact(stored_state.ArtifactKind.EVIDENCE, f"failure-{item_id}-{role}", 1, ".md", b"x"),
            )
            before = store.snapshot()
            with self.subTest(item_id=item_id, role=role), self.assertRaises(StorageError):
                store.accept_artifact_reference(
                    roots.work_root,
                    candidate,
                    SQLITE_NOW,
                    relationship=ArtifactRelationship(item_id, role),
                )
            self.assertEqual(before, store.snapshot())

    def test_expected_stale_artifact_acceptance_returns_failure_and_rolls_back(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        for relationship in (False, True):
            published = write_revision(
                roots,
                NewArtifact(
                    stored_state.ArtifactKind.EVIDENCE,
                    f"stale-artifact-{relationship}",
                    1,
                    ".md",
                    b"ready\n",
                ),
            )
            before = store.snapshot()
            connection = open_database(roots.database_path, OpenMode.READ_WRITE)
            target = (
                "UPDATE work_items SET subject_revision = subject_revision + 1 WHERE item_id = 'work-a'"
                if relationship
                else "UPDATE project_meta SET revision = revision + 1 WHERE singleton = 1"
            )
            connection.execute(
                f"""
                CREATE TEMP TRIGGER arrange_real_artifact_staleness
                BEFORE INSERT ON artifact_refs
                BEGIN
                    {target};
                END
                """
            )
            with patch("pinboard.adapters.sqlite.store.open_database", return_value=connection):
                if relationship:
                    result = store.accept_artifact_reference(
                        roots.work_root,
                        published,
                        SQLITE_NOW,
                        relationship=ArtifactRelationship(ItemId("work-a"), work_models.ArtifactRole.EVIDENCE),
                    )
                else:
                    result = store.accept_artifact_reference(roots.work_root, published, SQLITE_NOW)

            self.assertIsInstance(result, DecisionFailure)
            assert isinstance(result, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
