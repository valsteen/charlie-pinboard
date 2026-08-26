import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from charlie_pinboard.adapters.files.artifacts import verify_reference, write_revision
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.service import change_attempt_authority
from charlie_pinboard.application.stored_state import (
    TransitionHistoryActionKind,
)
from charlie_pinboard.application.transfer import PortableCopyError, create_portable_copy
from charlie_pinboard.domain.authority_decisions import (
    AttemptLeaseStatus,
    RenewAttemptAuthority,
)
from charlie_pinboard.domain.errors import DecisionFailure
from charlie_pinboard.domain.model import CommandAttemptAuthority, CoordinationLeaseStatus
from tests.support import SQLITE_NOW, complete_sqlite_state


class PortableCopyTest(unittest.TestCase):
    def _source(self, *, live_authority: bool = False) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        state = complete_sqlite_state()
        references = []
        for reference in state.artifact_references:
            content = f"portable artifact {reference.key}\n".encode()
            published = write_revision(
                roots,
                NewArtifact(reference.kind, reference.key, reference.revision, ".md", content),
            )
            references.append(
                replace(
                    reference,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
            )
        if not live_authority:
            assert state.authority.coordination is not None
            state = replace(
                state,
                authority=replace(
                    state.authority,
                    coordination=replace(
                        state.authority.coordination,
                        state=CoordinationLeaseStatus.RELEASED,
                    ),
                    attempt_leases=tuple(
                        replace(lease, state=AttemptLeaseStatus.RELEASED) for lease in state.authority.attempt_leases
                    ),
                ),
            )
        state = replace(state, artifact_references=tuple(references))
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        return roots.work_root, store

    @staticmethod
    def _bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
        }

    def test_portable_copy_retains_semantics_and_artifacts_but_neutralizes_host_authority(self) -> None:
        source, source_store = self._source()
        source_state = source_store.snapshot()
        source_bytes = self._bytes(source)
        destination_parent = Path(tempfile.mkdtemp()).resolve()
        destination = destination_parent / "relocated-work"

        receipt = create_portable_copy(source, destination)

        destination_store = SQLiteWorkStore(destination / "state.sqlite3")
        copied = destination_store.snapshot()
        self.assertEqual(source_state.lifecycle.work_items, copied.lifecycle.work_items)
        self.assertEqual(source_state.lifecycle.scope_revisions, copied.lifecycle.scope_revisions)
        self.assertEqual(source_state.lifecycle.dependencies, copied.lifecycle.dependencies)
        self.assertEqual(source_state.lifecycle.item_artifacts, copied.lifecycle.item_artifacts)
        self.assertEqual(source_state.lifecycle.attempts, copied.lifecycle.attempts)
        self.assertEqual(source_state.proposals, copied.proposals)
        self.assertEqual(source_state.artifact_references, copied.artifact_references)
        self.assertTrue(
            copied.authority.coordination is None
            or copied.authority.coordination.state != CoordinationLeaseStatus.ACTIVE
        )
        self.assertTrue(all(lease.state != AttemptLeaseStatus.ACTIVE for lease in copied.authority.attempt_leases))
        self.assertEqual(source_state.lifecycle.project.host_epoch + 1, copied.lifecycle.project.host_epoch)
        self.assertEqual(source_state.lifecycle.project.revision + 1, copied.lifecycle.project.revision)
        self.assertEqual(TransitionHistoryActionKind.PORTABLE_COPY, copied.transition_receipts[-1].action_kind)
        self.assertEqual(source_state.lifecycle.project.revision, receipt.source_revision)
        self.assertEqual(copied.lifecycle.project.revision, receipt.destination_revision)
        self.assertEqual(len(source_state.artifact_references), receipt.artifacts_copied)
        for reference in copied.artifact_references:
            verify_reference(destination, reference)
        self.assertTrue((destination / "views" / "queue.md").is_file())
        self.assertTrue((destination / "views" / "attempts" / "work-a-1.md").is_file())
        self.assertEqual(source_bytes, self._bytes(source))

        source_attempt = source_state.lifecycle.attempts[0]
        source_item = next(item for item in source_state.lifecycle.work_items if item.item_id == source_attempt.item_id)
        stale = CommandAttemptAuthority(
            source_state.lifecycle.project.host_epoch,
            source_attempt.item_id,
            str(source_item.subject_revision),
            source_attempt.attempt_id,
            str(source_attempt.subject_revision),
            source_state.authority.attempt_generations[0].task_id,
            source_state.authority.attempt_generations[0].host_id,
            source_state.authority.attempt_generations[0].lease_id,
            source_state.authority.attempt_generations[0].generation,
            SQLITE_NOW + timedelta(minutes=5),
        )
        before_rejection = destination_store.snapshot().lifecycle.project.revision
        rejected = change_attempt_authority(
            destination_store,
            RenewAttemptAuthority(stale, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=10)),
        )
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(before_rejection, destination_store.snapshot().lifecycle.project.revision)

    def test_portable_copy_rejects_live_authority_existing_destination_and_missing_artifact(self) -> None:
        source, _store = self._source(live_authority=True)
        destination_parent = Path(tempfile.mkdtemp()).resolve()
        destination = destination_parent / "relocated-work"
        with self.assertRaises(PortableCopyError) as live:
            create_portable_copy(source, destination)
        self.assertEqual("PORTABLE_COPY_SOURCE_NOT_QUIESCENT", live.exception.code)
        self.assertFalse(destination.exists())

        source, source_store = self._source()
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_bytes(b"existing destination")
        with self.assertRaises(PortableCopyError) as collision:
            create_portable_copy(source, destination)
        self.assertEqual("PORTABLE_COPY_DESTINATION_EXISTS", collision.exception.code)
        self.assertEqual(b"existing destination", sentinel.read_bytes())

        missing_destination = destination_parent / "missing-artifact-copy"
        missing = source / source_store.snapshot().artifact_references[0].selector
        missing.unlink()
        with self.assertRaises(PortableCopyError) as artifact:
            create_portable_copy(source, missing_destination)
        self.assertEqual("STORAGE_INVARIANT_VIOLATION", artifact.exception.code)
        self.assertFalse(missing_destination.exists())

    def test_portable_copy_rejects_destination_nested_in_source_without_changing_source(self) -> None:
        source, _store = self._source()
        source_bytes = self._bytes(source)
        destination = source / "nested-portable-work"

        with self.assertRaises(PortableCopyError) as invalid:
            create_portable_copy(source, destination)

        self.assertEqual("PORTABLE_COPY_DESTINATION_INVALID", invalid.exception.code)
        self.assertFalse(destination.exists())
        self.assertEqual(source_bytes, self._bytes(source))


if __name__ == "__main__":
    unittest.main()
