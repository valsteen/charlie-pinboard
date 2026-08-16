import contextlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_work.actions import actions_for
from repo_work.cli import main
from repo_work.root import RootError

from .support import create_state


class CliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def snapshot(self, work: Path) -> dict[str, bytes]:
        return {str(path.relative_to(work)): path.read_bytes() for path in work.rglob("*") if path.is_file()}

    def test_validate_json_is_read_only(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
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
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
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
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])

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
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = next(
            action for action in actions_for(work, project, "coordinator") if action.action_id == "activate:reveal-core"
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

    def test_root_status_and_actions_have_human_and_json_views(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        common = ("--project-root", str(project), "--work-root", str(work))

        root_result, root_stdout, _ = self.run_cli(*common, "root")
        status_result, status_stdout, _ = self.run_cli(*common, "status")
        status_json_result, status_json_stdout, _ = self.run_cli(*common, "status", "--json")
        actions_result, actions_stdout, _ = self.run_cli(*common, "actions", "--role", "observer")

        self.assertEqual(0, root_result)
        self.assertEqual(str(work), json.loads(root_stdout)["work_root"])
        self.assertEqual(0, status_result)
        self.assertIn("focus_item=none", status_stdout)
        self.assertEqual(0, status_json_result)
        self.assertEqual(1, json.loads(status_json_stdout)["counts"]["ready"])
        self.assertEqual(0, actions_result)
        self.assertIn("inspect:ledger", actions_stdout)

    def test_init_and_proposal_commands_use_installed_package_paths(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        init_result, init_stdout, init_stderr = self.run_cli(
            *common,
            "init",
            "--coordinator-task-id",
            "coordinator",
            "--host-id",
            "local",
        )
        proposal_path = project / "proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "repo-work/v1",
                    "proposal_id": "finding-1",
                    "created_at": "2026-08-16T12:30:00Z",
                    "source_task_id": "investigation",
                    "user_label": "Reveal ownership",
                    "trigger": "Ownership is coupled.",
                    "evidence": ["source"],
                    "why_it_matters": "Changes are wider.",
                    "relation": {"kind": "independent", "item": None},
                    "effect": "Ownership narrows.",
                    "unlock": "Consumers reuse it.",
                    "urgency_evidence": "Current objective.",
                    "freshness_assumptions": ["Code is current."],
                }
            ),
            encoding="utf-8",
        )
        proposal_result, proposal_stdout, proposal_stderr = self.run_cli(
            *common, "proposal", "--file", str(proposal_path)
        )

        self.assertEqual(0, init_result, init_stderr)
        self.assertIn("WORK_STATE_INITIALIZED", init_stdout)
        self.assertEqual(0, proposal_result, proposal_stderr)
        self.assertIn("PROPOSAL_CREATED", proposal_stdout)

    def test_cli_maps_root_registration_and_json_failures_to_stable_results(self) -> None:
        project, work = create_state([])
        common = ("--project-root", str(project), "--work-root", str(work))
        invalid = project / "invalid.json"
        invalid.write_text("[]", encoding="utf-8")

        registration_result, _, registration_stderr = self.run_cli(
            *common,
            "init",
            "--coordinator-task-id",
            "replacement",
            "--host-id",
            "local",
        )
        proposal_result, _, proposal_stderr = self.run_cli(*common, "proposal", "--file", str(invalid))
        with patch("repo_work.cli.resolve_project_root", side_effect=RootError("PROJECT_ROOT_NOT_FOUND", "missing")):
            root_result, _, root_stderr = self.run_cli("root")

        self.assertEqual(12, registration_result)
        self.assertIn("WORK_STATE_ALREADY_EXISTS", registration_stderr)
        self.assertEqual(2, proposal_result)
        self.assertIn("root must be an object", proposal_stderr)
        self.assertEqual(2, root_result)
        self.assertIn("PROJECT_ROOT_NOT_FOUND", root_stderr)

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["repo-work", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("repo_work.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
