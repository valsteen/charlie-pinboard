import contextlib
import io
import json
import runpy
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.files.root import RootError
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.stored_state import StoredWorkState
from charlie_pinboard.domain.model import AttemptState, WorkState
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.interfaces.transitions import apply_action as apply_transition
from charlie_pinboard.legacy.actions import actions_for, state_revision
from charlie_pinboard.legacy.atomic import atomic_write_text as real_atomic_write_text
from charlie_pinboard.legacy.leases import LeaseRecord
from charlie_pinboard.legacy.leases import acquire_coordination as real_acquire_coordination
from charlie_pinboard.legacy.leases import release_coordination as real_release_coordination
from charlie_pinboard.legacy.markdown import parse_attempt, parse_current, parse_queue
from charlie_pinboard.legacy.migration import migrate_to_v2
from charlie_pinboard.legacy.transaction_store import journal_path_for
from charlie_pinboard.legacy.validate import validate_work_state

from .support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, create_state


class CliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def snapshot(self, work: Path) -> dict[str, bytes]:
        return {str(path.relative_to(work)): path.read_bytes() for path in work.rglob("*") if path.is_file()}

    def run_json_cli(self, *arguments: str) -> JsonObject:
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, stderr)
        value = json.loads(stdout)
        if not isinstance(value, dict):
            self.fail("CLI JSON result must be an object")
        return value

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def json_list(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("JSON value must be a list")
        return value

    def json_string(self, value: JsonValue) -> str:
        if not isinstance(value, str):
            self.fail("JSON value must be a string")
        return value

    def json_object_at(self, value: JsonObject, *keys: str) -> JsonObject:
        current = value
        for key in keys[:-1]:
            current = self.json_object(current[key])
        return self.json_object(current[keys[-1]])

    def json_schema_root(self, schema: JsonObject) -> JsonObject:
        reference = self.json_string(schema["$ref"])
        return self.json_object(self.json_object(schema["$defs"])[reference.rsplit("/", 1)[-1]])

    def run_json_transition(
        self,
        common: tuple[str, ...],
        action: JsonObject,
        payload: Path,
    ) -> tuple[int, str, str]:
        arguments = [
            *common,
            "transition",
            "--action-id",
            str(action["action_id"]),
            "--expected-revision",
            str(action["expected_revision"]),
            "--generation",
            str(action["coordinator_generation"]),
            "--authorization",
            str(action["authorization"]),
        ]
        subject_revision = action.get("subject_revision")
        if subject_revision:
            arguments.extend(("--subject-revision", str(subject_revision)))
        lease_id = action.get("lease_id")
        if lease_id:
            arguments.extend(("--lease-id", str(lease_id)))
        resource_claims = action.get("resource_claims", [])
        if not isinstance(resource_claims, list):
            self.fail("resource_claims must be a JSON list")
        for claim in resource_claims:
            if not isinstance(claim, dict):
                self.fail("each resource claim must be a JSON object")
            arguments.extend(
                (
                    "--resource-claim",
                    str(claim["resource_id"]),
                    str(claim["host_id"]),
                    str(claim["lease_id"]),
                    str(claim["generation"]),
                )
            )
        arguments.extend(("--payload", str(payload)))
        return self.run_cli(*arguments)

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

    def test_exact_action_query_and_input_contract_avoid_broad_action_discovery(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        common = ("--project-root", str(project), "--work-root", str(work))
        before = self.snapshot(work)

        actions = self.json_list(
            self.run_json_cli(*common, "actions", "--role", "coordinator", "--action-id", "activate:reveal-core")[
                "actions"
            ]
        )
        action = self.json_object(actions[0])
        self.assertEqual("activate:reveal-core", action["action_id"])
        schema = self.json_object_at(action, "input_contract", "payload_schema")
        activate = self.json_schema_root(schema)
        self.assertEqual(["attempt", "branch", "base_revision", "owner"], activate["required"])
        self.assertFalse(activate["additionalProperties"])

        contract = self.run_json_cli(*common, "input-contract", "accept-proposal")
        self.assertEqual("accept-proposal", contract["action_kind"])
        contract_schema = self.json_object(contract["payload_schema"])
        accepted = self.json_schema_root(contract_schema)
        self.assertEqual(["item", "state", "next_action"], accepted["required"])
        self.assertEqual([], self.json_object_at(accepted, "properties", "depends_on")["default"])
        accepted_state = self.json_object_at(contract_schema, "$defs", "AcceptedProposalState")
        self.assertEqual(["blocked", "deferred", "intake", "ready"], accepted_state["enum"])
        empty = self.json_schema_root(
            self.json_object(self.run_json_cli(*common, "input-contract", "submit-review")["payload_schema"])
        )
        self.assertEqual([], empty["required"])
        self.assertEqual({}, empty["properties"])
        checkpoint = self.json_schema_root(
            self.json_object(self.run_json_cli(*common, "input-contract", "accept-checkpoint")["payload_schema"])
        )
        self.assertEqual(["checkpoint", "candidate", "evidence"], checkpoint["required"])
        self.assertFalse(checkpoint["additionalProperties"])

        missing_result, _, missing_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "coordinator",
            "--action-id",
            "activate:missing",
            "--json",
        )
        self.assertEqual(11, missing_result)
        self.assertIn("ACTION_NOT_AVAILABLE", missing_stderr)
        self.assertEqual(before, self.snapshot(work))

    def test_one_shot_coordination_is_recoverable_before_during_and_after_apply(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        migrate_to_v2(work, project)
        common = ("--project-root", str(project), "--work-root", str(work))
        payload = Path(tempfile.mkdtemp()) / "activate.json"
        payload.write_text(
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
        invalid_payload = payload.with_name("invalid.json")
        invalid_payload.write_text("{}", encoding="utf-8")
        before = self.snapshot(work)
        apply_arguments = (
            *common,
            "coordination",
            "apply",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "host",
            "--action-id",
            "activate:reveal-core",
        )

        invalid_result, _, invalid_stderr = self.run_cli(*apply_arguments, "--payload", str(invalid_payload))
        self.assertEqual(11, invalid_result)
        self.assertIn("TRANSITION_INPUT_INVALID", invalid_stderr)
        self.assertEqual(before, self.snapshot(work))

        with (
            patch("charlie_pinboard.interfaces.cli.apply_action", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_cli(*apply_arguments, "--payload", str(payload))
        self.assertEqual("released", self.run_json_cli(*common, "coordination", "status")["status"])
        self.assertEqual(WorkState.READY, parse_queue(work / "v2" / "queue.md").items[0].state)
        self.assertTrue(validate_work_state(work, project).valid)

        lease = self.run_json_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "interrupted-task",
            "--host-id",
            "host",
            "--ttl-seconds",
            "60",
        )
        retained_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "coordinator",
                "--lease-id",
                str(lease["lease_id"]),
                "--generation",
                str(lease["generation"]),
                "--action-id",
                "activate:reveal-core",
            )["actions"]
        )
        retained = self.json_object(retained_actions[0])
        self.run_json_cli(
            *common,
            "coordination",
            "release",
            "--lease-id",
            str(lease["lease_id"]),
            "--generation",
            str(lease["generation"]),
        )
        released = self.snapshot(work)

        stale_result, _, stale_stderr = self.run_json_transition(common, retained, payload)
        self.assertEqual(11, stale_result)
        self.assertRegex(stale_stderr, "LEASE_FENCED|ACTION_NOT_AVAILABLE")
        self.assertEqual(released, self.snapshot(work))

        receipt = self.run_json_cli(*apply_arguments, "--payload", str(payload))
        self.assertEqual("activate:reveal-core", receipt["action_id"])
        self.assertEqual(64, len(self.json_string(receipt["revision"])))
        self.assertEqual(WorkState.ACTIVE, parse_queue(work / "v2" / "queue.md").items[0].state)
        self.assertTrue(validate_work_state(work, project).valid)
        self.assertEqual("released", self.run_json_cli(*common, "coordination", "status")["status"])

    def test_one_shot_coordination_cleans_catchable_acquire_and_release_interrupts(self) -> None:
        for phase, interrupted_write, expected_state in (
            ("acquire", 1, WorkState.READY),
            ("release", 2, WorkState.ACTIVE),
        ):
            with self.subTest(phase=phase):
                project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
                migrate_to_v2(work, project)
                common = ("--project-root", str(project), "--work-root", str(work))
                payload = Path(tempfile.mkdtemp()) / "activate.json"
                payload.write_text(
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
                writes = 0

                def interrupt_after_write(path: Path, text: str, threshold: int = interrupted_write) -> None:
                    nonlocal writes
                    real_atomic_write_text(path, text)
                    writes += 1
                    if writes == threshold:
                        raise KeyboardInterrupt

                with (
                    patch("charlie_pinboard.legacy.leases.atomic_write_text", side_effect=interrupt_after_write),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    self.run_cli(
                        *common,
                        "coordination",
                        "apply",
                        "--task-id",
                        "coordinator-task",
                        "--host-id",
                        "host",
                        "--action-id",
                        "activate:reveal-core",
                        "--payload",
                        str(payload),
                    )

                self.assertEqual("released", self.run_json_cli(*common, "coordination", "status")["status"])
                self.assertEqual(expected_state, parse_queue(work / "v2" / "queue.md").items[0].state)
                self.assertTrue(validate_work_state(work, project).valid)

    def test_one_shot_receipt_keeps_the_transition_revision_across_a_release_race(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | ready | — | — | — | design | activate | Ready. |",
                "| later-work | ready | — | — | — | design | activate | Ready. |",
            ]
        )
        migrate_to_v2(work, project)
        common = ("--project-root", str(project), "--work-root", str(work))
        payload = Path(tempfile.mkdtemp()) / "activate.json"
        payload.write_text(
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
        observed: dict[str, str] = {}

        def release_then_change(work_root: Path, lease_id: str, generation: int) -> LeaseRecord:
            observed["transition_revision"] = state_revision(work_root)
            released = real_release_coordination(work_root, lease_id, generation)
            replacement = real_acquire_coordination(work_root, "other-task", "host", 60)
            action = next(
                candidate
                for candidate in actions_for(
                    work_root,
                    project,
                    "coordinator",
                    lease_id=replacement.lease_id,
                    generation=replacement.generation,
                )
                if candidate.action_id == "close:later-work"
            )
            apply_transition(
                work_root,
                project,
                action,
                json.dumps({"outcome": "done", "reason": "Concurrent decision completed."}),
            )
            real_release_coordination(work_root, replacement.lease_id, replacement.generation)
            return released

        with patch("charlie_pinboard.interfaces.cli.release_coordination", side_effect=release_then_change):
            receipt = self.run_json_cli(
                *common,
                "coordination",
                "apply",
                "--task-id",
                "coordinator-task",
                "--host-id",
                "host",
                "--action-id",
                "activate:reveal-core",
                "--payload",
                str(payload),
            )

        self.assertEqual(observed["transition_revision"], receipt["revision"])
        self.assertNotEqual(state_revision(work), receipt["revision"])
        self.assertTrue(validate_work_state(work, project).valid)

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

    def test_v1_worker_action_round_trips_through_review_and_completion(self) -> None:
        project, work = create_state(
            ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="reveal-core-1",
            create_active_attempt=True,
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        result_path = work / "attempts" / "reveal-core-1" / "result.md"
        result_path.write_text("Ready for review.\n", encoding="utf-8")
        payload = Path(tempfile.mkdtemp()) / "transition.json"
        payload.write_text("{}\n", encoding="utf-8")

        actions_result, actions_stdout, actions_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "worker",
            "--json",
        )
        self.assertEqual(0, actions_result, actions_stderr)
        submit = next(
            action
            for action in json.loads(actions_stdout)["actions"]
            if action["action_id"] == "submit-review:reveal-core-1"
        )

        before_rejection = self.snapshot(work)
        wrong_role = {**submit, "authorization": "coordinator"}
        rejected_result, _, rejected_stderr = self.run_json_transition(common, wrong_role, payload)

        self.assertEqual(11, rejected_result)
        self.assertIn("ACTION_NOT_AVAILABLE", rejected_stderr)
        self.assertEqual(before_rejection, self.snapshot(work))
        self.assertTrue(validate_work_state(work, project).valid)

        transition_result, _, transition_stderr = self.run_json_transition(common, submit, payload)

        self.assertEqual(0, transition_result, transition_stderr)
        self.assertEqual("attempt", submit["authorization"])
        self.assertEqual(WorkState.REVIEW, parse_queue(work / "queue.md").items[0].state)
        self.assertEqual(
            AttemptState.REVIEW,
            parse_attempt(work / "attempts" / "reveal-core-1" / "attempt.md").state,
        )
        self.assertIsNone(parse_current(work / "current.md").focus_item)
        self.assertEqual("Ready for review.\n", result_path.read_text(encoding="utf-8"))

        completion_payload = Path(tempfile.mkdtemp()) / "complete.json"
        completion_payload.write_text('{"evidence":"accepted review"}\n', encoding="utf-8")
        coordinator_result, coordinator_stdout, coordinator_stderr = self.run_cli(
            *common,
            "actions",
            "--role",
            "coordinator",
            "--json",
        )
        self.assertEqual(0, coordinator_result, coordinator_stderr)
        complete = next(
            action
            for action in json.loads(coordinator_stdout)["actions"]
            if action["action_id"] == "complete:reveal-core-1"
        )

        completion_result, _, completion_stderr = self.run_json_transition(common, complete, completion_payload)

        self.assertEqual(0, completion_result, completion_stderr)
        self.assertEqual((), parse_queue(work / "queue.md").items)
        self.assertTrue((work / "history" / "items" / "reveal-core.md").is_file())
        self.assertIsNone(parse_current(work / "current.md").focus_item)
        self.assertTrue(validate_work_state(work, project).valid)

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

    def test_overview_returns_one_revision_stamped_live_snapshot_without_history(self) -> None:
        project, work = create_state(
            [
                "| reveal-core | ready | cheaper-now | — | — | design | activate | Ready. |",
                "| mapping | blocked | must-now | reveal-core | — | finding | none | Waiting. |",
            ]
        )
        (work / "history" / "items" / "old-work.md").write_text(
            "---\nkind: work-history\nschema: wrong\nitem: old-work\nstate: done\n---\n",
            encoding="utf-8",
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        before = self.snapshot(work)

        result, stdout, stderr = self.run_cli(*common, "overview", "--json")

        self.assertEqual(0, result, stderr)
        value = json.loads(stdout)
        self.assertEqual("repo-work-overview/v1", value["schema"])
        self.assertEqual(64, len(value["revision"]))
        self.assertEqual(["mapping", "reveal-core"], sorted(item["item_id"] for item in value["items"]))
        self.assertNotIn("history", value)
        reveal = next(item for item in value["items"] if item["item_id"] == "reveal-core")
        self.assertEqual("Reveal Core", reveal["label"])
        self.assertEqual("activate", reveal["next_action"])
        self.assertEqual(["reveal-core"], value["immediate_options"])
        self.assertFalse(validate_work_state(work, project).valid)
        self.assertEqual(before, self.snapshot(work))

        (work / "history" / "items" / "old-work.md").unlink()
        migrate_to_v2(work, project)
        v2_before = self.snapshot(work)
        v2_result, v2_stdout, v2_stderr = self.run_cli(*common, "overview", "--json")
        self.assertEqual(0, v2_result, v2_stderr)
        self.assertEqual("v2", json.loads(v2_stdout)["authority"])
        self.assertEqual(value["items"], json.loads(v2_stdout)["items"])
        self.assertEqual(v2_before, self.snapshot(work))

    def test_overview_reads_an_exact_historical_prerequisite_but_not_all_history(self) -> None:
        project, work = create_state(["| dependent | blocked | — | completed-work | — | design | none | Waiting. |"])
        history = work / "history" / "items" / "completed-work.md"
        history.write_text(
            "---\nkind: work-history\nschema: repo-work/v1\nitem: completed-work\nstate: done\n---\n",
            encoding="utf-8",
        )
        common = ("--project-root", str(project), "--work-root", str(work))

        result, _, stderr = self.run_cli(*common, "overview", "--json")
        self.assertEqual(0, result, stderr)

        history.write_text(history.read_text(encoding="utf-8").replace("repo-work/v1", "wrong"), encoding="utf-8")
        invalid_result, _, invalid_stderr = self.run_cli(*common, "overview", "--json")
        self.assertEqual(11, invalid_result)
        self.assertIn("DOCUMENT_SCHEMA_INVALID", invalid_stderr)

    def test_close_records_a_terminal_decision_in_one_v1_command(self) -> None:
        project, work = create_state(
            ["| old-work | deferred | safe-to-defer | — | — | design | none | Revisit later. |"]
        )
        common = ("--project-root", str(project), "--work-root", str(work))

        result, stdout, stderr = self.run_cli(
            *common,
            "close",
            "old-work",
            "--outcome",
            "done",
            "--reason",
            "The user made the final decision.",
            "--json",
        )

        self.assertEqual(0, result, stderr)
        self.assertEqual("done", json.loads(stdout)["outcome"])
        self.assertFalse((work / "items" / "old-work.md").exists())
        history = work / "history" / "items" / "old-work.md"
        self.assertTrue(history.is_file())
        self.assertIn("The user made the final decision.", history.read_text(encoding="utf-8"))

    def test_close_borrows_and_releases_v2_coordination_in_one_command(self) -> None:
        project, work = create_state(
            ["| old-work | deferred | safe-to-defer | — | — | design | none | Revisit later. |"]
        )
        migrate_to_v2(work, project)
        common = ("--project-root", str(project), "--work-root", str(work))

        result, stdout, stderr = self.run_cli(
            *common,
            "close",
            "old-work",
            "--outcome",
            "dropped",
            "--reason",
            "No longer useful.",
            "--task-id",
            "chat-a",
            "--host-id",
            "mac--one",
            "--json",
        )

        self.assertEqual(0, result, stderr)
        self.assertEqual("dropped", json.loads(stdout)["outcome"])
        lease_result, lease_stdout, lease_stderr = self.run_cli(*common, "coordination", "status", "--json")
        self.assertEqual(0, lease_result, lease_stderr)
        self.assertEqual("released", json.loads(lease_stdout)["status"])

    def test_close_refuses_active_work_and_dropped_prerequisites(self) -> None:
        active_project, active_work = create_state(
            ["| active-work | active | — | — | active-work-1 | design | continue | Active. |"],
            focus_item="active-work",
            focus_attempt="active-work-1",
            create_active_attempt=True,
        )
        active_before = self.snapshot(active_work)

        active_result, _, active_stderr = self.run_cli(
            "--project-root",
            str(active_project),
            "--work-root",
            str(active_work),
            "close",
            "active-work",
            "--outcome",
            "done",
            "--reason",
            "Skip review.",
        )

        self.assertEqual(11, active_result)
        self.assertIn("ACTION_NOT_AVAILABLE", active_stderr)
        self.assertEqual(active_before, self.snapshot(active_work))

        project, work = create_state(
            [
                "| prerequisite | deferred | safe-to-defer | — | — | design | none | Old. |",
                "| dependent | blocked | — | prerequisite | — | design | none | Waiting. |",
            ]
        )
        before = self.snapshot(work)
        result, _, stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "close",
            "prerequisite",
            "--outcome",
            "dropped",
            "--reason",
            "Not doing it.",
        )

        self.assertEqual(11, result)
        self.assertIn("LIVE_DEPENDENTS", stderr)
        self.assertEqual(before, self.snapshot(work))

        migrate_to_v2(work, project)
        v2_result, _, v2_stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "close",
            "prerequisite",
            "--outcome",
            "dropped",
            "--reason",
            "Not doing it.",
            "--task-id",
            "chat-a",
            "--host-id",
            "studio",
        )
        self.assertEqual(11, v2_result)
        self.assertIn("LIVE_DEPENDENTS", v2_stderr)
        self.assertTrue((work / "v2" / "items" / "prerequisite.md").is_file())
        lease_result, lease_stdout, lease_stderr = self.run_cli(
            "--project-root",
            str(project),
            "--work-root",
            str(work),
            "coordination",
            "status",
            "--json",
        )
        self.assertEqual(0, lease_result, lease_stderr)
        self.assertEqual("released", json.loads(lease_stdout)["status"])

    def test_init_command_uses_the_current_installed_package_path(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        rejected_result, _, rejected_stderr = self.run_cli(
            *common, "init", "--coordinator-task-id", "coordinator", "--host-id", "local"
        )
        self.assertEqual(12, rejected_result)
        self.assertIn("MIGRATION_REQUIRED", rejected_stderr)
        self.assertFalse((work / "queue.md").exists())
        self.assertFalse((work / "state.sqlite3").exists())

        init_result, init_stdout, init_stderr = self.run_cli(*common, "init")
        self.assertEqual(0, init_result, init_stderr)
        self.assertIn("WORK_STATE_INITIALIZED", init_stdout)
        self.assertTrue((work / "state.sqlite3").is_file())
        self.assertFalse((work / "queue.md").exists())

    def test_v2_init_needs_no_master_task_and_coordination_is_borrowed(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        init_result, _, init_stderr = self.run_cli(*common, "init")
        available_result, available_stdout, available_stderr = self.run_cli(*common, "coordination", "status")
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
        self.assertEqual(0, available_result, available_stderr)
        self.assertIn("COORDINATION_AVAILABLE", available_stdout)
        self.assertEqual(0, status_result, status_stderr)
        self.assertEqual("sqlite-v1", json.loads(status_stdout)["authority"])
        self.assertIsNone(json.loads(status_stdout)["coordinator"])
        self.assertEqual(0, acquire_result, acquire_stderr)
        self.assertEqual("chat-a", json.loads(acquire_stdout)["task_id"])

        views = work / "views"
        for path in sorted(views.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        views.rmdir()
        for command in (
            ("validate", "--json"),
            ("overview", "--json"),
            ("actions", "--role", "observer", "--json"),
            ("parallel", "preview", "--host-id", "studio", "--json"),
        ):
            result, _, stderr = self.run_cli(*common, *command)
            self.assertEqual(0, result, stderr)
        rebuild_result, rebuild_stdout, rebuild_stderr = self.run_cli(*common, "views", "rebuild")
        self.assertEqual(0, rebuild_result, rebuild_stderr)
        self.assertIn("VIEWS_REBUILT", rebuild_stdout)

        (views / "queue.md").write_text("stale generated bytes", encoding="utf-8")
        validation = self.run_json_cli(*common, "validate")
        self.assertTrue(validation["valid"])
        self.assertTrue(
            any(
                self.json_object(value)["code"] == "VIEW_REFRESH_REQUIRED"
                for value in self.json_list(validation["diagnostics"])
            )
        )

        acquired = json.loads(acquire_stdout)
        renew_result, renew_stdout, renew_stderr = self.run_cli(
            *common,
            "coordination",
            "renew",
            "--lease-id",
            acquired["lease_id"],
            "--generation",
            str(acquired["generation"]),
            "--ttl-seconds",
            "120",
            "--json",
        )
        self.assertEqual(0, renew_result, renew_stderr)
        renewed = json.loads(renew_stdout)
        release_result, _, release_stderr = self.run_cli(
            *common,
            "coordination",
            "release",
            "--lease-id",
            renewed["lease_id"],
            "--generation",
            str(renewed["generation"]),
            "--json",
        )
        self.assertEqual(0, release_result, release_stderr)
        second = self.run_json_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "chat-b",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        revoke_result, revoke_stdout, revoke_stderr = self.run_cli(*common, "coordination", "revoke", "--json")
        self.assertEqual(0, revoke_result, revoke_stderr)
        self.assertGreater(json.loads(revoke_stdout)["generation"], second["generation"])

    def test_current_sqlite_cli_reads_complete_state_without_generated_views(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        assert state.authority.coordination is not None
        state = replace(
            state,
            authority=replace(
                state.authority,
                coordination=replace(state.authority.coordination, expires_at=now + timedelta(minutes=5)),
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        SQLiteWorkStore(roots.database_path).initialize_state(state)
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))

        status = self.run_json_cli(*common, "status")
        self.assertEqual("sqlite-v1", status["authority"])
        overview = self.run_json_cli(*common, "overview")
        self.assertEqual("12", overview["revision"])
        actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "coordinator",
                "--lease-id",
                "coordination-a",
                "--generation",
                "9",
            )["actions"]
        )
        self.assertTrue(actions)
        self.assertEqual(
            "active",
            self.run_json_cli(*common, "attempt", "status", "--attempt-id", "work-a-1")["status"],
        )
        resource = self.run_json_cli(*common, "resource", "status", "--resource-id", "workspace")
        self.assertEqual("workspace", resource["resource_id"])
        instance = self.run_json_cli(
            *common,
            "resource",
            "status",
            "--resource-id",
            "workspace",
            "--host-id",
            "host-a",
            "--detail",
            "detailed",
        )
        self.assertEqual("workspace-on-host", instance["instance_id"])
        self.assertEqual("detailed", instance["detail"])
        self.assertTrue(self.json_list(instance["task_uses"]))
        self.assertTrue(self.json_list(instance["mutation_intents"]))
        self.assertTrue(self.json_list(instance["history"]))
        preview = self.run_json_cli(*common, "parallel", "preview", "--host-id", "host-a")
        self.assertEqual("sqlite-v1", overview["authority"])
        self.assertEqual("repo-work-parallel-preview/v1", preview["schema"])
        human_commands = (
            ("status",),
            ("overview",),
            (
                "actions",
                "--role",
                "coordinator",
                "--lease-id",
                "coordination-a",
                "--generation",
                "9",
            ),
            ("input-contract", "activate"),
            ("attempt", "status", "--attempt-id", "work-a-1"),
            ("resource", "status", "--resource-id", "workspace"),
            (
                "resource",
                "status",
                "--resource-id",
                "workspace",
                "--host-id",
                "host-a",
                "--detail",
                "detailed",
            ),
            ("parallel", "preview", "--host-id", "host-a"),
        )
        for command in human_commands:
            with self.subTest(command=command):
                result, stdout, stderr = self.run_cli(*common, *command)
                self.assertEqual(0, result, stderr or stdout)
                self.assertTrue(stdout)

    def test_current_sqlite_cli_applies_transition_and_manages_attempt_authority(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        state = complete_sqlite_state()
        state = replace(
            state,
            authority=replace(
                state.authority,
                coordination=None,
                attempt_counters=(),
                attempt_generations=(),
                attempt_leases=(),
            ),
            resources=replace(state.resources, reservations=(), use_leases=(), mutation_intents=()),
        )
        SQLiteWorkStore(roots.database_path).initialize_state(state)
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        payload = project / "activate.json"
        payload.write_text(
            json.dumps(
                {
                    "attempt": "work-c-1",
                    "branch": "codex/work-c",
                    "base_revision": "candidate-base",
                    "owner": "worker-task",
                    "brief_artifact_ref_id": 1,
                }
            ),
            encoding="utf-8",
        )

        applied = self.run_json_cli(
            *common,
            "coordination",
            "apply",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
            "--action-id",
            "activate:work-c",
            "--payload",
            str(payload),
        )
        self.assertEqual("activate:work-c", applied["action_id"])
        self.assertEqual("released", self.run_json_cli(*common, "coordination", "status")["status"])

        acquired = self.run_json_cli(
            *common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-c-1",
            "--task-id",
            "worker-task",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        renewed = self.run_json_cli(
            *common,
            "attempt",
            "renew",
            "--attempt-id",
            "work-c-1",
            "--lease-id",
            str(acquired["lease_id"]),
            "--generation",
            str(acquired["generation"]),
            "--ttl-seconds",
            "120",
        )
        released = self.run_json_cli(
            *common,
            "attempt",
            "release",
            "--attempt-id",
            "work-c-1",
            "--lease-id",
            str(renewed["lease_id"]),
            "--generation",
            str(renewed["generation"]),
        )
        self.assertEqual("released", released["status"])
        status_result, status_stdout, status_stderr = self.run_cli(
            *common, "attempt", "status", "--attempt-id", "work-c-1"
        )
        self.assertEqual(0, status_result, status_stderr)
        self.assertIn("status=released", status_stdout)

        coordination = self.run_json_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        reacquired = self.run_json_cli(
            *common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-c-1",
            "--task-id",
            "worker-task-2",
            "--host-id",
            "studio",
            "--coordination-lease-id",
            str(coordination["lease_id"]),
            "--coordination-generation",
            str(coordination["generation"]),
            "--ttl-seconds",
            "60",
        )
        self.assertEqual("active", reacquired["status"])
        coordinator_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "coordinator",
                "--lease-id",
                str(coordination["lease_id"]),
                "--generation",
                str(coordination["generation"]),
            )["actions"]
        )
        pause = next(
            self.json_object(value)
            for value in coordinator_actions
            if self.json_object(value)["action_id"] == "pause:work-c-1"
        )
        pause_payload = project / "pause.json"
        pause_payload.write_text('{"reason":"Pause after the functional check."}\n', encoding="utf-8")
        transition_result, transition_stdout, transition_stderr = self.run_json_transition(common, pause, pause_payload)
        self.assertEqual(0, transition_result, transition_stderr)
        self.assertIn("TRANSITION_APPLIED", transition_stdout)

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
                self.assertIn("pinboard migrate --to v2", stderr)

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
        exact_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                str(attempt["lease_id"]),
                "--generation",
                str(attempt["generation"]),
                "--action-id",
                "submit-review:reveal-core-1",
            )["actions"]
        )
        submit = self.json_object(exact_actions[0])
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
        submit_input = self.json_schema_root(self.json_object_at(submit, "input_contract", "payload_schema"))
        self.assertEqual([], submit_input["required"])
        self.assertEqual({}, submit_input["properties"])
        payload = Path(tempfile.mkdtemp()) / "submit.json"
        payload.write_text("{}\n", encoding="utf-8")
        transition_result, _, transition_stderr = self.run_json_transition(common, submit, payload)
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

        registration_result, _, registration_stderr = self.run_cli(*common, "init")
        proposal_result, _, proposal_stderr = self.run_cli(*common, "proposal", "--file", str(invalid))
        with patch(
            "charlie_pinboard.interfaces.cli.resolve_project_root",
            side_effect=RootError("PROJECT_ROOT_NOT_FOUND", "missing"),
        ):
            root_result, _, root_stderr = self.run_cli("root")
        journal_path_for(work).mkdir()
        recovery_result, _, recovery_stderr = self.run_cli(*common, "recover")

        self.assertEqual(12, registration_result)
        self.assertIn("MIGRATION_REQUIRED", registration_stderr)
        self.assertEqual(2, proposal_result)
        self.assertIn("Expected `object`, got `array`", proposal_stderr)
        self.assertEqual(2, root_result)
        self.assertIn("PROJECT_ROOT_NOT_FOUND", root_stderr)
        self.assertEqual(11, recovery_result)
        self.assertIn("COMMIT_JOURNAL_INVALID", recovery_stderr)

    def test_sqlite_proposal_command_persists_once_without_legacy_authority(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        proposal_path = project / "proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "repo-work/v1",
                    "proposal_id": "cli-sqlite-proposal",
                    "created_at": (SQLITE_NOW + timedelta(seconds=1)).isoformat(),
                    "source_task_id": "discovering-task",
                    "user_label": "SQLite CLI proposal",
                    "trigger": "The public proposal command must use current authority.",
                    "evidence": ["source:cli"],
                    "why_it_matters": "A legacy writer cannot persist to SQLite authority.",
                    "relation": {"kind": "follow-up", "item": "work-c"},
                    "effect": "The proposal appears once in the SQLite inbox.",
                    "unlock": "Route the command through the existing application service.",
                    "urgency_evidence": "The installed command currently fails on SQLite authority.",
                    "freshness_assumptions": ["SQLite remains authoritative."],
                }
            ),
            encoding="utf-8",
        )
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        before_revision = store.snapshot().lifecycle.project.revision

        result, stdout, stderr = self.run_cli(*common, "proposal", "--file", str(proposal_path))
        duplicate_result, _, duplicate_stderr = self.run_cli(*common, "proposal", "--file", str(proposal_path))

        after = store.snapshot()
        self.assertEqual(0, result, stderr)
        created = next(value for value in after.proposals.proposals if str(value.proposal_id) == "cli-sqlite-proposal")
        self.assertIn("OK PROPOSAL_CREATED cli-sqlite-proposal", stdout)
        self.assertEqual(before_revision + 1, after.lifecycle.project.revision)
        self.assertEqual("SQLite CLI proposal", created.user_label)
        self.assertEqual("follow-up", created.relation.value)
        self.assertEqual("work-c", created.relation_item_id)
        self.assertEqual(
            ("source:cli",),
            tuple(value.selector for value in after.proposals.evidence if value.proposal_id == created.proposal_id),
        )
        self.assertEqual(13, duplicate_result)
        self.assertIn("PROPOSAL_ALREADY_EXISTS", duplicate_stderr)
        self.assertEqual(before_revision + 1, store.snapshot().lifecycle.project.revision)

    def test_current_sqlite_status_uses_one_snapshot_and_query_failures_are_stable(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        SQLiteWorkStore(roots.database_path).initialize_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(roots.work_root))
        original_snapshot = SQLiteWorkStore.snapshot
        calls = 0

        def counted(store: SQLiteWorkStore) -> StoredWorkState:
            nonlocal calls
            calls += 1
            return original_snapshot(store)

        with patch.object(SQLiteWorkStore, "snapshot", counted):
            status = self.run_json_cli(*common, "status")
        self.assertEqual("12", status["revision"])
        self.assertEqual(1, calls)

        result, _, stderr = self.run_cli(*common, "parallel", "preview", "--host-id", "host-a", "--item", "missing")
        self.assertEqual(11, result)
        self.assertIn("PARALLEL_SELECTION_INVALID", stderr)

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["pinboard", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("charlie_pinboard.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
