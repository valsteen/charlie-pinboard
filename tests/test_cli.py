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

from charlie_pinboard.adapters.files.artifacts import write_revision
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind, StoredWorkItemState, StoredWorkState
from charlie_pinboard.domain import work_models
from charlie_pinboard.domain.identifiers import AttemptId, ItemId
from charlie_pinboard.interfaces.cli import build_parser, main
from charlie_pinboard.interfaces.work_briefs import canonical_work_brief_bytes

from .support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state
from .work_brief_support import work_a_brief, work_c_brief


class CliTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def run_json_cli(self, *arguments: str) -> JsonObject:
        result, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertEqual(0, result, stderr)
        value = json.loads(stdout)
        if not isinstance(value, dict):
            self.fail("CLI JSON result must be an object")
        return value

    def run_cli_parse_error(self, *arguments: str) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(arguments)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()

    def json_list(self, value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            self.fail("JSON value must be a list")
        return value

    def json_object(self, value: JsonValue) -> JsonObject:
        if not isinstance(value, dict):
            self.fail("JSON value must be an object")
        return value

    def json_int(self, value: JsonValue) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            self.fail("JSON value must be an integer")
        return value

    def run_transition(self, common: tuple[str, ...], action: JsonObject, payload: Path) -> tuple[int, str, str]:
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
        arguments.extend(("--payload", str(payload)))
        return self.run_cli(*arguments)

    def initialized_state(self, state: StoredWorkState | None = None) -> tuple[Path, Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        if state is not None:
            reference = state.artifact_references[0]
            if reference.selector.endswith(".opaque"):
                value = work_a_brief(project)
                published = write_revision(
                    roots,
                    NewArtifact(ArtifactKind.BRIEF, value.attempt_id, 1, ".json", canonical_work_brief_bytes(value)),
                )
                reference = replace(
                    reference,
                    key=published.key,
                    revision=published.revision,
                    selector=published.selector,
                    content_sha256=published.content_sha256,
                    size_bytes=published.size_bytes,
                )
                state = replace(state, artifact_references=(reference, *state.artifact_references[1:]))
            store.initialize_state(state)
        return project, roots.work_root, store

    def test_current_command_surface_lists_every_command(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        for retained in (
            "root",
            "validate",
            "status",
            "overview",
            "item",
            "close",
            "actions",
            "input-contract",
            "brief",
            "brief-sources",
            "init",
            "proposal",
            "transition",
            "dispatch",
            "coordination",
            "attempt",
            "parallel",
            "views",
        ):
            self.assertIn(retained, help_text)
        item_status_help = io.StringIO()
        with contextlib.redirect_stdout(item_status_help), self.assertRaises(SystemExit) as raised:
            parser.parse_args(("item", "status", "--help"))
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--item-id", item_status_help.getvalue())

    def test_init_and_current_read_commands_need_no_filesystem_authority(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        common = ("--project-root", str(project), "--work-root", str(work))

        result, stdout, stderr = self.run_cli(*common, "init")

        self.assertEqual(0, result, stderr)
        self.assertIn("WORK_STATE_INITIALIZED", stdout)
        self.assertTrue((work / "state.sqlite3").is_file())
        self.assertFalse((work / "authority.json").exists())
        self.assertFalse((work / "queue.md").exists())
        self.assertTrue(self.run_json_cli(*common, "validate")["valid"])
        self.assertEqual("sqlite-v1", self.run_json_cli(*common, "status")["authority"])
        overview = self.run_json_cli(*common, "overview")
        self.assertEqual("sqlite-v1", overview["authority"])
        self.assertEqual("pinboard-overview/v2", overview["schema"])
        actions = self.run_json_cli(*common, "actions", "--role", "observer")["actions"]
        self.assertIsInstance(actions, list)
        assert isinstance(actions, list)
        action_ids: list[str] = []
        for action in actions:
            self.assertIsInstance(action, dict)
            assert isinstance(action, dict)
            action_id = action.get("action_id")
            self.assertIsInstance(action_id, str)
            assert isinstance(action_id, str)
            action_ids.append(action_id)
        self.assertEqual(["inspect:ledger"], action_ids)
        self.assertEqual(
            "pinboard-parallel-preview/v1",
            self.run_json_cli(*common, "parallel", "preview")["schema"],
        )

        coordination = self.run_json_cli(*common, "coordination", "status")
        self.assertIsNone(coordination["lease"])
        result, stdout, stderr = self.run_cli(*common, "coordination", "status")
        self.assertEqual(0, result, stderr)
        self.assertIn("COORDINATION_AVAILABLE", stdout)
        result, stdout, stderr = self.run_cli(*common, "overview")
        self.assertEqual(0, result, stderr)
        self.assertIn("live_work=none", stdout)
        exact_result, _exact_stdout, exact_stderr = self.run_cli(
            *common, "actions", "--role", "observer", "--action-id", "inspect:missing"
        )
        self.assertEqual(11, exact_result)
        self.assertIn("ACTION_NOT_AVAILABLE", exact_stderr)
        renew_result, _renew_stdout, renew_stderr = self.run_cli(
            *common,
            "coordination",
            "renew",
            "--lease-id",
            "missing",
            "--generation",
            "1",
            "--ttl-seconds",
            "60",
        )
        self.assertEqual(11, renew_result)
        self.assertIn("COORDINATION_LEASE_REQUIRED", renew_stderr)

    def test_coordination_and_attempt_lifecycle_use_sqlite(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        state = store.snapshot()
        state = replace(
            state,
            authority=replace(
                state.authority,
                coordination=None,
                attempt_counters=(),
                attempt_generations=(),
                attempt_leases=(),
            ),
        )
        work.unlink(missing_ok=True) if work.is_file() else None
        database = work / "state.sqlite3"
        database.unlink()
        initialize_database(resolve_durable_roots(project), SQLITE_NOW)
        store = SQLiteWorkStore(database)
        store.initialize_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        candidate = work_c_brief()
        brief_path = project / "work-c-brief.json"
        brief_path.write_bytes(canonical_work_brief_bytes(candidate))
        publication = self.run_json_cli(*common, "brief", "publish", "--file", str(brief_path))
        payload = project / "activate.json"
        payload.write_text(
            json.dumps(
                {
                    "attempt": "work-c-1",
                    "branch": "codex/work-c",
                    "base_revision": "candidate-base",
                    "owner": "worker-task",
                    "brief_artifact_ref_id": publication["artifact_ref_id"],
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
        coordination = self.run_json_cli(
            *common,
            "coordination",
            "renew",
            "--lease-id",
            str(coordination["lease_id"]),
            "--generation",
            str(coordination["generation"]),
            "--ttl-seconds",
            "120",
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
        revoked = self.run_json_cli(
            *common,
            "attempt",
            "revoke",
            "--attempt-id",
            "work-c-1",
            "--lease-id",
            str(reacquired["lease_id"]),
            "--generation",
            str(reacquired["generation"]),
            "--coordination-lease-id",
            str(coordination["lease_id"]),
            "--coordination-generation",
            str(coordination["generation"]),
        )
        self.assertEqual("revoked", revoked["status"])
        released_coordination = self.run_json_cli(
            *common,
            "coordination",
            "release",
            "--lease-id",
            str(coordination["lease_id"]),
            "--generation",
            str(coordination["generation"]),
        )
        self.assertEqual("released", released_coordination["status"])
        replacement = self.run_json_cli(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "replacement-task",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        revoked_coordination = self.run_json_cli(*common, "coordination", "revoke")
        self.assertGreater(
            self.json_int(revoked_coordination["generation"]),
            self.json_int(replacement["generation"]),
        )

    def test_current_read_surface_has_human_and_json_views(self) -> None:
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
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

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
            "active", self.run_json_cli(*common, "attempt", "status", "--attempt-id", "work-a-1")["status"]
        )
        parallel = self.run_json_cli(*common, "parallel", "preview")
        self.assertEqual(
            {"excluded", "launchable", "revision", "safe", "schema", "selection"},
            set(parallel),
        )
        self.assertEqual(
            ("pinboard-parallel-preview/v1", "12", "all-safe", True),
            (parallel["schema"], parallel["revision"], parallel["selection"], parallel["safe"]),
        )
        items = [
            self.json_object(item)
            for item in (*self.json_list(parallel["launchable"]), *self.json_list(parallel["excluded"]))
        ]
        self.assertTrue(
            all(set(item) == {"attempt_id", "item_id", "label", "outcome", "reasons", "state"} for item in items)
        )
        self.assertEqual(
            [
                ("work-c", "Work work-c", "ready", None, "launchable", []),
                (
                    "intake-work",
                    "Work intake-work",
                    "intake",
                    None,
                    "excluded",
                    [
                        {
                            "code": "state-not-launchable",
                            "message": "Item 'intake-work' is intake; only ready items and unowned active attempts can launch.",
                        }
                    ],
                ),
                (
                    "work-a",
                    "Work work-a",
                    "active",
                    "work-a-1",
                    "excluded",
                    [
                        {
                            "code": "dependency-live",
                            "message": "Item 'work-a' still depends on live work: work-c.",
                        }
                    ],
                ),
                (
                    "zz-proposal-a",
                    "Work zz-proposal-a",
                    "intake",
                    None,
                    "excluded",
                    [
                        {
                            "code": "state-not-launchable",
                            "message": "Item 'zz-proposal-a' is intake; only ready items and unowned active attempts can launch.",
                        }
                    ],
                ),
            ],
            [
                (
                    item["item_id"],
                    item["label"],
                    item["state"],
                    item["attempt_id"],
                    item["outcome"],
                    item["reasons"],
                )
                for item in items
            ],
        )
        self.assertIn("payload_schema", self.run_json_cli(*common, "input-contract", "activate"))

        human_commands = (
            ("root",),
            ("status",),
            ("overview",),
            ("actions", "--role", "observer"),
            ("input-contract", "activate"),
            ("attempt", "status", "--attempt-id", "work-a-1"),
            ("parallel", "preview"),
            ("views", "rebuild"),
        )
        for command in human_commands:
            with self.subTest(command=command):
                result, stdout, stderr = self.run_cli(*common, *command)
                self.assertEqual(0, result, stderr)
                self.assertTrue(stdout)
        result, stdout, stderr = self.run_cli(*common, "parallel", "preview")
        self.assertEqual(0, result, stderr)
        self.assertEqual(
            """OK PARALLEL_PREVIEW revision=12 selection=all-safe safe=yes
Ready to launch together:
- work-c (ready)
Not launchable:
- intake-work (intake) — Item 'intake-work' is intake; only ready items and unowned active attempts can launch.
- work-a (active, attempt work-a-1) — Item 'work-a' still depends on live work: work-c.
- zz-proposal-a (intake) — Item 'zz-proposal-a' is intake; only ready items and unowned active attempts can launch.
""",
            stdout,
        )

    def test_blocker_actions_and_input_contracts_are_unambiguous(self) -> None:
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
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        coordinator_actions = self.json_list(
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
        worker_actions = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                "3",
            )["actions"]
        )
        expected: dict[str, tuple[str, str, JsonObject]] = {
            "report-blocker": (
                "report-blocker:work-a-1",
                "Prepare blocker report for work-a",
                {
                    "effect": "advisory",
                    "required_role": "worker",
                    "subject_kind": "attempt",
                    "lifecycle_precondition": "active-attempt",
                },
            ),
            "block": (
                "block:work-a-1",
                "Block active attempt for work-a",
                {
                    "effect": "mutating",
                    "required_role": "coordinator",
                    "subject_kind": "attempt",
                    "lifecycle_precondition": "active-attempt",
                },
            ),
            "block-item": (
                "block-item:intake-work",
                "Block unstarted work item intake-work",
                {
                    "effect": "mutating",
                    "required_role": "coordinator",
                    "subject_kind": "item",
                    "lifecycle_precondition": "intake-item",
                },
            ),
        }
        all_actions = tuple(self.json_object(action) for action in (*coordinator_actions, *worker_actions))
        selected = {
            kind: next(action for action in all_actions if action["action_id"] == action_id)
            for kind, (action_id, _label, _semantics) in expected.items()
        }
        for kind, (action_id, label, semantics) in expected.items():
            with self.subTest(kind=kind):
                action = selected[kind]
                self.assertEqual(action_id, action["action_id"])
                self.assertEqual(label, action["label"])
                self.assertEqual(semantics, self.json_object(action["semantics"]))
                contract = self.run_json_cli(*common, "input-contract", kind)
                self.assertEqual(kind, contract["action_kind"])
                self.assertEqual(semantics, self.json_object(contract["semantics"]))
                if kind == "report-blocker":
                    self.assertIsNone(contract["payload_schema"])
                else:
                    self.assertIsInstance(contract["payload_schema"], dict)

    def test_active_attempt_blocker_flow_persists_dependencies_and_resumes_through_commands(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        lifecycle = replace(
            state.lifecycle,
            dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")),
        )
        state = replace(
            state,
            lifecycle=lifecycle,
            authority=replace(
                state.authority,
                coordination=None,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        report = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "worker",
                    "--lease-id",
                    "attempt-lease-a",
                    "--generation",
                    "3",
                    "--action-id",
                    "report-blocker:work-a-1",
                )["actions"]
            )[0]
        )
        self.assertEqual("advisory", self.json_object(report["semantics"])["effect"])
        released = self.run_json_cli(
            *common,
            "attempt",
            "release",
            "--attempt-id",
            "work-a-1",
            "--lease-id",
            "attempt-lease-a",
            "--generation",
            "3",
        )
        self.assertEqual("released", released["status"])

        block = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "coordinator",
                    "--action-id",
                    "block:work-a-1",
                )["actions"]
            )[0]
        )
        self.assertEqual("active-attempt", self.json_object(block["semantics"])["lifecycle_precondition"])
        block_payload = project / "block.json"
        block_payload.write_text(
            '{"reason":"Waiting for the intake prerequisite.","depends_on":["intake-work"]}\n',
            encoding="utf-8",
        )
        self.run_json_cli(
            *common,
            "coordination",
            "apply",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
            "--action-id",
            "block:work-a-1",
            "--payload",
            str(block_payload),
        )

        blocked = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        blocked_item = next(value for value in blocked.lifecycle.work_items if value.item_id == ItemId("work-a"))
        blocked_attempt = next(
            value for value in blocked.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        self.assertEqual(StoredWorkItemState.BLOCKED, blocked_item.state)
        self.assertEqual(work_models.AttemptState.BLOCKED, blocked_attempt.state)
        self.assertEqual(
            ("intake-work",),
            tuple(
                str(value.dependency_id)
                for value in blocked.lifecycle.dependencies
                if value.item_id == ItemId("work-a")
            ),
        )

        self.run_json_cli(
            *common,
            "close",
            "intake-work",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite is satisfied.",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
        )
        resume = self.json_object(
            self.json_list(
                self.run_json_cli(
                    *common,
                    "actions",
                    "--role",
                    "coordinator",
                    "--action-id",
                    "resume:work-a",
                )["actions"]
            )[0]
        )
        self.assertEqual("resume:work-a", resume["action_id"])
        resume_payload = project / "resume.json"
        resume_payload.write_text("{}\n", encoding="utf-8")
        self.run_json_cli(
            *common,
            "coordination",
            "apply",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
            "--action-id",
            "resume:work-a",
            "--payload",
            str(resume_payload),
        )
        resumed = SQLiteWorkStore(work / "state.sqlite3").snapshot()
        resumed_item = next(value for value in resumed.lifecycle.work_items if value.item_id == ItemId("work-a"))
        resumed_attempt = next(
            value for value in resumed.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
        )
        self.assertEqual(StoredWorkItemState.ACTIVE, resumed_item.state)
        self.assertEqual(work_models.AttemptState.ACTIVE, resumed_attempt.state)

    def test_blocker_skill_guidance_names_advisory_and_mutating_responsibilities(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        coordinator = (repository / "skills" / "pinboard" / "SKILL.md").read_text(encoding="utf-8")
        worker = (repository / "skills" / "pinboard-deliver" / "SKILL.md").read_text(encoding="utf-8")

        for text in (coordinator, worker):
            self.assertIn("report-blocker:<attempt>", text)
            self.assertIn("block:<attempt>", text)
            self.assertIn("block-item:<item>", text)
            self.assertIn("pause:<attempt>", text)
        self.assertIn("advisory and has no mutation payload", worker)
        self.assertIn("never use the intake-only `block-item:<item>` action for active work", coordinator)

    def test_invalid_current_inputs_map_to_stable_cli_failures(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        invalid = project / "invalid.json"
        invalid.write_text("[]", encoding="utf-8")
        cases = (
            (("proposal", "--file", str(invalid)), "PROPOSAL_INVALID"),
            (("attempt", "status", "--attempt-id", "missing"), "ATTEMPT_LEASE_REQUIRED"),
            (
                ("coordination", "renew", "--lease-id", "wrong", "--generation", "9", "--ttl-seconds", "60"),
                "LEASE_FENCED",
            ),
        )
        for arguments, code in cases:
            with self.subTest(arguments=arguments):
                result, _stdout, stderr = self.run_cli(*common, *arguments)
                self.assertNotEqual(0, result)
                self.assertIn(code, stderr)

        identifier_stderr = self.run_cli_parse_error(
            *common,
            "coordination",
            "acquire",
            "--task-id",
            "..",
            "--host-id",
            "studio",
            "--ttl-seconds",
            "60",
        )
        self.assertIn("pinboard coordination acquire", identifier_stderr)
        self.assertIn("$.task_id", identifier_stderr)

    def test_direct_transition_reselects_exact_worker_capability(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        exact = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "worker",
                "--lease-id",
                "attempt-lease-a",
                "--generation",
                "3",
                "--action-id",
                "submit-review:work-a-1",
            )["actions"]
        )
        action = self.json_object(exact[0])
        payload = project / "submit-review.json"
        payload.write_text('{"candidate":"candidate-cli-direct"}\n', encoding="utf-8")

        result, stdout, stderr = self.run_transition(common, action, payload)
        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED submit-review:work-a-1", stdout)

        invalid_cases = (
            (("--action-id", "invalid"), "ACTION_ID_INVALID"),
            (
                (
                    "--action-id",
                    "invented:work-a",
                ),
                "ACTION_ID_INVALID",
            ),
        )
        for replacement, code in invalid_cases:
            arguments = [
                *common,
                "transition",
                "--action-id",
                "pause:work-a-1",
                "--expected-revision",
                "stale",
                "--generation",
                "3",
                "--authorization",
                "attempt",
                "--lease-id",
                "attempt-lease-a",
                "--payload",
                str(payload),
            ]
            option = replacement[0]
            if option == "--action-id":
                index = arguments.index(option)
                arguments[index : index + 2] = replacement
            else:
                arguments.extend(replacement)
            with self.subTest(code=code):
                invalid_result, _invalid_stdout, invalid_stderr = self.run_cli(*arguments)
                self.assertEqual(11, invalid_result)
                self.assertIn(code, invalid_stderr)

    def test_review_acceptance_and_continuation_uses_the_exact_coordination_capability(self) -> None:
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        coordination = state.authority.coordination
        assert coordination is not None
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=StoredWorkItemState.REVIEW) if value.item_id == ItemId("work-a") else value
                    for value in state.lifecycle.work_items
                ),
                attempts=tuple(
                    replace(
                        value,
                        state=work_models.AttemptState.REVIEW,
                        candidate_revision="candidate-cli-review",
                        candidate_recorded_at=now,
                    )
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in state.lifecycle.attempts
                ),
            ),
            authority=replace(
                state.authority,
                coordination=replace(
                    coordination,
                    acquired_at=now,
                    expires_at=now + timedelta(minutes=5),
                ),
                attempt_leases=tuple(
                    replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
                ),
            ),
        )
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        exact = self.json_list(
            self.run_json_cli(
                *common,
                "actions",
                "--role",
                "coordinator",
                "--lease-id",
                "coordination-a",
                "--generation",
                "9",
                "--action-id",
                "accept-review-and-continue:work-a-1",
            )["actions"]
        )
        action = self.json_object(exact[0])
        payload = project / "accept-review-and-continue.json"
        payload.write_text(
            '{"candidate":"candidate-cli-review","evidence":"accepted through the CLI"}\n',
            encoding="utf-8",
        )

        result, stdout, stderr = self.run_transition(common, action, payload)

        self.assertEqual(0, result, stderr)
        self.assertIn("OK TRANSITION_APPLIED accept-review-and-continue:work-a-1", stdout)
        reloaded = store.snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.assertEqual(work_models.AttemptState.ACTIVE, attempt.state)
        self.assertIsNone(attempt.candidate_revision)

    def test_close_borrows_sqlite_coordination_and_records_terminal_history(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, authority=replace(state.authority, coordination=None))
        project, work, store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))

        missing_stderr = self.run_cli_parse_error(
            *common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite outcome is already complete.",
        )
        self.assertIn("pinboard close", missing_stderr)
        self.assertIn("--task-id, --host-id", missing_stderr)

        closed = self.run_json_cli(
            *common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite outcome is already complete.",
            "--task-id",
            "coordinator-task",
            "--host-id",
            "studio",
        )
        self.assertEqual("work-c", closed["item_id"])
        self.assertEqual("done", closed["outcome"])
        coordination = store.snapshot().authority.coordination
        self.assertIsNotNone(coordination)
        assert coordination is not None
        self.assertEqual("released", coordination.state.value)

    def test_proposal_persists_once_through_native_intake(self) -> None:
        project, work, store = self.initialized_state(complete_sqlite_state())
        proposal_path = project / "proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "schema": "pinboard-proposal/v1",
                    "proposal_id": "cli-sqlite-proposal",
                    "created_at": (SQLITE_NOW + timedelta(seconds=1)).isoformat(),
                    "source_task_id": "discovering-task",
                    "user_label": "SQLite CLI proposal",
                    "trigger": "The public proposal command must use current authority.",
                    "evidence": ["source:cli"],
                    "why_it_matters": "A filesystem writer cannot persist to SQLite authority.",
                    "relation": {"kind": "follow-up", "item": "work-c"},
                    "effect": "The proposal appears once as visible intake.",
                    "unlock": "Use the application proposal service.",
                    "urgency_evidence": "The installed command must remain current.",
                    "freshness_assumptions": ["SQLite remains authoritative."],
                }
            ),
            encoding="utf-8",
        )
        common = ("--project-root", str(project), "--work-root", str(work))
        before_revision = store.snapshot().lifecycle.project.revision

        result, stdout, stderr = self.run_cli(*common, "proposal", "--file", str(proposal_path))
        duplicate_result, _, duplicate_stderr = self.run_cli(*common, "proposal", "--file", str(proposal_path))

        self.assertEqual(0, result, stderr)
        self.assertIn("OK PROPOSAL_CREATED cli-sqlite-proposal", stdout)
        after = store.snapshot()
        self.assertEqual(before_revision + 1, after.lifecycle.project.revision)
        visible = next(value for value in after.lifecycle.work_items if str(value.item_id) == "cli-sqlite-proposal")
        self.assertEqual((StoredWorkItemState.INTAKE, 5), (visible.state, visible.queue_position))
        self.assertEqual(("work-a", "work-a-1"), (str(after.focus.item_id), str(after.focus.attempt_id)))
        self.assertEqual(
            ("work-c",),
            tuple(
                str(value.dependency_id) for value in after.lifecycle.dependencies if value.item_id == visible.item_id
            ),
        )
        self.assertEqual(13, duplicate_result)
        self.assertIn("PROPOSAL_ALREADY_EXISTS", duplicate_stderr)

    def test_proposal_timestamp_and_file_failures_are_stable(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
        path = project / "proposal.json"
        value: JsonObject = {
            "schema": "pinboard-proposal/v1",
            "proposal_id": "date-only-proposal",
            "created_at": "2026-08-25",
            "source_task_id": "task",
            "user_label": "Date-only proposal",
            "trigger": "A timestamp boundary needs coverage.",
            "evidence": ["source:test"],
            "why_it_matters": "The boundary remains deterministic.",
            "relation": {"kind": "independent", "item": None},
            "effect": "The proposal persists.",
            "unlock": "Timestamp normalization is explicit.",
            "urgency_evidence": "This is current intake behavior.",
            "freshness_assumptions": ["SQLite remains authoritative."],
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        result, _stdout, stderr = self.run_cli(*common, "proposal", "--file", str(path))
        self.assertEqual(0, result, stderr)

        for proposal_id, created_at in (
            ("naive-proposal", "2026-08-25T12:00:00"),
            ("invalid-date-proposal", "not-a-date"),
        ):
            value["proposal_id"] = proposal_id
            value["created_at"] = created_at
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(created_at=created_at):
                invalid, _invalid_stdout, invalid_stderr = self.run_cli(*common, "proposal", "--file", str(path))
                self.assertEqual(2, invalid)
                self.assertIn("PROPOSAL_INVALID", invalid_stderr)

        missing, _missing_stdout, missing_stderr = self.run_cli(
            *common, "proposal", "--file", str(project / "missing.json")
        )
        self.assertEqual(2, missing)
        self.assertIn("PROPOSAL_INVALID", missing_stderr)

    def test_status_uses_one_snapshot_and_query_failures_are_stable(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))
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

        result, _, stderr = self.run_cli(*common, "parallel", "preview", "--item", "missing")
        self.assertEqual(11, result)
        self.assertIn("PARALLEL_SELECTION_INVALID", stderr)

    def test_item_status_emits_exact_json_and_text_from_one_snapshot(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        done_item = replace(
            state.lifecycle.work_items[2],
            state=StoredWorkItemState.DONE,
            timing=work_models.Timing.SAFE_TO_DEFER,
            outcome_evidence="accepted completion",
            next_action=None,
            notes=None,
        )
        done_attempt = replace(
            active,
            attempt_id=AttemptId("work-b-1"),
            item_id=done_item.item_id,
            state=work_models.AttemptState.DONE,
            candidate_revision="candidate-b",
            candidate_recorded_at=SQLITE_NOW,
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(*state.lifecycle.work_items[:2], done_item, *state.lifecycle.work_items[3:]),
                attempts=(*state.lifecycle.attempts, done_attempt),
            ),
        )
        project, work, _store = self.initialized_state(state)
        common = ("--project-root", str(project), "--work-root", str(work))
        original_snapshot = SQLiteWorkStore.snapshot
        calls = 0

        def counted(store: SQLiteWorkStore) -> StoredWorkState:
            nonlocal calls
            calls += 1
            return original_snapshot(store)

        with patch.object(SQLiteWorkStore, "snapshot", counted):
            status = self.run_json_cli(*common, "item", "status", "--item-id", "work-b")

        self.assertEqual(1, calls)
        self.assertEqual(
            {
                "schema": "pinboard-item-status/v1",
                "authority": "sqlite-v1",
                "revision": "12",
                "item_id": "work-b",
                "label": "Work work-b",
                "state": "done",
                "timing": "safe-to-defer",
                "outcome_evidence": "accepted completion",
                "next_action": None,
                "notes": "",
                "attempts": [{"attempt_id": "work-b-1", "state": "done", "candidate_revision": "candidate-b"}],
            },
            status,
        )
        self.assertEqual(
            {
                "schema": "pinboard-item-status/v1",
                "authority": "sqlite-v1",
                "revision": "12",
                "item_id": "work-a",
                "label": "Work work-a",
                "state": "active",
                "timing": "must-now",
                "outcome_evidence": None,
                "next_action": "continue",
                "notes": "Current work remains bounded.",
                "attempts": [{"attempt_id": "work-a-1", "state": "active", "candidate_revision": None}],
            },
            self.run_json_cli(*common, "item", "status", "--item-id", "work-a"),
        )
        result, stdout, stderr = self.run_cli(*common, "item", "status", "--item-id", "work-b")
        self.assertEqual(0, result, stderr)
        self.assertIn("OK ITEM_STATUS item=work-b state=done revision=12 authority=sqlite-v1", stdout)
        self.assertIn("outcome_evidence=accepted completion", stdout)
        self.assertIn("attempt=work-b-1 state=done candidate=candidate-b", stdout)

    def test_item_status_rejects_missing_and_malformed_identities(self) -> None:
        project, work, _store = self.initialized_state(complete_sqlite_state())
        common = ("--project-root", str(project), "--work-root", str(work))

        missing, _missing_stdout, missing_stderr = self.run_cli(*common, "item", "status", "--item-id", "missing-item")
        malformed_stderr = self.run_cli_parse_error(*common, "item", "status", "--item-id", "bad/item")

        self.assertEqual(11, missing)
        self.assertIn("ITEM_NOT_FOUND", missing_stderr)
        self.assertIn("pinboard item status", malformed_stderr)
        self.assertIn("$.item_id", malformed_stderr)

    def test_relational_cli_inputs_are_rejected_at_the_selected_leaf(self) -> None:
        cases = (
            (("actions", "--role", "worker", "--lease-id", "lease-a"), "pinboard actions"),
            (
                (
                    "attempt",
                    "acquire",
                    "--attempt-id",
                    "attempt-a",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--ttl-seconds",
                    "60",
                    "--coordination-lease-id",
                    "coordination-a",
                ),
                "pinboard attempt acquire",
            ),
            (
                (
                    "transition",
                    "--action-id",
                    "pause:attempt-a",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--authorization",
                    "attempt",
                    "--payload",
                    "payload.json",
                ),
                "pinboard transition",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "dispatch:attempt-a",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                    "--review-id",
                    "review-a",
                ),
                "pinboard dispatch",
            ),
        )
        for arguments, route in cases:
            with self.subTest(arguments=arguments):
                stderr = self.run_cli_parse_error(*arguments)
                self.assertIn(route, stderr)

    def test_custom_command_decoders_preserve_identifier_constraints(self) -> None:
        cases = (
            (("actions", "--role", "observer", "--action-id", "bad/id"), "$.action_id"),
            (
                (
                    "attempt",
                    "acquire",
                    "--attempt-id",
                    "bad/id",
                    "--task-id",
                    "task-a",
                    "--host-id",
                    "host-a",
                    "--ttl-seconds",
                    "60",
                ),
                "$.attempt_id",
            ),
            (
                (
                    "transition",
                    "--action-id",
                    "bad/id",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--payload",
                    "payload.json",
                ),
                "$.action_id",
            ),
            (
                (
                    "dispatch",
                    "--action-id",
                    "bad/id",
                    "--expected-revision",
                    "1",
                    "--generation",
                    "1",
                    "--checkpoint",
                    "checkpoint-a",
                    "--environment",
                    "environment.json",
                ),
                "$.action_id",
            ),
        )
        for arguments, field_path in cases:
            with self.subTest(arguments=arguments):
                self.assertIn(field_path, self.run_cli_parse_error(*arguments))

    def test_module_entrypoint_delegates_to_cli(self) -> None:
        with patch.object(sys, "argv", ["pinboard", "--version"]), self.assertRaises(SystemExit) as raised:
            runpy.run_module("charlie_pinboard.__main__", run_name="__main__")

        self.assertEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
