import contextlib
import hashlib
import io
import json
import multiprocessing
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.artifacts import ArtifactRepository, NewArtifact, write_revision
from charlie_pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import discover_actions
from charlie_pinboard.application.dispatch import prepare_sqlite_dispatch
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.domain.decisions import ActionKind, Role
from charlie_pinboard.domain.identifiers import LeaseId
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.legacy.actions import Action, actions_for
from charlie_pinboard.legacy.atomic import transition_lock
from charlie_pinboard.legacy.dispatch import (
    DispatchEnvironment,
    DispatchError,
    DispatchPermission,
    prepare_dispatch,
    prepare_dispatch_from_artifact,
    read_dispatch_environment,
)

from .support import SQLITE_NOW, complete_sqlite_state, create_state

CHECKPOINT = "Sequence 2 — Complete the shared protocol cutover"
CONTRACT_INVARIANT = "Kotlin and Rust use protocol v13 together."
CONTRACT_TABLE = """\
#### Contract table

| Invariant | Authority / owner | Required consumer or production observation | Failure classification | Exact verification | Preflight / final revalidation |
| --- | --- | --- | --- | --- | --- |
| Kotlin and Rust use protocol v13 together. | Extension protocol | Rust connector | Unsupported version is explicit. | `pnpm rust:test` | Re-run after both consumers change. |
"""
AUTHORITY_COLUMNS = "| Authority ID | Selector | Reviewed SHA-256 | In-scope families |"
COVERAGE_COLUMNS = "| Authority / invariant family | Required distinction | Required consumer / production observation | Disposition | Brief owner | Cheapest counterexample |"
REVIEW_COLUMNS = "| Authority / invariant family | Brief owner | Verdict | Cheapest counterexample result |"
LIFECYCLE_COLUMNS = "| Operation | Allowed source state | Required authority | Required observation / evidence | State and fencing effects | Nearest illegal sibling / stable rejection |"
ATTEMPT = f"""\
---
kind: work-attempt
schema: repo-work/v1
attempt: universal-reveal-core-1
item: universal-reveal-core
state: active
branch: codex/universal-reveal-core
base_revision: abc123
owner: worker-task
updated: "2026-08-16"
---

# Attempt

### {CHECKPOINT}

Checkpoint boundary: cross-boundary
Checkpoint outcome: independently-buildable

{CONTRACT_TABLE}

Implement the coherent cutover described here.

### Sequence 3 — Wire the product UI

Later work.
"""

OLD_CONTRADICTORY_PROMPT = """\
Use the bounded implementer for sequence 2 only.

Read completely attempt.md, review.md, result.md, all current guidance, and every linked source before editing.
Update canonical Kotlin vectors and matching Rust decode fixtures now, even though the Rust product adapter remains sequence 3.
Run the Kotlin checks and only the proportionate Rust format/lint checks needed for protocol model changes.
Do not begin sequence 3.
"""


def _section_bytes(text: str, heading: str) -> bytes:
    lines = text.splitlines()
    heading_line = f"### {heading}"
    start = lines.index(heading_line)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### ")), len(lines))
    return ("\n".join(lines[start:end]) + "\n").encode()


def _table_lines(section: str, header: str) -> tuple[str, ...]:
    lines = section.splitlines()
    start = lines.index(header)
    rows = [header, lines[start + 1]]
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return tuple(rows)


def _cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip()[1:-1].split("|"))


def _prepare_waiting_dispatch(
    work: str,
    project: str,
    environment: str,
    action: Action,
    started: EventType,
    result: str,
) -> None:
    started.set()
    try:
        prepare_dispatch(
            Path(work),
            Path(project),
            action,
            CHECKPOINT,
            read_dispatch_environment(Path(environment)),
        )
        outcome = "success"
    except DispatchError as error:
        outcome = error.code
    Path(result).write_text(outcome, encoding="utf-8")


class DispatchTest(unittest.TestCase):
    def _assert_resource_backed_review_validation(
        self,
        store: SQLiteWorkStore,
        roots: DurableRoots,
        project: Path,
        dispatch_action: Callable[[], Action],
        environment: DispatchEnvironment,
        candidate: bytes,
    ) -> None:
        before_unsupported = store.snapshot()
        mutating_environment = msgspec.structs.replace(
            environment,
            permissions=(DispatchPermission.REPOSITORY_READ, DispatchPermission.REPOSITORY_WRITE),
        )
        for candidate_value, review_id_value, code in (
            (None, None, "DISPATCH_BRIEF_REVIEW_MISSING"),
            (candidate, "Invalid Review", "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID"),
        ):
            with self.subTest(resource_backed_validation=code), self.assertRaises(DispatchError) as rejected:
                prepare_sqlite_dispatch(
                    store,
                    ArtifactRepository(roots),
                    prepare_dispatch_from_artifact,
                    project,
                    dispatch_action(),
                    CHECKPOINT,
                    mutating_environment,
                    brief_review=candidate_value,
                    review_id=review_id_value,
                )
            self.assertEqual(code, rejected.exception.code)
            self.assertEqual(before_unsupported, store.snapshot())
        with self.assertRaises(DispatchError) as unsupported:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                dispatch_action(),
                CHECKPOINT,
                mutating_environment,
                brief_review=candidate,
                review_id="validated-but-unpublished",
            )
        self.assertEqual("RESOURCE_BACKED_MUTATING_DISPATCH_UNSUPPORTED", unsupported.exception.code)
        self.assertEqual(before_unsupported, store.snapshot())

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def _reviewed_attempt(self, project: Path, *, lifecycle: str | None = None) -> str:
        architecture = "# Architecture\r\n\r\n## Protocol semantics\r\n\r\nProtocol v13 is shared.\r\n\r\n## Other\r\n\r\nNot selected.\r\n"
        plan = b'{"consumer":"rust"}\n'
        (project / "architecture.md").write_bytes(architecture.encode())
        (project / "plan.json").write_bytes(plan)
        architecture_digest = hashlib.sha256(b"## Protocol semantics\n\nProtocol v13 is shared.\n\n").hexdigest()
        plan_digest = hashlib.sha256(plan).hexdigest()
        lifecycle_text = (
            "Lifecycle partition: not-applicable — this protocol cutover changes no lifecycle operation."
            if lifecycle is None
            else lifecycle
        )
        checkpoint = f"""\
### {CHECKPOINT}

Checkpoint boundary: cross-boundary
Checkpoint outcome: independently-buildable

#### Reviewed authorities

{AUTHORITY_COLUMNS}
| --- | --- | --- | --- |
| architecture | `architecture.md#Protocol semantics` | `{architecture_digest}` | protocol-contract |
| plan | `plan.json` | `{plan_digest}` | consumer-proof |

{CONTRACT_TABLE}

#### Authoritative coverage

{COVERAGE_COLUMNS}
| --- | --- | --- | --- | --- | --- |
| `authority:architecture#protocol-contract` | The protocol version remains shared. | Kotlin and Rust protocol consumers | contract | `contract:{CONTRACT_INVARIANT}` | Keep Kotlin on v12 while Rust moves to v13. |
| `authority:plan#consumer-proof` | The Rust consumer is verified directly. | Rust connector | acceptance | `criterion:1` | Delete the Rust protocol test. |

{lifecycle_text}

#### Acceptance criteria

1. The production Rust connector accepts protocol v13.

Implement the coherent cutover described here.
"""
        return f"""\
---
kind: work-attempt
schema: repo-work/v1
attempt: universal-reveal-core-1
item: universal-reveal-core
state: active
branch: codex/universal-reveal-core
base_revision: abc123
owner: worker-task
owner_task_id: worker-task
updated: "2026-08-16"
---

# Attempt

{checkpoint}

### Sequence 3 — Wire the product UI

Later work.
"""

    def _write_review(
        self,
        work: Path,
        *,
        status: str = "complete",
        verdict: str = "ready",
        reviewer: str = "brief-reviewer-task",
        authority_set_digest: str | None = None,
        omit_last_row: bool = False,
    ) -> Path:
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        section_bytes = _section_bytes(attempt_path.read_text(encoding="utf-8"), CHECKPOINT)
        section = section_bytes.decode()
        authority_table = _table_lines(section, AUTHORITY_COLUMNS)
        coverage_rows = _table_lines(section, COVERAGE_COLUMNS)[2:]
        if omit_last_row:
            coverage_rows = coverage_rows[:-1]
        review_rows = "\n".join(
            f"| {cells[0]} | {cells[4]} | covered | Counterexample rejected. |"
            for cells in (_cells(row) for row in coverage_rows)
        )
        checkpoint_digest = hashlib.sha256(section_bytes).hexdigest()
        expected_authority_digest = hashlib.sha256(("\n".join(authority_table) + "\n").encode()).hexdigest()
        review = f"""\
---
kind: work-brief-review
schema: repo-work/v2
attempt: universal-reveal-core-1
checkpoint: "{CHECKPOINT}"
checkpoint_sha256: "{checkpoint_digest}"
reviewed_authority_set_sha256: "{authority_set_digest or expected_authority_digest}"
reviewer_task_id: "{reviewer}"
status: {status}
verdict: {verdict}
---

# Brief review

{REVIEW_COLUMNS}
| --- | --- | --- | --- |
{review_rows}
"""
        review_dir = attempt_path.parent / "brief-reviews"
        review_dir.mkdir(exist_ok=True)
        path = review_dir / f"{checkpoint_digest}.md"
        path.write_text(review, encoding="utf-8")
        return path

    def active_state(self, *, local: bool = False) -> tuple[Path, Path, Path]:
        project, work = create_state(
            ["| universal-reveal-core | active | — | — | universal-reveal-core-1 | design | continue | Active. |"],
            focus_item="universal-reveal-core",
            focus_attempt="universal-reveal-core-1",
            create_active_attempt=True,
        )
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        if local:
            local_attempt = ATTEMPT.replace(
                "Checkpoint boundary: cross-boundary", "Checkpoint boundary: local"
            ).replace(f"Checkpoint outcome: independently-buildable\n\n{CONTRACT_TABLE}\n", "")
            attempt_path.write_text(local_attempt, encoding="utf-8")
        else:
            attempt_path.write_text(self._reviewed_attempt(project), encoding="utf-8")
            self._write_review(work)
        environment_path = Path(tempfile.mkdtemp()) / "environment.json"
        environment_path.write_text(
            json.dumps(
                {
                    "schema": "repo-work-dispatch/v1",
                    "checkout": str(project),
                    "branch": "codex/universal-reveal-core",
                    "starting_revision": "def456",
                    "permissions": ["repository-read", "repository-write"],
                }
            ),
            encoding="utf-8",
        )
        return project, work, environment_path

    def dispatch_arguments(
        self,
        project: Path,
        work: Path,
        environment: Path,
        *,
        brief_review: Path | None = None,
        review_id: str | None = None,
    ) -> tuple[str, ...]:
        action = next(
            action
            for action in actions_for(work, project, "coordinator")
            if action.action_id == "dispatch:universal-reveal-core-1"
        )
        arguments = (
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "dispatch",
            "--action-id",
            action.action_id,
            "--expected-revision",
            action.expected_revision,
            "--generation",
            str(action.coordinator_generation),
            "--checkpoint",
            CHECKPOINT,
            "--environment",
            str(environment),
        )
        if brief_review is not None:
            arguments += ("--brief-review", str(brief_review))
        if review_id is not None:
            arguments += ("--review-id", review_id)
        return arguments

    def test_real_contradictory_launch_is_rejected_but_canonical_launch_is_accepted(self) -> None:
        project, work, environment = self.active_state()
        arguments = self.dispatch_arguments(project, work, environment)

        render_result, canonical_prompt, render_stderr = self.run_cli(*arguments)
        canonical_path = project / "canonical-prompt.txt"
        canonical_path.write_text(canonical_prompt, encoding="utf-8")
        old_path = project / "old-prompt.txt"
        old_path.write_text(OLD_CONTRADICTORY_PROMPT, encoding="utf-8")

        rejected_result, _, rejected_stderr = self.run_cli(*arguments, "--prompt", str(old_path))
        accepted_result, accepted_stdout, accepted_stderr = self.run_cli(*arguments, "--prompt", str(canonical_path))

        self.assertEqual(0, render_result, render_stderr)
        self.assertIn("Use $deliver", canonical_prompt)
        self.assertIn("sole semantic execution contract", canonical_prompt)
        self.assertNotIn("pnpm rust:test", canonical_prompt)
        self.assertEqual(14, rejected_result)
        self.assertIn("DISPATCH_PROMPT_NOT_CANONICAL", rejected_stderr)
        self.assertIn("adds or changes instructions", rejected_stderr)
        self.assertEqual(0, accepted_result, accepted_stderr)
        self.assertIn("OK DISPATCH_READY", accepted_stdout)

    def test_cross_boundary_checkpoint_requires_a_complete_contract_table(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(
            attempt_path.read_text(encoding="utf-8").replace("Rust connector", "—", 1), encoding="utf-8"
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_CONTRACT_INCOMPLETE", stderr)
        self.assertIn("Required consumer or production observation", stderr)

    def test_contract_table_requires_a_markdown_separator(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        invalid_separator = "| not | a | markdown | table | separator | row |"
        attempt_path.write_text(
            attempt_path.read_text(encoding="utf-8").replace(
                "| --- | --- | --- | --- | --- | --- |", invalid_separator, 1
            ),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_CONTRACT_INVALID", stderr)

    def test_cross_boundary_checkpoint_must_be_independently_buildable(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(
            attempt_path.read_text(encoding="utf-8").replace(
                "Checkpoint outcome: independently-buildable", "Checkpoint outcome: partial"
            ),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_CHECKPOINT_NOT_BUILDABLE", stderr)

    def test_checkpoint_requires_an_explicit_boundary_classification(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(
            attempt_path.read_text(encoding="utf-8").replace("Checkpoint boundary: cross-boundary\n", ""),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_BOUNDARY_MISSING", stderr)

    def test_local_checkpoint_does_not_require_a_contract_table(self) -> None:
        project, work, environment = self.active_state(local=True)
        arguments = self.dispatch_arguments(project, work, environment)

        result, prompt, stderr = self.run_cli(*arguments)

        self.assertEqual(0, result, stderr)
        self.assertIn("sole semantic execution contract", prompt)

    def test_authority_coverage_and_owner_failures_reject_before_launch(self) -> None:
        cases = (
            (
                "absent declared family",
                "| `authority:plan#consumer-proof` | The Rust consumer is verified directly. | Rust connector | acceptance | `criterion:1` | Delete the Rust protocol test. |",
                "",
            ),
            (
                "unresolved contract owner",
                f"`contract:{CONTRACT_INVARIANT}`",
                "`contract:Missing invariant`",
            ),
            (
                "unknown family",
                "authority:plan#consumer-proof",
                "authority:plan#unknown-family",
            ),
        )
        for name, old, new in cases:
            with self.subTest(name=name):
                project, work, environment = self.active_state()
                attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
                attempt_path.write_text(attempt_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                self._write_review(work)

                result, _, stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

                self.assertEqual(14, result)
                self.assertIn("DISPATCH_AUTHORITY_COVERAGE_INVALID", stderr)

    def test_changed_reviewed_source_rejects_but_unselected_heading_bytes_do_not(self) -> None:
        project, work, environment = self.active_state()
        architecture = project / "architecture.md"
        architecture.write_bytes(architecture.read_bytes().replace(b"Not selected.", b"Still not selected."))

        accepted, _, accepted_stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

        self.assertEqual(0, accepted, accepted_stderr)
        architecture.write_bytes(
            architecture.read_bytes().replace(b"Protocol v13 is shared.", b"Protocol v14 is shared.")
        )
        rejected, _, rejected_stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))
        self.assertEqual(14, rejected)
        self.assertIn("DISPATCH_AUTHORITY_STALE", rejected_stderr)

        project, work, environment = self.active_state()
        plan = project / "plan.json"
        plan.write_bytes(plan.read_bytes().replace(b"\n", b"\r\n"))
        rejected, _, rejected_stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))
        self.assertEqual(14, rejected)
        self.assertIn("DISPATCH_AUTHORITY_STALE", rejected_stderr)

    def test_heading_selectors_support_h1_through_h6_and_literal_hash_characters(self) -> None:
        cases = (
            ("h1", "# Protocol semantics", "Protocol semantics"),
            ("h2 with literal hash", "## Protocol # semantics", "Protocol # semantics"),
            ("h3", "### Protocol semantics", "Protocol semantics"),
            ("h4", "#### Protocol semantics", "Protocol semantics"),
            ("h5", "##### Protocol semantics", "Protocol semantics"),
            ("h6", "###### Protocol semantics", "Protocol semantics"),
        )
        old_digest = hashlib.sha256(b"## Protocol semantics\n\nProtocol v13 is shared.\n\n").hexdigest()
        for name, heading_line, selector_heading in cases:
            with self.subTest(name=name):
                project, work, environment = self.active_state()
                source = f"{heading_line}\r\n\r\nProtocol v13 is shared.\r\n\r\n# Other\r\n\r\nNot selected.\r\n"
                (project / "architecture.md").write_bytes(source.encode())
                selected = f"{heading_line}\n\nProtocol v13 is shared.\n\n".encode()
                digest = hashlib.sha256(selected).hexdigest()
                attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
                attempt_path.write_text(
                    attempt_path.read_text(encoding="utf-8")
                    .replace("architecture.md#Protocol semantics", f"architecture.md#{selector_heading}")
                    .replace(old_digest, digest),
                    encoding="utf-8",
                )
                self._write_review(work)

                result, _, stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

                self.assertEqual(0, result, stderr)

    def test_padded_same_owner_reviewer_identity_rejects(self) -> None:
        project, work, environment = self.active_state()
        review_path = next((work / "attempts" / "universal-reveal-core-1" / "brief-reviews").glob("*.md"))
        review_path.unlink()
        self._write_review(work, reviewer=" worker-task ")

        result, _, stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT", stderr)

    def test_ready_review_publication_reuses_identical_and_preserves_differing_collision(self) -> None:
        project, work, environment = self.active_state()
        review_dir = work / "attempts" / "universal-reveal-core-1" / "brief-reviews"
        ready_path = next(review_dir.glob("*.md"))
        ready_bytes = ready_path.read_bytes()
        ready_path.unlink()
        candidate_path = project / "ready-review.md"
        candidate_path.write_bytes(ready_bytes)

        created, _, created_stderr = self.run_cli(
            *self.dispatch_arguments(
                project,
                work,
                environment,
                brief_review=candidate_path,
                review_id="reviewer-a",
            )
        )
        reused, _, reused_stderr = self.run_cli(
            *self.dispatch_arguments(
                project,
                work,
                environment,
                brief_review=candidate_path,
                review_id="reviewer-a",
            )
        )
        later_bytes = ready_bytes.replace(b"Counterexample rejected.", b"Counterexample rejected again.")
        later_path = project / "later-ready-review.md"
        later_path.write_bytes(later_bytes)
        collided, _, collided_stderr = self.run_cli(
            *self.dispatch_arguments(
                project,
                work,
                environment,
                brief_review=later_path,
                review_id="reviewer-b",
            )
        )

        rejected_path = review_dir / "rejected" / f"{ready_path.stem}-reviewer-b.md"
        self.assertEqual(0, created, created_stderr)
        self.assertEqual(0, reused, reused_stderr)
        self.assertEqual(14, collided)
        self.assertIn("DISPATCH_BRIEF_REVIEW_COLLISION", collided_stderr)
        self.assertEqual(ready_bytes, ready_path.read_bytes())
        self.assertEqual(later_bytes, rejected_path.read_bytes())

    def test_lifecycle_partition_shape_and_review_truth_are_separate(self) -> None:
        lifecycle_table = f"""\
Lifecycle partition: required

#### Lifecycle partition

{LIFECYCLE_COLUMNS}
| --- | --- | --- | --- | --- | --- |
| direct-preserve | active | human authorization | exact observation | revoke and fence | quarantined / stable rejection |
"""
        collapsed_lifecycle_table = lifecycle_table.replace(
            "| direct-preserve | active |", "| direct-preserve | active or quarantined |"
        )
        cases = (
            ("required and reviewed", lifecycle_table, "ready", 0, ""),
            (
                "required without table",
                "Lifecycle partition: required",
                "ready",
                14,
                "DISPATCH_LIFECYCLE_PARTITION_INVALID",
            ),
            (
                "full shape but rejected semantics",
                collapsed_lifecycle_table,
                "rejected",
                14,
                "DISPATCH_BRIEF_REVIEW_NOT_READY",
            ),
        )
        for name, lifecycle, verdict, expected_result, expected_error in cases:
            with self.subTest(name=name):
                project, work, environment = self.active_state()
                attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
                attempt_path.write_text(self._reviewed_attempt(project, lifecycle=lifecycle), encoding="utf-8")
                self._write_review(work, verdict=verdict)

                result, _, stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

                self.assertEqual(expected_result, result, stderr)
                if expected_error:
                    self.assertIn(expected_error, stderr)

    def test_absent_stale_mismatched_incomplete_or_nonindependent_review_rejects(self) -> None:
        cases = (
            ("absent", "DISPATCH_BRIEF_REVIEW_MISSING"),
            ("stale", "DISPATCH_BRIEF_REVIEW_MISSING"),
            ("authority", "DISPATCH_BRIEF_REVIEW_STALE"),
            ("nonready", "DISPATCH_BRIEF_REVIEW_NOT_READY"),
            ("incomplete", "DISPATCH_BRIEF_REVIEW_INCOMPLETE"),
            ("worker", "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT"),
        )
        for mutation, expected in cases:
            with self.subTest(mutation=mutation):
                project, work, environment = self.active_state()
                attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
                review_path = next((attempt_path.parent / "brief-reviews").glob("*.md"))
                if mutation == "absent":
                    review_path.unlink()
                elif mutation == "stale":
                    attempt_path.write_text(
                        attempt_path.read_text(encoding="utf-8").replace(
                            "The production Rust connector accepts protocol v13.",
                            "The production Rust connector accepts protocol v13 exactly.",
                        ),
                        encoding="utf-8",
                    )
                elif mutation == "authority":
                    review_path.unlink()
                    self._write_review(work, authority_set_digest="b" * 64)
                elif mutation == "nonready":
                    review_path.unlink()
                    self._write_review(work, verdict="rejected")
                elif mutation == "incomplete":
                    review_path.unlink()
                    self._write_review(work, omit_last_row=True)
                elif mutation == "worker":
                    review_path.unlink()
                    self._write_review(work, reviewer="worker-task")

                result, _, stderr = self.run_cli(*self.dispatch_arguments(project, work, environment))

                self.assertEqual(14, result)
                self.assertIn(expected, stderr)

    def test_environment_branch_must_match_the_attempt(self) -> None:
        project, work, environment = self.active_state()
        environment.write_text(
            json.dumps(
                {
                    "schema": "repo-work-dispatch/v1",
                    "checkout": str(project),
                    "branch": "codex/different-branch",
                    "starting_revision": "def456",
                    "permissions": ["repository-read"],
                }
            ),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_BRANCH_MISMATCH", stderr)

    def test_coordinator_replacement_has_a_distinct_dispatch_outcome(self) -> None:
        project, work, environment = self.active_state()
        arguments = self.dispatch_arguments(project, work, environment)
        coordinator_path = work / "coordinator.json"
        registration = json.loads(coordinator_path.read_text(encoding="utf-8"))
        registration["generation"] = 2
        coordinator_path.write_text(json.dumps(registration) + "\n", encoding="utf-8")

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("COORDINATOR_REPLACED", stderr)

    def test_dispatch_waits_for_transition_lock_then_rejects_changed_state(self) -> None:
        project, work, environment = self.active_state()
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "dispatch:universal-reveal-core-1"
        )
        result_path = project / "dispatch-result.txt"
        context = multiprocessing.get_context("fork")
        started = context.Event()
        process = context.Process(
            target=_prepare_waiting_dispatch,
            args=(str(work), str(project), str(environment), action, started, str(result_path)),
        )
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"

        with transition_lock(work):
            process.start()
            self.assertTrue(started.wait(timeout=5))
            attempt_path.write_text(
                attempt_path.read_text(encoding="utf-8").replace("Later work.", "Later accepted work."),
                encoding="utf-8",
            )
        process.join(timeout=10)

        self.assertEqual(0, process.exitcode)
        self.assertEqual("STALE_ACTION", result_path.read_text(encoding="utf-8"))

    def test_sqlite_dispatch_reads_accepted_brief_and_preserves_resource_feature_limit(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        brief_bytes = b"""---
kind: work-attempt
schema: repo-work/v2
attempt: work-a-1
item: work-a
state: active
branch: codex/work-a
base_revision: base-revision
owner_task_id: worker
updated: \"2026-08-23\"
---

# Attempt

## Local implementation

Checkpoint boundary: local
Checkpoint outcome: independently-buildable
"""
        published = write_revision(
            roots,
            NewArtifact(ArtifactKind.BRIEF, "work-a", 1, ".md", brief_bytes),
        )
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        brief = replace(
            state.artifacts.references[0],
            key=published.key,
            revision=published.revision,
            selector=published.selector,
            content_sha256=published.content_sha256,
            size_bytes=published.size_bytes,
        )
        self.assertIsNotNone(state.authority.coordination)
        assert state.authority.coordination is not None
        coordination = replace(state.authority.coordination, expires_at=now + timedelta(minutes=5))
        attempt_leases = tuple(
            replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
        )
        state = replace(
            state,
            artifacts=replace(state.artifacts, references=(brief, *state.artifacts.references[1:])),
            authority=replace(state.authority, coordination=coordination, attempt_leases=attempt_leases),
        )
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        action = next(
            value
            for value in discover_actions(
                store,
                Role.COORDINATOR,
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if value.kind == ActionKind.DISPATCH
        )
        read_only = DispatchEnvironment(
            "repo-work-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )

        prompt = prepare_sqlite_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action,
            "Local implementation",
            read_only,
        )

        self.assertIn(f"Canonical brief: {roots.work_root / published.selector}", prompt)
        for changed, code in (
            (replace(action, expected_revision="stale"), "STALE_ACTION"),
            (replace(action, label="changed"), "DISPATCH_ACTION_INVALID"),
            (replace(action, kind=ActionKind.INSPECT), "DISPATCH_ACTION_UNAVAILABLE"),
            (replace(action, lease_id=LeaseId("wrong")), "COORDINATION_LEASE_REQUIRED"),
        ):
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                prepare_sqlite_dispatch(
                    store,
                    ArtifactRepository(roots),
                    prepare_dispatch_from_artifact,
                    project,
                    changed,
                    "Local implementation",
                    read_only,
                )
            self.assertEqual(code, rejected.exception.code)
        before = store.snapshot()
        mutating = msgspec.structs.replace(
            read_only,
            permissions=(DispatchPermission.REPOSITORY_READ, DispatchPermission.REPOSITORY_WRITE),
        )
        with self.assertRaises(DispatchError) as unsupported:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                action,
                "Local implementation",
                mutating,
            )
        self.assertEqual("RESOURCE_BACKED_MUTATING_DISPATCH_UNSUPPORTED", unsupported.exception.code)
        self.assertEqual(before, store.snapshot())

    def test_sqlite_cross_boundary_review_is_immutable_reusable_and_collision_preserving(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        reviewed = self._reviewed_attempt(project)
        review_work = Path(tempfile.mkdtemp())
        review_attempt = review_work / "attempts" / "universal-reveal-core-1"
        review_attempt.mkdir(parents=True)
        (review_attempt / "attempt.md").write_text(reviewed, encoding="utf-8")
        candidate = self._write_review(review_work).read_bytes().replace(b"universal-reveal-core-1", b"work-a-1")
        brief_bytes = (
            reviewed.replace("universal-reveal-core-1", "work-a-1")
            .replace("universal-reveal-core", "work-a")
            .replace("codex/work-a", "codex/work-a")
            .encode()
        )
        published = write_revision(
            roots,
            NewArtifact(ArtifactKind.BRIEF, "work-a", 1, ".md", brief_bytes),
        )
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        assert state.authority.coordination is not None
        coordination = replace(state.authority.coordination, expires_at=now + timedelta(minutes=5))
        brief = replace(
            state.artifacts.references[0],
            key=published.key,
            selector=published.selector,
            content_sha256=published.content_sha256,
            size_bytes=published.size_bytes,
        )
        state = replace(
            state,
            artifacts=replace(state.artifacts, references=(brief, *state.artifacts.references[1:])),
            authority=replace(state.authority, coordination=coordination),
        )
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
        environment = DispatchEnvironment(
            "repo-work-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )

        def dispatch_action() -> Action:
            return next(
                value
                for value in discover_actions(
                    store,
                    Role.COORDINATOR,
                    lease_id=coordination.lease_id,
                    generation=coordination.generation,
                )
                if value.kind == ActionKind.DISPATCH
            )

        self._assert_resource_backed_review_validation(store, roots, project, dispatch_action, environment, candidate)

        with self.assertRaises(DispatchError) as missing_review:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                dispatch_action(),
                CHECKPOINT,
                environment,
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_MISSING", missing_review.exception.code)
        with self.assertRaises(DispatchError) as orphaned_review_id:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                dispatch_action(),
                CHECKPOINT,
                environment,
                review_id="orphaned-review",
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", orphaned_review_id.exception.code)
        with self.assertRaises(DispatchError) as invalid_review_id:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                dispatch_action(),
                CHECKPOINT,
                environment,
                brief_review=candidate,
                review_id="Invalid Review",
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", invalid_review_id.exception.code)

        prompt = prepare_sqlite_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            dispatch_action(),
            CHECKPOINT,
            environment,
            brief_review=candidate,
            review_id="sqlite-review",
        )
        revision_after_publish = store.snapshot().lifecycle.project.revision
        self.assertIn(CHECKPOINT, prompt)
        self.assertEqual(13, revision_after_publish)

        current_action = dispatch_action()
        self.assertEqual(
            prompt,
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                current_action,
                CHECKPOINT,
                environment,
            ),
        )
        self.assertEqual(
            prompt,
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                current_action,
                CHECKPOINT,
                environment,
                brief_review=candidate,
                review_id="sqlite-review",
            ),
        )
        with self.assertRaises(DispatchError) as collision:
            prepare_sqlite_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                current_action,
                CHECKPOINT,
                environment,
                brief_review=candidate + b"\nAdditional reviewer note.\n",
                review_id="later-review",
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", collision.exception.code)
        self.assertTrue(any("rejected-later-review" in value.key for value in store.snapshot().artifacts.references))


if __name__ == "__main__":
    unittest.main()
