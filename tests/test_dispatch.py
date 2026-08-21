import contextlib
import io
import json
import multiprocessing
import tempfile
import unittest
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

from repo_work.actions import Action, actions_for
from repo_work.atomic import transition_lock
from repo_work.cli import main
from repo_work.dispatch import DispatchError, prepare_dispatch, read_dispatch_environment

from .support import create_state

CHECKPOINT = "Sequence 2 — Complete the shared protocol cutover"
CONTRACT_TABLE = """\
#### Contract table

| Invariant | Authority / owner | Required consumer or production observation | Failure classification | Exact verification | Preflight / final revalidation |
| --- | --- | --- | --- | --- | --- |
| Kotlin and Rust use protocol v13 together. | Extension protocol | Rust connector | Unsupported version is explicit. | `pnpm rust:test` | Re-run after both consumers change. |
"""
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
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def active_state(self) -> tuple[Path, Path, Path]:
        project, work = create_state(
            ["| universal-reveal-core | active | — | — | universal-reveal-core-1 | design | continue | Active. |"],
            focus_item="universal-reveal-core",
            focus_attempt="universal-reveal-core-1",
            create_active_attempt=True,
        )
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(ATTEMPT, encoding="utf-8")
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

    def dispatch_arguments(self, project: Path, work: Path, environment: Path) -> tuple[str, ...]:
        action = next(
            action
            for action in actions_for(work, project, "coordinator")
            if action.action_id == "dispatch:universal-reveal-core-1"
        )
        return (
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
        attempt_path.write_text(ATTEMPT.replace("Rust connector", "—"), encoding="utf-8")
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
            ATTEMPT.replace("| --- | --- | --- | --- | --- | --- |", invalid_separator),
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
            ATTEMPT.replace("Checkpoint outcome: independently-buildable", "Checkpoint outcome: partial"),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_CHECKPOINT_NOT_BUILDABLE", stderr)

    def test_checkpoint_requires_an_explicit_boundary_classification(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(ATTEMPT.replace("Checkpoint boundary: cross-boundary\n", ""), encoding="utf-8")
        arguments = self.dispatch_arguments(project, work, environment)

        result, _, stderr = self.run_cli(*arguments)

        self.assertEqual(14, result)
        self.assertIn("DISPATCH_BOUNDARY_MISSING", stderr)

    def test_local_checkpoint_does_not_require_a_contract_table(self) -> None:
        project, work, environment = self.active_state()
        attempt_path = work / "attempts" / "universal-reveal-core-1" / "attempt.md"
        attempt_path.write_text(
            ATTEMPT.replace("Checkpoint boundary: cross-boundary", "Checkpoint boundary: local").replace(
                f"{CONTRACT_TABLE}\n", ""
            ),
            encoding="utf-8",
        )
        arguments = self.dispatch_arguments(project, work, environment)

        result, prompt, stderr = self.run_cli(*arguments)

        self.assertEqual(0, result, stderr)
        self.assertIn("sole semantic execution contract", prompt)

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
            attempt_path.write_text(ATTEMPT.replace("Later work.", "Later accepted work."), encoding="utf-8")
        process.join(timeout=10)

        self.assertEqual(0, process.exitcode)
        self.assertEqual("STALE_ACTION", result_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
