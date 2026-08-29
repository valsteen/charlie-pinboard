import contextlib
import io
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msgspec
from msgspec.structs import replace

from pinboard.adapters.files.artifacts import ArtifactRepository, write_revision
from pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.actions import discover_actions
from pinboard.application.artifacts import NewArtifact
from pinboard.application.dispatch import prepare_dispatch
from pinboard.application.dispatch_models import DispatchEnvironment, DispatchPermission
from pinboard.application.errors import DispatchError, DispatchErrorCode
from pinboard.application.stored_state import ArtifactKind
from pinboard.domain import decision_models
from pinboard.interfaces.cli import main
from pinboard.interfaces.dispatch_brief import prepare_dispatch_from_artifact, read_dispatch_environment
from pinboard.interfaces.work_brief_models import (
    CrossBoundaryCheckpoint,
    LocalCheckpoint,
    WorkBrief,
    WorkBriefReview,
)
from pinboard.interfaces.work_briefs import canonical_work_brief_bytes, canonical_work_brief_review_bytes
from tests.support import SQLITE_DIGEST, SQLITE_NOW, complete_sqlite_state
from tests.work_brief_support import CHECKPOINT_ID, ready_review, work_a_brief


def _reuse_review(_digest: str, review: bytes | None, _review_id: str | None) -> tuple[bytes, str]:
    return review or b"", "review"


class DispatchTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def environment(self, project: Path) -> DispatchEnvironment:
        return DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )

    def run_git(self, cwd: Path, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)

    def initialized(
        self,
        project: Path | None = None,
        roots: DurableRoots | None = None,
    ) -> tuple[
        Path,
        DurableRoots,
        SQLiteWorkStore,
        WorkBrief,
        Callable[[], decision_models.DispatchAction],
        DispatchEnvironment,
    ]:
        project = Path(tempfile.mkdtemp()).resolve() if project is None else project
        roots = resolve_durable_roots(project) if roots is None else roots
        initialize_database(roots, SQLITE_NOW)
        brief = work_a_brief(project)
        published = write_revision(
            roots,
            NewArtifact(
                ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                canonical_work_brief_bytes(brief),
            ),
        )
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        assert state.authority.coordination is not None
        coordination = dataclass_replace(state.authority.coordination, expires_at=now + timedelta(minutes=5))
        leases = tuple(
            dataclass_replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
        )
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
            authority=dataclass_replace(state.authority, coordination=coordination, attempt_leases=leases),
        )
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)

        def action() -> decision_models.DispatchAction:
            selected = next(
                candidate
                for candidate in discover_actions(
                    store,
                    decision_models.Role.COORDINATOR,
                    lease_id=coordination.lease_id,
                    generation=coordination.generation,
                )
                if candidate.kind == decision_models.ActionKind.DISPATCH
            )
            assert isinstance(selected, decision_models.DispatchAction)
            return selected

        return project, roots, store, brief, action, self.environment(project)

    def test_direct_typed_dispatch_validates_identity_sources_review_and_prompt(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))
        environment = self.environment(project)
        candidate = ready_review(value)

        prompt = prepare_dispatch_from_artifact(
            path,
            value.attempt_id,
            value.branch,
            project,
            CHECKPOINT_ID,
            environment,
            accepted_item_id=value.item_id,
            accepted_scope_revision=value.accepted_scope.revision,
            accepted_scope_digest=value.accepted_scope.digest,
            brief_review=candidate,
            review_id="review-id",
            review_publisher=lambda _digest, review, _review_id: (review or b"", "review"),
        )

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        self.assertIn(f"Canonical brief: {path}", prompt)
        with self.assertRaises(DispatchError) as altered_prompt:
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                project,
                CHECKPOINT_ID,
                environment,
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                supplied_prompt=(prompt + "extra").encode(),
                brief_review=candidate,
                review_id="review-id",
                review_publisher=lambda _digest, review, _review_id: (review or b"", "review"),
            )
        self.assertEqual(DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL, altered_prompt.exception.code)

        project.joinpath("architecture.md").write_text("# Architecture\n\n## Contract\n\nChanged.\n", encoding="utf-8")
        with self.assertRaises(DispatchError) as stale_source:
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                project,
                CHECKPOINT_ID,
                environment,
                accepted_item_id=value.item_id,
                accepted_scope_revision=value.accepted_scope.revision,
                accepted_scope_digest=value.accepted_scope.digest,
                brief_review=candidate,
                review_id="review-id",
                review_publisher=lambda _digest, review, _review_id: (review or b"", "review"),
            )
        self.assertEqual(DispatchErrorCode.DISPATCH_AUTHORITY_STALE, stale_source.exception.code)

    def test_identity_review_and_environment_failure_matrix_is_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        path = project / "brief.json"
        path.write_bytes(canonical_work_brief_bytes(value))
        environment = self.environment(project)

        cases = (
            ("attempt", {"attempt_id": "other"}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("item", {"accepted_item_id": "other"}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("scope-revision", {"accepted_scope_revision": 2}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("scope-digest", {"accepted_scope_digest": "f" * 64}, DispatchErrorCode.DISPATCH_BRIEF_INVALID),
            ("checkpoint", {"checkpoint": "other"}, DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING),
            (
                "branch",
                {"environment": replace(environment, branch="other")},
                DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            ),
            (
                "checkout",
                {"environment": replace(environment, checkout=str(project / "missing"))},
                DispatchErrorCode.DISPATCH_CHECKOUT_MISSING,
            ),
            (
                "checkout-mismatch",
                {"source_checkout_root": Path(tempfile.mkdtemp()).resolve()},
                DispatchErrorCode.DISPATCH_CHECKOUT_MISMATCH,
            ),
        )
        for _name, changed, code in cases:
            arguments = {
                "attempt_path": path,
                "attempt_id": value.attempt_id,
                "attempt_branch": value.branch,
                "source_checkout_root": project,
                "checkpoint": CHECKPOINT_ID,
                "environment": environment,
                "accepted_item_id": value.item_id,
                "accepted_scope_revision": value.accepted_scope.revision,
                "accepted_scope_digest": value.accepted_scope.digest,
                "brief_review": ready_review(value),
                "review_id": "review-id",
                "review_publisher": _reuse_review,
            }
            arguments.update(changed)
            with self.subTest(name=_name), self.assertRaises(DispatchError) as raised:
                prepare_dispatch_from_artifact(**arguments)
            self.assertEqual(code, raised.exception.code)

        review = msgspec.json.decode(ready_review(value), type=WorkBriefReview)
        for changed, code in (
            ({"reviewer_task_id": value.owner_task_id}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT),
            ({"checkpoint_sha256": "f" * 64}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE),
            ({"coverage": ()}, DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID),
        ):
            with self.subTest(changed=changed), self.assertRaises(DispatchError) as raised:
                prepare_dispatch_from_artifact(
                    path,
                    value.attempt_id,
                    value.branch,
                    project,
                    CHECKPOINT_ID,
                    environment,
                    accepted_item_id=value.item_id,
                    accepted_scope_revision=value.accepted_scope.revision,
                    accepted_scope_digest=value.accepted_scope.digest,
                    brief_review=canonical_work_brief_review_bytes(replace(review, **changed)),
                    review_id="review-id",
                    review_publisher=lambda _digest, candidate, _review_id: (candidate or b"", "review"),
                )
            self.assertEqual(code, raised.exception.code)

    def test_local_checkpoint_rejects_review_arguments(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        value = work_a_brief(project)
        cross = value.checkpoint
        assert isinstance(cross, CrossBoundaryCheckpoint)
        local = LocalCheckpoint(
            "local-cutover",
            "Local cutover",
            cross.architecture_impact,
            cross.outcome_description,
            cross.acceptance_criteria,
            cross.verification,
            cross.deferrals,
        )
        value = replace(value, checkpoint=local)
        path = project / "local.json"
        path.write_bytes(canonical_work_brief_bytes(value))

        prompt = prepare_dispatch_from_artifact(
            path,
            value.attempt_id,
            value.branch,
            project,
            local.checkpoint_id,
            self.environment(project),
            accepted_item_id=value.item_id,
            accepted_scope_revision=1,
            accepted_scope_digest=SQLITE_DIGEST,
        )
        self.assertIn("Checkpoint: local-cutover", prompt)
        with self.assertRaises(DispatchError) as raised:
            prepare_dispatch_from_artifact(
                path,
                value.attempt_id,
                value.branch,
                project,
                local.checkpoint_id,
                self.environment(project),
                accepted_item_id=value.item_id,
                accepted_scope_revision=1,
                accepted_scope_digest=SQLITE_DIGEST,
                brief_review=b"{}",
            )
        self.assertEqual(DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID, raised.exception.code)

    def test_sqlite_dispatch_publishes_reuses_and_preserves_review_collisions(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        first_review = ready_review(value)

        prompt = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action(),
            CHECKPOINT_ID,
            environment,
            brief_review=first_review,
            review_id="first-review",
        )

        self.assertIn(f"Checkpoint: {CHECKPOINT_ID}", prompt)
        after_first = store.snapshot()
        ready = tuple(
            reference
            for reference in after_first.artifact_references
            if reference.kind == ArtifactKind.EVIDENCE and "brief-review" in reference.key
        )
        self.assertEqual(1, len(ready))
        self.assertTrue(ready[0].selector.endswith(".json"))

        reused = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action(),
            CHECKPOINT_ID,
            environment,
        )
        self.assertEqual(prompt, reused)
        self.assertEqual(after_first, store.snapshot())

        with self.assertRaises(DispatchError) as collision:
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                action(),
                CHECKPOINT_ID,
                environment,
                brief_review=ready_review(value, result="Different complete result."),
                review_id="later-review",
            )
        self.assertEqual(DispatchErrorCode.DISPATCH_BRIEF_REVIEW_COLLISION, collision.exception.code)
        self.assertTrue(
            any("rejected-later-review" in reference.key for reference in store.snapshot().artifact_references)
        )

    def test_sqlite_dispatch_rejects_stale_action_and_cli_verifies_prompt(self) -> None:
        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        store.accept_artifact_reference(
            roots.work_root,
            write_revision(roots, NewArtifact(ArtifactKind.EVIDENCE, "revision-bump", 1, ".json", b"{}\n")),
            datetime.now(UTC),
        )
        with self.assertRaises(DispatchError) as stale:
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                selected,
                CHECKPOINT_ID,
                environment,
                brief_review=ready_review(value),
                review_id="review-id",
            )
        self.assertEqual(DispatchErrorCode.STALE_ACTION, stale.exception.code)

        project, roots, store, value, action, environment = self.initialized()
        selected = action()
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment))
        review_path = project / "review.json"
        review_path.write_bytes(ready_review(value))
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        arguments = (
            *common,
            "dispatch",
            "--action-id",
            str(decision_models.action_id(selected)),
            "--expected-revision",
            selected.capability.expected_revision,
            "--generation",
            str(selected.capability.coordinator_generation),
            "--lease-id",
            str(selected.capability.lease_id),
            "--checkpoint",
            CHECKPOINT_ID,
            "--environment",
            str(environment_path),
            "--brief-review",
            str(review_path),
            "--review-id",
            "cli-review",
        )
        result, prompt, stderr = self.run_cli(*arguments)
        self.assertEqual(0, result, stderr)
        prompt_path = project / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        refreshed = action()
        verify_arguments = list(arguments)
        verify_arguments[verify_arguments.index(selected.capability.expected_revision)] = (
            refreshed.capability.expected_revision
        )
        verify_arguments.extend(("--prompt", str(prompt_path)))
        result, stdout, stderr = self.run_cli(*verify_arguments)
        self.assertEqual(0, result, stderr)
        self.assertIn("DISPATCH_READY", stdout)

    def test_cli_dispatch_revalidates_the_linked_source_checkout_against_the_shared_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            linked = root / "linked"
            repository.mkdir()
            self.run_git(repository, "init", "-b", "main")
            (repository / "architecture.md").write_text(
                "# Architecture\n\n## Contract\n\nTyped JSON is canonical.\n",
                encoding="utf-8",
            )
            self.run_git(repository, "add", "architecture.md")
            self.run_git(
                repository,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            )
            self.run_git(repository, "worktree", "add", "-b", "codex/work-a", str(linked))
            _, roots, _, value, action, environment = self.initialized(linked, resolve_durable_roots(repository))
            (repository / "architecture.md").write_text(
                "# Architecture\n\n## Contract\n\nDirty primary authority.\n",
                encoding="utf-8",
            )
            environment_path = linked / "environment.json"
            environment_path.write_bytes(msgspec.json.encode(environment))
            review_path = linked / "review.json"
            review_path.write_bytes(ready_review(value))
            selected = action()

            result, prompt, stderr = self.run_cli(
                "--project-root",
                str(linked),
                "dispatch",
                "--action-id",
                str(decision_models.action_id(selected)),
                "--expected-revision",
                selected.capability.expected_revision,
                "--generation",
                str(selected.capability.coordinator_generation),
                "--lease-id",
                str(selected.capability.lease_id),
                "--checkpoint",
                CHECKPOINT_ID,
                "--environment",
                str(environment_path),
                "--brief-review",
                str(review_path),
                "--review-id",
                "linked-review",
            )
            shared_database_exists = (roots.work_root / "state.sqlite3").is_file()
            duplicate_ledger_exists = (linked / ".codex" / "pinboard").exists()
            linked_checkout = str(linked)

        self.assertEqual(0, result, stderr)
        self.assertIn(f"Checkout: {linked_checkout}", prompt)
        self.assertTrue(shared_database_exists)
        self.assertFalse(duplicate_ledger_exists)

    def test_dispatch_environment_is_strict(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        path = project / "environment.json"
        path.write_text(
            '{"schema":"pinboard-dispatch/v2","checkout":"x","branch":"b","starting_revision":"r","permissions":[]}',
            encoding="utf-8",
        )
        with self.assertRaises(DispatchError) as raised:
            read_dispatch_environment(path)
        self.assertEqual(DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
