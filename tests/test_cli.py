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

    def test_v2_init_needs_no_master_task_and_coordination_is_borrowed(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        init_result, _, init_stderr = self.run_cli(*common, "init")
        status_result, status_stdout, status_stderr = self.run_cli(*common, "status", "--json")
        acquire_result, acquire_stdout, acquire_stderr = self.run_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
            "--json",
        )

        self.assertEqual(0, init_result, init_stderr)
        self.assertEqual(0, status_result, status_stderr)
        self.assertEqual("v2", json.loads(status_stdout)["authority"])
        self.assertIsNone(json.loads(status_stdout)["coordinator"])
        self.assertEqual(0, acquire_result, acquire_stderr)
        self.assertEqual("chat-a", json.loads(acquire_stdout)["task_id"])

    def test_v1_lease_and_resource_commands_require_explicit_migration(self) -> None:
        project, work = create_state([])
        common = ("--project-root", str(project), "--work-root", str(work))
        commands = (
            ("coordination", "status"),
            ("attempt", "status", "--attempt-id", "attempt-1"),
            ("resource", "status", "--resource-id", "bitwig-live"),
        )
        for command in commands:
            with self.subTest(command=command):
                result, _, stderr = self.run_cli(*common, *command)
                self.assertEqual(11, result)
                self.assertIn("MIGRATION_REQUIRED", stderr)
                self.assertIn("repo-work migrate --to v2", stderr)

    def test_parallel_preview_has_machine_and_human_views_and_exact_selection(self) -> None:
        project, work = create_state(
            [
                "| alpha | ready | — | — | — | design | activate | Ready. |",
                "| foundation | intake | — | — | — | finding | classify | Intake. |",
            ]
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "migrate", "--to", "v2")[0])

        json_result, json_stdout, json_stderr = self.run_cli(
            *common,
            "parallel",
            "preview",
            "--host-id",
            "studio",
            "--json",
        )
        selected_result, selected_stdout, selected_stderr = self.run_cli(
            *common,
            "parallel",
            "preview",
            "--host-id",
            "studio",
            "--item",
            "alpha",
            "--json",
        )
        human_result, human_stdout, human_stderr = self.run_cli(
            *common,
            "parallel",
            "preview",
            "--host-id",
            "studio",
        )

        self.assertEqual(0, json_result, json_stderr)
        preview = json.loads(json_stdout)
        self.assertEqual("repo-work-parallel-preview/v1", preview["schema"])
        self.assertEqual("all-safe", preview["selection"])
        self.assertTrue(preview["safe"])
        self.assertEqual(["alpha"], [item["item_id"] for item in preview["launchable"]])
        self.assertEqual(
            ["state-not-launchable"],
            [reason["code"] for reason in preview["excluded"][0]["reasons"]],
        )
        self.assertEqual(0, selected_result, selected_stderr)
        self.assertEqual("selected", json.loads(selected_stdout)["selection"])
        self.assertEqual(0, human_result, human_stderr)
        self.assertIn("Ready to launch together", human_stdout)
        self.assertIn("Not launchable", human_stdout)

    def test_parallel_preview_requires_schema_v2(self) -> None:
        project, work = create_state(["| alpha | ready | — | — | — | design | activate | Ready. |"])
        result, _, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "parallel",
            "preview",
            "--host-id",
            "studio",
        )

        self.assertEqual(11, result)
        self.assertIn("MIGRATION_REQUIRED", stderr)

    def test_cli_path_identities_cannot_escape_v2_authoritative_directories(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "init")[0])
        commands = (
            (
                "coordination",
                "acquire",
                "--task-id",
                "task",
                "--host-id",
                "../../escape",
                "--ttl-seconds",
                "60",
            ),
            ("attempt", "status", "--attempt-id", "../../escape"),
            ("resource", "status", "--resource-id", "bitwig-live", "--host-id", "../../escape"),
        )
        for command in commands:
            with self.subTest(command=command):
                result, _, stderr = self.run_cli(*common, *command)
                self.assertEqual(11, result)
                self.assertRegex(stderr, "IDENTITY_INVALID|ID_INVALID")
        self.assertFalse((work / "escape").exists())
        self.assertFalse((project / "escape").exists())

    def test_migrate_and_v2_actions_expose_lease_authority(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        common = ("--project-root", str(project), "--work-root", str(work))
        migrate_result, migrate_stdout, migrate_stderr = self.run_cli(*common, "migrate", "--to", "v2", "--json")
        acquire_result, acquire_stdout, acquire_stderr = self.run_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
            "--json",
        )
        lease = json.loads(acquire_stdout)
        actions_result, actions_stdout, actions_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "coordinator",
            "--lease-id",
            lease["lease_id"],
            "--generation",
            str(lease["generation"]),
            "--json",
        )

        self.assertEqual(0, migrate_result, migrate_stderr)
        self.assertTrue(json.loads(migrate_stdout)["cutover"])
        self.assertEqual(0, acquire_result, acquire_stderr)
        self.assertEqual(0, actions_result, actions_stderr)
        activation = next(value for value in json.loads(actions_stdout)["actions"] if value["kind"] == "activate")
        self.assertEqual("coordination", activation["authorization"])
        self.assertEqual(lease["lease_id"], activation["lease_id"])

    def test_v2_lease_and_resource_commands_cover_the_concurrent_chat_lifecycle(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        self.assertEqual(0, self.run_cli(*common, "migrate", "--to", "v2")[0])

        def json_command(*arguments: str) -> dict[str, str | int]:
            result, stdout, stderr = self.run_cli(*common, *arguments, "--json")
            self.assertEqual(0, result, stderr)
            value = json.loads(stdout)
            self.assertIsInstance(value, dict)
            return value

        coordination = json_command(
            "coordination",
            "acquire",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
        )
        json_command("coordination", "status")
        coordination = json_command(
            "coordination",
            "renew",
            "--lease-id",
            str(coordination["lease_id"]),
            "--generation",
            str(coordination["generation"]),
            "--ttl-seconds",
            "120",
        )
        json_command(
            "resource",
            "declare",
            "--resource-id",
            "bitwig-live",
            "--label",
            "Bitwig live application",
            "--scope",
            "host-local",
            "--coordination-lease-id",
            str(coordination["lease_id"]),
            "--coordination-generation",
            str(coordination["generation"]),
        )
        attempt = json_command(
            "attempt",
            "acquire",
            "--attempt-id",
            "reveal-core-1",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "120",
        )
        json_command("attempt", "status", "--attempt-id", "reveal-core-1")
        attempt = json_command(
            "attempt",
            "renew",
            "--attempt-id",
            "reveal-core-1",
            "--lease-id",
            str(attempt["lease_id"]),
            "--generation",
            str(attempt["generation"]),
            "--ttl-seconds",
            "180",
        )
        claim = json_command(
            "resource",
            "claim",
            "--resource-id",
            "bitwig-live",
            "--attempt-id",
            "reveal-core-1",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
            "--attempt-lease-id",
            str(attempt["lease_id"]),
            "--attempt-generation",
            str(attempt["generation"]),
        )
        item_path = work / "v2" / "items" / "reveal-core.md"
        item_path.write_text(
            item_path.read_text(encoding="utf-8").replace("resources: —", "resources: bitwig-live"),
            encoding="utf-8",
        )
        actions_result, actions_stdout, actions_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "worker",
            "--lease-id",
            str(attempt["lease_id"]),
            "--generation",
            str(attempt["generation"]),
            "--json",
        )
        self.assertEqual(0, actions_result, actions_stderr)
        submit = next(
            action
            for action in json.loads(actions_stdout)["actions"]
            if action["action_id"] == "submit-review:reveal-core-1"
        )
        self.assertEqual(
            [
                {
                    "generation": claim["generation"],
                    "host_id": "mac--one",
                    "lease_id": claim["lease_id"],
                    "resource_id": "bitwig-live",
                }
            ],
            submit["resource_claims"],
        )
        payload = Path(tempfile.mkdtemp()) / "submit.json"
        payload.write_text("{}\n", encoding="utf-8")
        transition_result, _, transition_stderr = self.run_cli(
            *common,
            "transition",
            "--action-id",
            submit["action_id"],
            "--expected-revision",
            submit["expected_revision"],
            "--generation",
            str(submit["coordinator_generation"]),
            "--subject-revision",
            submit["subject_revision"],
            "--lease-id",
            submit["lease_id"],
            "--authorization",
            submit["authorization"],
            "--resource-claim",
            "bitwig-live",
            "mac--one",
            str(claim["lease_id"]),
            str(claim["generation"]),
            "--payload",
            str(payload),
        )
        self.assertEqual(0, transition_result, transition_stderr)
        json_command("resource", "status", "--resource-id", "bitwig-live")
        json_command("resource", "status", "--resource-id", "bitwig-live", "--host-id", "mac--one")
        validate_result, _, validate_stderr = self.run_cli(*common, "validate")
        self.assertEqual(0, validate_result, validate_stderr)
        claim = json_command(
            "resource",
            "renew",
            "--resource-id",
            "bitwig-live",
            "--host-id",
            "mac--one",
            "--lease-id",
            str(claim["lease_id"]),
            "--generation",
            str(claim["generation"]),
            "--ttl-seconds",
            "120",
        )
        json_command(
            "resource",
            "release",
            "--resource-id",
            "bitwig-live",
            "--host-id",
            "mac--one",
            "--lease-id",
            str(claim["lease_id"]),
            "--generation",
            str(claim["generation"]),
        )
        claim = json_command(
            "resource",
            "claim",
            "--resource-id",
            "bitwig-live",
            "--attempt-id",
            "reveal-core-1",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
            "--attempt-lease-id",
            str(attempt["lease_id"]),
            "--attempt-generation",
            str(attempt["generation"]),
        )
        revoked_claim = json_command(
            "resource",
            "revoke",
            "--resource-id",
            "bitwig-live",
            "--host-id",
            "mac--one",
            "--coordination-lease-id",
            str(coordination["lease_id"]),
            "--coordination-generation",
            str(coordination["generation"]),
        )
        self.assertGreater(int(revoked_claim["generation"]), int(claim["generation"]))

        json_command(
            "attempt",
            "release",
            "--attempt-id",
            "reveal-core-1",
            "--lease-id",
            str(attempt["lease_id"]),
            "--generation",
            str(attempt["generation"]),
        )
        attempt = json_command(
            "attempt",
            "acquire",
            "--attempt-id",
            "reveal-core-1",
            "--task-id",
            "chat-b",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
        )
        revoked_attempt = json_command(
            "attempt",
            "revoke",
            "--attempt-id",
            "reveal-core-1",
            "--coordination-lease-id",
            str(coordination["lease_id"]),
            "--coordination-generation",
            str(coordination["generation"]),
        )
        self.assertGreater(int(revoked_attempt["generation"]), int(attempt["generation"]))

        json_command(
            "coordination",
            "release",
            "--lease-id",
            str(coordination["lease_id"]),
            "--generation",
            str(coordination["generation"]),
        )
        replacement = json_command(
            "coordination",
            "acquire",
            "--task-id",
            "chat-b",
            "--host-id",
            "mac--one",
            "--ttl-seconds",
            "60",
        )
        revoked_coordination = json_command("coordination", "revoke")
        self.assertGreater(int(revoked_coordination["generation"]), int(replacement["generation"]))

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
        self.assertIn("Expected `object`, got `array`", proposal_stderr)
        self.assertEqual(2, root_result)
        self.assertIn("PROJECT_ROOT_NOT_FOUND", root_stderr)

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["repo-work", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("repo_work.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
