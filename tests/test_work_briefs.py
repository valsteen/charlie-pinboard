import contextlib
import hashlib
import io
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

import msgspec
from msgspec.structs import replace

from charlie_pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifact_publication import validate_transition_work_brief
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.domain.decision_models import (
    ActionCapability,
    ActivateCommand,
    AuthorizationKind,
    ResumeCommand,
)
from charlie_pinboard.domain.errors import DecisionFailureCode
from charlie_pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId
from charlie_pinboard.domain.work_models import ActivateInput, ResumeInput
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.interfaces.errors import WorkBriefError, WorkBriefErrorCode
from charlie_pinboard.interfaces.work_brief_models import (
    AcceptanceCoverageOwner,
    AuthorityAuthorization,
    CrossBoundaryCheckpoint,
    DeferredCoverageOwner,
    ExistingConsumerAuthorization,
    LifecycleRecord,
    NoArchitectureImpact,
    NotApplicableCoverageOwner,
    ReadOnlyArchitecture,
    RequiredLifecyclePartition,
    ReviewCoverageResult,
    WorkBriefReview,
)
from charlie_pinboard.interfaces.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    canonical_work_brief_bytes,
    decode_work_brief,
    decode_work_brief_identity,
    decode_work_brief_review,
    render_work_brief_markdown,
    validate_work_brief_review,
)
from tests.support import complete_sqlite_state
from tests.work_brief_support import example_work_brief, work_a_brief, work_c_brief


class WorkBriefBoundaryTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_candidate_decodes_strictly_and_canonicalizes(self) -> None:
        value = example_work_brief()
        candidate = msgspec.json.encode(value)

        decoded = decode_work_brief(candidate)

        self.assertEqual(value, decoded)
        canonical = canonical_work_brief_bytes(decoded)
        self.assertTrue(canonical.endswith(b"\n"))
        self.assertEqual(decoded, decode_work_brief(canonical))
        with self.assertRaises(WorkBriefError) as unknown:
            decode_work_brief(candidate[:-1] + b',"unknown":true}')
        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID, unknown.exception.code)

    def test_cross_references_are_rejected_at_the_typed_boundary(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, CrossBoundaryCheckpoint)
        changed = replace(
            value,
            checkpoint=replace(
                checkpoint,
                coverage=(
                    replace(
                        checkpoint.coverage[0],
                        owner=AcceptanceCoverageOwner(criterion=99),
                    ),
                ),
            ),
        )

        with self.assertRaises(WorkBriefError) as raised:
            canonical_work_brief_bytes(changed)

        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID, raised.exception.code)

    def test_every_tagged_variant_decodes_through_the_strict_boundary(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, CrossBoundaryCheckpoint)
        authority_basis = AuthorityAuthorization("repository-guidance", "repository-practice")
        existing_consumer_basis = ExistingConsumerAuthorization("repository-guidance", "repository-practice")
        for name, changed in (
            ("architecture-none", replace(checkpoint, architecture_impact=NoArchitectureImpact("No change."))),
            (
                "architecture-read-only",
                replace(checkpoint, architecture_impact=ReadOnlyArchitecture("ARCHITECTURE.md", "Conform.")),
            ),
            (
                "authority-authorization",
                replace(
                    checkpoint,
                    contracts=(replace(checkpoint.contracts[0], authorization_basis=authority_basis),),
                ),
            ),
            (
                "existing-consumer-authorization",
                replace(
                    checkpoint,
                    verification=(replace(checkpoint.verification[0], authorization_basis=existing_consumer_basis),),
                ),
            ),
            (
                "acceptance-owner",
                replace(
                    checkpoint,
                    coverage=(replace(checkpoint.coverage[0], owner=AcceptanceCoverageOwner(1)),),
                ),
            ),
            (
                "deferred-owner",
                replace(
                    checkpoint,
                    coverage=(replace(checkpoint.coverage[0], owner=DeferredCoverageOwner("later-work")),),
                ),
            ),
            (
                "not-applicable-owner",
                replace(
                    checkpoint,
                    coverage=(
                        replace(
                            checkpoint.coverage[0],
                            owner=NotApplicableCoverageOwner("No effect."),
                        ),
                    ),
                ),
            ),
            (
                "required-lifecycle",
                replace(
                    checkpoint,
                    lifecycle_partition=RequiredLifecyclePartition(
                        (LifecycleRecord("publish", "candidate", "application", "receipt", "accepted", "overwrite"),)
                    ),
                ),
            ),
        ):
            with self.subTest(name=name):
                candidate = replace(value, checkpoint=changed)
                self.assertEqual(candidate, decode_work_brief(canonical_work_brief_bytes(candidate)))

        invalid = replace(value, owner_task_id=" owner-task ")
        with self.assertRaises(WorkBriefError) as raised:
            decode_work_brief(msgspec.json.encode(invalid))
        self.assertEqual(WorkBriefErrorCode.BRIEF_INVALID, raised.exception.code)

    def test_checkpoint_and_authority_digests_use_canonical_records(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, CrossBoundaryCheckpoint)

        self.assertEqual(
            "a2941d05f3c61a40ca5014af48a095ee919e2cad2fe2d78a09a74a8835693f1f",
            hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
        )
        renamed = replace(checkpoint, title="Renamed title")
        self.assertEqual(checkpoint.checkpoint_id, renamed.checkpoint_id)
        self.assertNotEqual(canonical_checkpoint_bytes(checkpoint), canonical_checkpoint_bytes(renamed))
        self.assertEqual(
            "ec8a74883f273b06ab9571a0b92c655280375fd3af2adcaa5c6bbe505a3c64a0",
            hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
        )
        self.assertEqual(
            msgspec.json.encode(checkpoint.reviewed_authorities, order="sorted"),
            canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities),
        )
        second = replace(
            checkpoint.reviewed_authorities[0],
            authority_id="second-authority",
            reviewed_sha256="c" * 64,
        )
        ordered = (*checkpoint.reviewed_authorities, second)
        self.assertNotEqual(
            canonical_reviewed_authority_set_bytes(ordered),
            canonical_reviewed_authority_set_bytes(tuple(reversed(ordered))),
        )

    def test_review_is_strict_digest_bound_and_independent(self) -> None:
        value = example_work_brief()
        checkpoint = value.checkpoint
        assert isinstance(checkpoint, CrossBoundaryCheckpoint)
        coverage = checkpoint.coverage[0]
        review = WorkBriefReview(
            schema="pinboard-work-brief-review/v2",
            attempt_id=value.attempt_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sha256=hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
            reviewed_authority_set_sha256=hashlib.sha256(
                canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
            ).hexdigest(),
            reviewer_task_id="independent-reviewer",
            status="complete",
            verdict="ready",
            coverage=(
                ReviewCoverageResult(
                    authority_id=coverage.authority_id,
                    family=coverage.family,
                    owner=coverage.owner,
                    verdict="covered",
                    counterexample_result="Untyped decoding is rejected by the strict model.",
                ),
            ),
        )

        decoded = decode_work_brief_review(msgspec.json.encode(review))
        validate_work_brief_review(decoded, value, reviewer_task_id=value.owner_task_id)
        with self.assertRaises(WorkBriefError) as same_owner:
            validate_work_brief_review(replace(review, reviewer_task_id=value.owner_task_id), value)
        self.assertEqual(WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT, same_owner.exception.code)
        with self.assertRaises(WorkBriefError) as stale:
            validate_work_brief_review(replace(review, checkpoint_sha256="f" * 64), value)
        self.assertEqual(WorkBriefErrorCode.REVIEW_STALE, stale.exception.code)

    def test_markdown_is_a_complete_generated_projection(self) -> None:
        rendered = render_work_brief_markdown(example_work_brief()).decode()

        self.assertIn("Generated projection; canonical JSON is authoritative.", rendered)
        self.assertIn("typed-json-cutover", rendered)
        self.assertIn("Strict typed JSON remains canonical.", rendered)
        self.assertIn("uv run --locked pyrefly check", rendered)
        self.assertIn("later-work", rendered)

    def test_activation_and_resume_reject_mismatched_typed_brief_identity(self) -> None:
        capability_values = ("label", "expected", 1, None, AuthorizationKind.COORDINATOR, None, None)
        for name in ("activate", "resume"):
            with self.subTest(name=name):
                project = Path(tempfile.mkdtemp()).resolve()
                roots = resolve_durable_roots(project)
                if name == "activate":
                    value = work_c_brief()
                    capability = ActionCapability(ItemId("work-c"), *capability_values)
                    command = ActivateCommand(
                        capability,
                        ActivateInput(
                            AttemptId("work-c-1"),
                            "codex/work-c",
                            "candidate-base",
                            "worker-task",
                            ArtifactRefId(1),
                        ),
                    )
                else:
                    value = work_a_brief(project)
                    capability = ActionCapability(ItemId("work-a"), *capability_values)
                    command = ResumeCommand(capability, ResumeInput(ArtifactRefId(1)))
                published = write_revision(
                    roots,
                    NewArtifact(ArtifactKind.BRIEF, value.attempt_id, 1, ".json", canonical_work_brief_bytes(value)),
                )
                state = complete_sqlite_state()
                reference = dataclass_replace(
                    state.artifact_references[0],
                    key=published.key,
                    revision=published.revision,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
                state = dataclass_replace(
                    state,
                    artifact_references=(reference, *state.artifact_references[1:]),
                )
                artifacts = ArtifactRepository(roots)

                self.assertIsNone(validate_transition_work_brief(state, command, artifacts, decode_work_brief_identity))

                mismatched = replace(value, branch="codex/different")
                mismatch = write_revision(
                    roots,
                    NewArtifact(
                        ArtifactKind.BRIEF,
                        f"{value.attempt_id}-mismatch",
                        1,
                        ".json",
                        canonical_work_brief_bytes(mismatched),
                    ),
                )
                mismatched_reference = dataclass_replace(
                    reference,
                    key=mismatch.key,
                    selector=mismatch.selector,
                    content_sha256=mismatch.content_sha256,
                    size_bytes=mismatch.size_bytes,
                )
                mismatched_state = dataclass_replace(
                    state,
                    artifact_references=(mismatched_reference, *state.artifact_references[1:]),
                )
                failure = validate_transition_work_brief(
                    mismatched_state,
                    command,
                    artifacts,
                    decode_work_brief_identity,
                )
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, failure.code)

    def test_installed_publication_is_canonical_scheduling_neutral_and_retryable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        result, _stdout, stderr = self.run_cli(*common, "init")
        self.assertEqual(0, result, stderr)
        candidate = project / "brief.json"
        candidate.write_bytes(msgspec.json.format(msgspec.json.encode(example_work_brief()), indent=2))
        before = SQLiteWorkStore(work / "state.sqlite3").snapshot()

        result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate), "--json")

        self.assertEqual(0, result, stderr)
        receipt = msgspec.json.decode(stdout.encode())
        self.assertEqual("artifacts/briefs/make-canonical-briefs-typed-json-1/1.json", receipt["selector"])
        after = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.focus, after.focus)
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        artifact = work / receipt["selector"]
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), artifact.read_bytes())

        retry_result, retry_stdout, retry_stderr = self.run_cli(
            *common, "brief", "publish", "--file", str(candidate), "--json"
        )
        self.assertEqual(0, retry_result, retry_stderr)
        self.assertEqual(receipt, msgspec.json.decode(retry_stdout.encode()))
        self.assertEqual(after, SQLiteWorkStore(work / "state.sqlite3").snapshot())

        candidate.write_bytes(canonical_work_brief_bytes(replace(example_work_brief(), title="Different title")))
        collision_result, _collision_stdout, collision_stderr = self.run_cli(
            *common, "brief", "publish", "--file", str(candidate)
        )
        self.assertEqual(12, collision_result)
        self.assertIn("STORAGE_INVARIANT_VIOLATION", collision_stderr)
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), artifact.read_bytes())

    def test_publication_failure_leaves_reusable_verified_orphan(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        candidate = project / "brief.json"
        candidate.write_bytes(canonical_work_brief_bytes(example_work_brief()))

        with (
            patch.object(SQLiteWorkStore, "accept_artifact_reference", side_effect=RuntimeError("database failed")),
            self.assertRaises(RuntimeError),
        ):
            self.run_cli(*common, "brief", "publish", "--file", str(candidate))

        orphan = work / "artifacts" / "briefs" / example_work_brief().attempt_id / "1.json"
        self.assertEqual(canonical_work_brief_bytes(example_work_brief()), orphan.read_bytes())
        result, stdout, stderr = self.run_cli(*common, "brief", "publish", "--file", str(candidate))
        self.assertEqual(0, result, stderr)
        self.assertIn("BRIEF_PUBLISHED", stdout)


if __name__ == "__main__":
    unittest.main()
