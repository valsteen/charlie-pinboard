import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from charlie_pinboard.adapters.files.artifacts import verify_reference, write_revision
from charlie_pinboard.adapters.files.errors import (
    ArtifactError,
    ArtifactErrorCode,
    FileIOError,
    FileIOErrorCode,
)
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.files.models import DurableFile
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.errors import StorageError
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.work_models import ArtifactRole
from tests.support import SQLITE_NOW, complete_sqlite_state


class ArtifactPersistenceTest(unittest.TestCase):
    def test_accepting_relationship_preserves_canonical_global_link_order(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(ArtifactKind.EVIDENCE, "intake-work-review", 1, ".md", b"ready\n"),
        )

        accepted = store.accept_artifact_reference(
            roots.work_root,
            published,
            SQLITE_NOW,
            item_id=ItemId("intake-work"),
            role=ArtifactRole.EVIDENCE,
        )

        reloaded = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertIn(accepted, reloaded.artifact_references)
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
            NewArtifact(ArtifactKind.EVIDENCE, "review-a", 1, ".md", b"ready\n"),
        )

        accepted = store.accept_artifact_reference(
            roots.work_root,
            published,
            SQLITE_NOW,
            item_id=ItemId("work-a"),
            role=ArtifactRole.EVIDENCE,
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
        artifact = NewArtifact(ArtifactKind.BRIEF, "attempt-a", 1, ".md", b"# Brief\n")

        reference = write_revision(roots, artifact)

        self.assertEqual("artifacts/briefs/attempt-a/1.md", reference.selector)
        self.assertEqual(reference, write_revision(roots, artifact))
        verify_reference(roots.work_root, reference)
        with self.assertRaises(ArtifactError) as collision:
            write_revision(roots, NewArtifact(ArtifactKind.BRIEF, "attempt-a", 1, ".md", b"different\n"))
        self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, collision.exception.code)
        self.assertEqual(b"# Brief\n", (roots.work_root / reference.selector).read_bytes())

    def test_reference_verification_rejects_escape_symlink_size_and_digest(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        reference = write_revision(
            roots,
            NewArtifact(ArtifactKind.EVIDENCE, "review-a", 1, ".json", b"{}\n"),
        )

        for changed in (
            replace(reference, selector="../outside"),
            replace(reference, size_bytes=reference.size_bytes + 1),
            replace(reference, content_sha256="0" * 64),
        ):
            with self.subTest(reference=changed), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, changed)

        target = roots.work_root / reference.selector
        original = target.with_name("original.json")
        target.rename(original)
        target.symlink_to(original.name)
        with self.assertRaises(ArtifactError):
            verify_reference(roots.work_root, reference)

    def test_artifact_identity_and_publication_failure_matrix_is_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        invalid = (
            NewArtifact(ArtifactKind.BRIEF, "../escape", 1, ".md", b"x"),
            NewArtifact(ArtifactKind.BRIEF, "brief", 0, ".md", b"x"),
            NewArtifact(ArtifactKind.BRIEF, "brief", 1, "md", b"x"),
        )
        for artifact in invalid:
            with self.subTest(artifact=artifact), self.assertRaises(ArtifactError) as raised:
                write_revision(roots, artifact)
            self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, raised.exception.code)

        published = write_revision(roots, NewArtifact(ArtifactKind.BRIEF, "brief", 1, ".md", b"x"))
        for selector in (
            "artifacts/briefs/other/1.md",
            "artifacts/briefs/brief/not-a-revision.md",
            "artifacts/briefs/brief/1.md/extra",
        ):
            with self.subTest(selector=selector), self.assertRaises(ArtifactError):
                verify_reference(roots.work_root, replace(published, selector=selector))

        with (
            patch(
                "charlie_pinboard.adapters.files.artifacts.create_immutable",
                return_value=DurableFile("0" * 64, 100),
            ),
            self.assertRaises(ArtifactError),
        ):
            write_revision(roots, NewArtifact(ArtifactKind.RESULT, "bad-facts", 1, ".md", b"x"))

        failing_project = Path(tempfile.mkdtemp()).resolve()
        failing_roots = resolve_durable_roots(failing_project)
        with (
            patch(
                "charlie_pinboard.adapters.files.artifacts.ensure_directory_chain",
                side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "unavailable"),
            ),
            self.assertRaises(ArtifactError) as io_error,
        ):
            write_revision(failing_roots, NewArtifact(ArtifactKind.RESULT, "result", 1, ".md", b"x"))
        self.assertEqual(ArtifactErrorCode.STORAGE_IO_ERROR, io_error.exception.code)

    def test_artifact_acceptance_reuse_and_relationship_failures_do_not_mutate(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        published = write_revision(
            roots,
            NewArtifact(ArtifactKind.EVIDENCE, "review-reuse", 1, ".md", b"ready\n"),
        )
        accepted = store.accept_artifact_reference(
            roots.work_root,
            published,
            SQLITE_NOW,
            item_id=ItemId("work-a"),
            role=ArtifactRole.EVIDENCE,
        )
        self.assertEqual(accepted, store.accept_artifact_reference(roots.work_root, published, SQLITE_NOW))

        later_link = write_revision(
            roots,
            NewArtifact(ArtifactKind.EVIDENCE, "review-linked-later", 1, ".md", b"ready\n"),
        )
        initially_unlinked = store.accept_artifact_reference(roots.work_root, later_link, SQLITE_NOW)
        before_link = store.snapshot()
        linked = store.accept_artifact_reference(
            roots.work_root,
            later_link,
            SQLITE_NOW,
            item_id=ItemId("work-a"),
            role=ArtifactRole.EVIDENCE,
        )
        after_link = store.snapshot()
        self.assertEqual(initially_unlinked, linked)
        self.assertEqual(before_link.lifecycle.project.revision + 1, after_link.lifecycle.project.revision)
        self.assertTrue(
            any(
                value.item_id == ItemId("work-a")
                and value.artifact_ref_id == linked.artifact_ref_id
                and value.role == ArtifactRole.EVIDENCE
                for value in after_link.lifecycle.item_artifacts
            )
        )
        with self.assertRaises(StorageError):
            store.accept_artifact_reference(roots.work_root, later_link, SQLITE_NOW, item_id=ItemId("work-a"))

        for item_id, role in (
            (ItemId("work-a"), None),
            (ItemId("missing"), ArtifactRole.EVIDENCE),
            (ItemId("work-a"), ArtifactRole.DESIGN),
        ):
            candidate = write_revision(
                roots,
                NewArtifact(ArtifactKind.EVIDENCE, f"failure-{item_id}-{role}", 1, ".md", b"x"),
            )
            before = store.snapshot()
            with self.subTest(item_id=item_id, role=role), self.assertRaises(StorageError):
                store.accept_artifact_reference(
                    roots.work_root,
                    candidate,
                    SQLITE_NOW,
                    item_id=item_id,
                    role=role,
                )
            self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
