from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from repo_work.actions import actions_for
from repo_work.cli import main
from tests.support import create_state


class CliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def snapshot(self, work: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(work)): path.read_bytes()
            for path in work.rglob("*")
            if path.is_file()
        }

    def test_validate_json_is_read_only(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
        before = self.snapshot(work)

        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "validate",
            "--json",
        )

        self.assertEqual(0, result, stderr)
        self.assertTrue(json.loads(stdout)["valid"])
        self.assertEqual(before, self.snapshot(work))

    def test_invalid_state_has_stable_nonzero_result(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
        (work / "items" / "reveal-core.md").unlink()

        result, stdout, _ = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "validate",
        )

        self.assertEqual(10, result)
        self.assertIn("ITEM_RECORD_MISSING", stdout)

    def test_actions_json_exposes_transition_tokens(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )

        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "actions",
            "--role",
            "coordinator",
            "--json",
        )

        self.assertEqual(0, result, stderr)
        actions = json.loads(stdout)["actions"]
        activation = next(action for action in actions if action["action_id"] == "activate:reveal-core")
        self.assertEqual(64, len(activation["expected_revision"]))
        self.assertEqual(1, activation["coordinator_generation"])

    def test_transition_applies_action_from_machine_fields(self) -> None:
        project, work = create_state(
            ["| reveal-core | ready | — | — | — | design | activate | Ready. |"]
        )
        action = next(
            action
            for action in actions_for(work, project, "coordinator")
            if action.action_id == "activate:reveal-core"
        )
        payload_path = Path(tempfile.mkdtemp()) / "payload.json"
        payload_path.write_text(
            json.dumps(
                {
                    "attempt": "reveal-core-1",
                    "branch": "codex/reveal-core",
                    "base_revision": "abc123",
                    "owner": "worker-task",
                }
            ),
            encoding="utf-8",
        )

        result, stdout, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "transition",
            "--action-id",
            action.action_id,
            "--expected-revision",
            action.expected_revision,
            "--generation",
            str(action.coordinator_generation),
            "--payload",
            str(payload_path),
        )

        self.assertEqual(0, result, stderr)
        self.assertIn("TRANSITION_APPLIED", stdout)


if __name__ == "__main__":
    unittest.main()
