import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from pinboard.adapters.files.artifacts import verify_reference, write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import CheckpointArtifacts, EvidenceArtifactRef, NewArtifact, ResultArtifactRef
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.service import (
    change_attempt_authority,
    change_coordination_authority,
    execute,
    execute_checkpoint_acceptance,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.authority_models import (
    AttemptLeaseStatus,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
)
from pinboard.domain.decisions import available_actions, bind_transition
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId, CandidateId, CheckpointId
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes
from tests.prototypes.portable_copy import PortableCopyError, PortableCopyErrorCode, create_portable_copy
from tests.support import SQLITE_NOW, complete_sqlite_state
from tests.work_brief_support import work_a_brief


class PortableCopyTest(unittest.TestCase):
    def _source(self, *, live_authority: bool = False) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        state = complete_sqlite_state()
        references = []
        for reference in state.artifact_references:
            if reference.kind == stored_state.ArtifactKind.BRIEF:
                value = work_a_brief(project)
                content = canonical_work_brief_bytes(value)
                suffix = ".json"
                key = value.attempt_id
            else:
                content = f"portable artifact {reference.key}\n".encode()
                suffix = ".md"
                key = reference.key
            published = write_revision(
                roots,
                NewArtifact(reference.kind, key, reference.revision, suffix, content),
            )
            references.append(
                replace(
                    reference,
                    key=published.key,
                    revision=published.revision,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
            )
        historical = write_revision(
            roots,
            NewArtifact(
                stored_state.ArtifactKind.BRIEF, "historical-v1-attempt", 1, ".md", b"opaque terminal v1 evidence\n"
            ),
        )
        references.append(
            stored_state.ArtifactReference(
                ArtifactRefId(1 + max(int(value.artifact_ref_id) for value in state.artifact_references)),
                historical.key,
                historical.revision,
                historical.kind,
                historical.selector,
                historical.content_sha256,
                historical.size_bytes,
                state.lifecycle.project.revision,
                SQLITE_NOW,
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
                        state=work_models.CoordinationLeaseStatus.RELEASED,
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
        self.assertEqual(source_state.lifecycle.definition_revisions, copied.lifecycle.definition_revisions)
        self.assertEqual(source_state.lifecycle.dependencies, copied.lifecycle.dependencies)
        self.assertEqual(source_state.lifecycle.item_artifacts, copied.lifecycle.item_artifacts)
        self.assertEqual(source_state.lifecycle.attempts, copied.lifecycle.attempts)
        self.assertEqual(source_state.proposals, copied.proposals)
        self.assertEqual(source_state.artifact_references, copied.artifact_references)
        self.assertTrue(
            copied.authority.coordination is None
            or copied.authority.coordination.state != work_models.CoordinationLeaseStatus.ACTIVE
        )
        self.assertTrue(all(lease.state != AttemptLeaseStatus.ACTIVE for lease in copied.authority.attempt_leases))
        self.assertEqual(source_state.lifecycle.project.host_epoch + 1, copied.lifecycle.project.host_epoch)
        self.assertEqual(source_state.lifecycle.project.revision, copied.lifecycle.project.revision)
        self.assertEqual(source_state.transition_receipts, copied.transition_receipts)
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
        stale = work_models.CommandAttemptAuthority(
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

    def test_portable_copy_includes_accepted_checkpoint_result_and_review_bytes(self) -> None:
        source, store = self._source(live_authority=True)
        snapshot = project_decision_snapshot(store.snapshot())
        attempt_authority = snapshot.command_attempt_authorities[0]
        worker = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            attempt_authority.generation,
            attempt_authority.lease_id,
            (attempt_authority.attempt,),
            False,
        )
        worker_actions = available_actions(snapshot, worker)
        self.assertIsInstance(worker_actions, tuple)
        assert isinstance(worker_actions, tuple)
        submit_action = next(
            value for value in worker_actions if value.kind == decision_models.ActionKind.SUBMIT_REVIEW
        )
        submit = bind_transition(submit_action, work_models.SubmitReviewInput(CandidateId("candidate-a")))
        assert isinstance(submit, decision_models.SubmitReviewCommand)
        self.assertNotIsInstance(execute(store, submit, SQLITE_NOW), DecisionFailure)
        review_snapshot = project_decision_snapshot(store.snapshot())
        coordination = review_snapshot.coordination_authority
        assert coordination is not None
        coordinator = decision_models.ActorAuthority(
            decision_models.Role.COORDINATOR,
            decision_models.AuthorizationKind.COORDINATION,
            coordination.generation,
            coordination.lease_id,
        )
        coordinator_actions = available_actions(review_snapshot, coordinator)
        self.assertIsInstance(coordinator_actions, tuple)
        assert isinstance(coordinator_actions, tuple)
        accept_action = next(
            value for value in coordinator_actions if value.kind == decision_models.ActionKind.ACCEPT_CHECKPOINT
        )
        accept = bind_transition(
            accept_action,
            work_models.AcceptCheckpointInput(
                CheckpointId("checkpoint-a"),
                CandidateId("candidate-a"),
                "Accepted for portable-copy proof.",
            ),
        )
        assert isinstance(accept, decision_models.AcceptCheckpointCommand)
        result_bytes = b"portable checkpoint result\n"
        review_bytes = b"portable checkpoint review\n"
        roots = resolve_durable_roots(source.parent.parent)
        result = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.RESULT, "work-a-1-checkpoint-a-result", 1, ".md", result_bytes),
        )
        review = write_revision(
            roots,
            NewArtifact(stored_state.ArtifactKind.EVIDENCE, "work-a-1-checkpoint-a-review", 1, ".md", review_bytes),
        )
        artifacts = CheckpointArtifacts(
            ResultArtifactRef(result.key, result.revision, result.selector, result.content_sha256, result.size_bytes),
            EvidenceArtifactRef(review.key, review.revision, review.selector, review.content_sha256, review.size_bytes),
        )
        self.assertNotIsInstance(
            execute_checkpoint_acceptance(store, accept, SQLITE_NOW, artifacts),
            DecisionFailure,
        )
        retained = project_decision_snapshot(store.snapshot()).coordination_authority
        assert retained is not None
        self.assertNotIsInstance(
            change_coordination_authority(
                store,
                ReleaseCoordinationAuthority(retained, SQLITE_NOW),
            ),
            DecisionFailure,
        )
        destination = Path(tempfile.mkdtemp()).resolve() / "checkpoint-copy"

        create_portable_copy(source, destination)

        copied = SQLiteWorkStore(destination / "state.sqlite3").snapshot()
        result_reference = next(
            value for value in copied.artifact_references if value.key == "work-a-1-checkpoint-a-result"
        )
        review_reference = next(
            value for value in copied.artifact_references if value.key == "work-a-1-checkpoint-a-review"
        )
        self.assertEqual(result_bytes, (destination / result_reference.selector).read_bytes())
        self.assertEqual(review_bytes, (destination / review_reference.selector).read_bytes())
        self.assertEqual(result_reference.artifact_ref_id, copied.lifecycle.attempts[0].result_artifact_ref_id)
        acceptance_receipt = next(
            value
            for value in copied.transition_receipts
            if value.action_kind == stored_state.TransitionHistoryActionKind.ACCEPT_CHECKPOINT
        )
        self.assertEqual(review_reference.artifact_ref_id, acceptance_receipt.artifact_ref_id)

    def test_portable_copy_rejects_live_authority_existing_destination_and_missing_artifact(self) -> None:
        source, _store = self._source(live_authority=True)
        destination_parent = Path(tempfile.mkdtemp()).resolve()
        destination = destination_parent / "relocated-work"
        with self.assertRaises(PortableCopyError) as live:
            create_portable_copy(source, destination)
        self.assertEqual(PortableCopyErrorCode.PORTABLE_COPY_SOURCE_NOT_QUIESCENT, live.exception.code)
        self.assertFalse(destination.exists())

        source, source_store = self._source()
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_bytes(b"existing destination")
        with self.assertRaises(PortableCopyError) as collision:
            create_portable_copy(source, destination)
        self.assertEqual(PortableCopyErrorCode.PORTABLE_COPY_DESTINATION_EXISTS, collision.exception.code)
        self.assertEqual(b"existing destination", sentinel.read_bytes())

        missing_destination = destination_parent / "missing-artifact-copy"
        missing = source / source_store.snapshot().artifact_references[0].selector
        missing.unlink()
        with self.assertRaises(PortableCopyError) as artifact:
            create_portable_copy(source, missing_destination)
        self.assertEqual(PortableCopyErrorCode.STORAGE_INVARIANT_VIOLATION, artifact.exception.code)
        self.assertFalse(missing_destination.exists())

    def test_portable_copy_rejects_destination_nested_in_source_without_changing_source(self) -> None:
        source, _store = self._source()
        source_bytes = self._bytes(source)
        destination = source / "nested-portable-work"

        with self.assertRaises(PortableCopyError) as invalid:
            create_portable_copy(source, destination)

        self.assertEqual(PortableCopyErrorCode.PORTABLE_COPY_DESTINATION_INVALID, invalid.exception.code)
        self.assertFalse(destination.exists())
        self.assertEqual(source_bytes, self._bytes(source))


if __name__ == "__main__":
    unittest.main()
