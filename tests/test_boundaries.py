import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import msgspec

from repo_work.atomic import PlatformNotSupportedError, transition_lock
from repo_work.coordinator import CoordinatorError, parse_coordinator, read_coordinator
from repo_work.markdown import (
    ParseError,
    parse_attempt,
    parse_current,
    parse_header,
    parse_item,
    parse_queue_text,
    render_current,
    render_queue,
    render_v2_item,
    replace_header_fields,
)
from repo_work.model import SCHEMA_V1, SCHEMA_V2, Queue, QueueItem, WorkState
from repo_work.proposals import ProposalError, parse_proposal, read_proposal
from repo_work.transition_input import ActivateInput, TransitionInputError, parse_transition_input

from .support import JsonObject
from .test_transition import proposal


class JsonBoundaryTest(unittest.TestCase):
    def test_json_boundaries_decode_exact_models_without_raw_mapping_contracts(self) -> None:
        coordinator = {
            "schema": "repo-work/v1",
            "project_root": "/project",
            "task_id": "task",
            "host_id": "local",
            "generation": 1,
            "registered_at": "2026-08-16T12:00:00Z",
        }
        self.assertEqual("task", parse_coordinator(json.dumps(coordinator)).task_id)
        with self.assertRaises(CoordinatorError):
            parse_coordinator(json.dumps({**coordinator, "unexpected": True}))

        proposal_value = proposal()
        self.assertEqual("finding-1", parse_proposal(json.dumps(proposal_value)).proposal_id)
        with self.assertRaises(ProposalError):
            parse_proposal(json.dumps({**proposal_value, "unexpected": True}))

        transition = parse_transition_input(
            "activate",
            json.dumps(
                {
                    "attempt": "attempt-1",
                    "branch": "codex/attempt-1",
                    "base_revision": "abc123",
                    "owner": "worker",
                }
            ),
        )
        self.assertIsInstance(transition, ActivateInput)
        if not isinstance(transition, ActivateInput):
            self.fail("activate payload did not produce ActivateInput")
        self.assertEqual("attempt-1", transition.attempt)
        with self.assertRaises(TransitionInputError):
            parse_transition_input("resume", '{"unexpected": true}')

    def test_json_reader_reports_syntax_root_and_io_failures(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing.json"

        with self.assertRaisesRegex(CoordinatorError, "Cannot decode"):
            parse_coordinator("{")
        with self.assertRaisesRegex(CoordinatorError, "Expected `object`, got `array`"):
            parse_coordinator("[]")
        with self.assertRaisesRegex(CoordinatorError, "Cannot read"):
            read_coordinator(missing)

    def test_coordinator_decoder_preserves_msgspec_validation_provenance(self) -> None:
        valid: JsonObject = {
            "schema": "repo-work/v1",
            "project_root": "/project",
            "task_id": "task",
            "host_id": "local",
            "generation": 1,
            "registered_at": "2026-08-16T12:00:00Z",
        }
        cases = (
            ({**valid, "schema": "repo-work/v2"}, "$.schema"),
            ({**valid, "generation": 0}, "$.generation"),
            ({**valid, "generation": True}, "$.generation"),
            ({**valid, "task_id": ""}, "$.task_id"),
        )

        for value, path in cases:
            with self.subTest(path=path), self.assertRaisesRegex(CoordinatorError, "COORDINATOR_INVALID") as caught:
                parse_coordinator(json.dumps(value))
            self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)
            self.assertIn(path, str(caught.exception.__cause__))

        path = Path(tempfile.mkdtemp()) / "coordinator.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(CoordinatorError, "COORDINATOR_INVALID"):
            read_coordinator(path)

    def test_proposal_decoder_rejects_invalid_boundary_shapes(self) -> None:
        valid = proposal()
        missing_trigger = valid.copy()
        del missing_trigger["trigger"]
        missing_proposal_id = valid.copy()
        del missing_proposal_id["proposal_id"]
        cases = (
            ({**valid, "schema": "repo-work/v2"}, "schema"),
            ({**valid, "proposal_id": "Not Valid"}, "proposal_id"),
            (missing_proposal_id, "proposal_id"),
            ({**valid, "proposal_id": 1}, "proposal_id"),
            (missing_trigger, "trigger"),
            ({**valid, "trigger": 1}, "trigger"),
            ({**valid, "trigger": ""}, "trigger"),
            ({**valid, "trigger": "line\nbreak"}, "trigger"),
            ({**valid, "evidence": "source"}, "evidence"),
            ({**valid, "evidence": [""]}, "evidence[0]"),
            ({**valid, "relation": None}, "relation"),
            ({**valid, "relation": {"kind": "invented", "item": None}}, "relation.kind"),
            ({**valid, "relation": {"kind": "duplicate", "item": "Bad Item"}}, "relation.item"),
        )

        for value, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(ProposalError, "PROPOSAL_INVALID") as caught:
                parse_proposal(json.dumps(value))
            self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)
            self.assertIn(field, str(caught.exception.__cause__))

        path = Path(tempfile.mkdtemp()) / "proposal.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ProposalError, "PROPOSAL_INVALID"):
            read_proposal(path)

    def test_platform_contract_rejects_unsupported_systems_explicitly(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        with (
            patch("repo_work.atomic.sys.platform", "win32"),
            self.assertRaises(PlatformNotSupportedError),
            transition_lock(work),
        ):
            self.fail("unsupported platform acquired a transition lock")

    def test_transition_input_parser_rejects_invalid_closed_choices(self) -> None:
        cases: tuple[tuple[str, JsonObject, str], ...] = (
            (
                "activate",
                {"attempt": "line\nbreak", "branch": "codex/work", "base_revision": "abc123", "owner": "worker"},
                "TRANSITION_INPUT_INVALID",
            ),
            ("block", {"reason": "blocked", "depends_on": "item"}, "TRANSITION_INPUT_INVALID"),
            (
                "accept-proposal",
                {"item": "new", "state": "invented", "next_action": "review", "depends_on": []},
                "TRANSITION_INPUT_INVALID",
            ),
            (
                "accept-proposal",
                {"item": "new", "state": "active", "next_action": "review", "depends_on": []},
                "TRANSITION_INPUT_INVALID",
            ),
            (
                "accept-proposal",
                {
                    "item": "new",
                    "state": "intake",
                    "timing": 1,
                    "next_action": "review",
                    "depends_on": [],
                },
                "TRANSITION_INPUT_INVALID",
            ),
            ("close", {"outcome": "later", "reason": "Not terminal."}, "TRANSITION_INPUT_INVALID"),
            ("unknown", {}, "ACTION_NOT_MUTATING"),
        )
        for kind, value, code in cases:
            with self.subTest(kind=kind), self.assertRaisesRegex(TransitionInputError, code) as caught:
                parse_transition_input(kind, json.dumps(value))
            if kind != "unknown":
                self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)

    def test_transition_input_errors_include_the_native_json_path(self) -> None:
        with self.assertRaises(TransitionInputError) as caught:
            parse_transition_input(
                "activate",
                '{"attempt":"attempt-1","branch":"codex/work","base_revision":"abc","owner":1}',
            )

        cause = caught.exception.__cause__
        self.assertIsInstance(cause, msgspec.ValidationError)
        self.assertIn("$.owner", str(cause))


class MarkdownBoundaryTest(unittest.TestCase):
    def write(self, text: str, name: str = "record.md") -> Path:
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_header_parser_handles_scalars_comments_and_structural_errors(self) -> None:
        header = parse_header(
            self.write("---\n# comment\nquoted: 'value'\nempty: null\nenabled: true\ndisabled: false\n---\n")
        )
        self.assertEqual({"quoted": "value", "empty": None, "enabled": True, "disabled": False}, header)

        cases = (
            ("plain text", "HEADER_MISSING"),
            ("---\ninvalid\n---\n", "HEADER_FIELD_INVALID"),
            ("---\n: value\n---\n", "HEADER_FIELD_INVALID"),
            ("---\nkind: one\nkind: two\n---\n", "HEADER_FIELD_DUPLICATE"),
        )
        for text, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ParseError, code):
                parse_header(self.write(text))

    def test_item_current_and_attempt_parsers_reject_invalid_owned_fields(self) -> None:
        valid_item = "---\nkind: work-item\nschema: repo-work/v1\nitem: reveal-core\nuser_label: Reveal\n---\n"
        valid_current = (
            "---\nkind: work-current\nschema: repo-work/v1\nfocus_item: null\n"
            "focus_attempt: null\nnext_action: select\n---\n"
        )
        valid_attempt = (
            "---\nkind: work-attempt\nschema: repo-work/v1\nattempt: attempt-1\nitem: reveal-core\n"
            "state: active\nbranch: codex/reveal\nbase_revision: abc\nowner: worker\n---\n"
        )

        self.assertEqual("reveal-core", parse_item(self.write(valid_item)).item)
        self.assertIsNone(parse_current(self.write(valid_current)).focus_item)
        self.assertEqual("attempt-1", parse_attempt(self.write(valid_attempt)).attempt)
        valid_v2_attempt = valid_attempt.replace("repo-work/v1", "repo-work/v2").replace(
            "owner: worker", "provenance: worker"
        )
        self.assertEqual("worker", parse_attempt(self.write(valid_v2_attempt)).provenance)
        with self.assertRaisesRegex(ParseError, "ATTEMPT_STATIC_OWNER_INVALID"):
            parse_attempt(self.write(valid_v2_attempt.replace("provenance: worker", "owner: worker")))

        cases = (
            (valid_item.replace("kind: work-item", "kind: other"), parse_item, "DOCUMENT_KIND_INVALID"),
            (valid_item.replace("repo-work/v1", "repo-work/v3"), parse_item, "DOCUMENT_SCHEMA_INVALID"),
            (valid_item.replace("reveal-core", "Bad Item"), parse_item, "ITEM_ID_INVALID"),
            (valid_current.replace("focus_item: null", "focus_item: Bad Item"), parse_current, "CURRENT_ITEM_INVALID"),
            (
                valid_current.replace("focus_attempt: null", "focus_attempt: Bad Attempt"),
                parse_current,
                "CURRENT_ATTEMPT_INVALID",
            ),
            (valid_attempt.replace("attempt-1", "Bad Attempt"), parse_attempt, "ATTEMPT_ID_INVALID"),
            (valid_attempt.replace("reveal-core", "Bad Item"), parse_attempt, "ATTEMPT_ITEM_INVALID"),
            (valid_attempt.replace("state: active", "state: invented"), parse_attempt, "ATTEMPT_STATE_INVALID"),
        )
        for text, parser, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ParseError, code):
                parser(self.write(text))

    def test_renderer_and_header_replacement_reject_unsafe_documents(self) -> None:
        path = Path(tempfile.mkdtemp()) / "queue.md"
        unsafe = QueueItem("item", WorkState.READY, None, (), None, "source", "activate", "line|break")
        queue = Queue(path, {}, (unsafe,), "")
        with self.assertRaisesRegex(ValueError, "QUEUE_CELL_UNSAFE"):
            render_queue(queue, queue.items)

        with self.assertRaisesRegex(ValueError, "HEADER_MISSING"):
            replace_header_fields("plain", {"state": "done"})
        with self.assertRaisesRegex(ValueError, "HEADER_UNTERMINATED"):
            replace_header_fields("---\nstate: active\n", {"state": "done"})
        with self.assertRaisesRegex(ValueError, "HEADER_FIELD_REQUIRED"):
            replace_header_fields("---\nstate: active\n---\n", {"missing": "value"})
        replaced = replace_header_fields(
            "---\n# note\nstate: active\n---\n",
            {"state": "done"},
            {"evidence": "accepted"},
        )
        self.assertIn("state: done", replaced)
        self.assertIn("evidence: accepted", replaced)

    def test_v2_item_renderer_quotes_reserved_string_tokens_and_preserves_missing_sentinels(self) -> None:
        source = (
            "---\nkind: work-item\nschema: repo-work/v1\nitem: token-item\n"
            'user_label: "Token item"\n---\n\n# Token item\n'
        )
        item = QueueItem(
            "token-item",
            WorkState.ACTIVE,
            "true",
            ("true", "false", "null"),
            "true",
            "false",
            "null",
            "~",
        )

        rendered = render_v2_item(source, item, ("true", "false", "null"))
        record = parse_item(self.write(rendered))

        self.assertEqual(item, record.queue_item)
        self.assertEqual(("true", "false", "null"), record.resources)
        for expected in (
            'timing: "true"',
            'depends_on: "true, false, null"',
            'attempt: "true"',
            'source: "false"',
            'next_action: "null"',
            'notes: "~"',
            'resources: "true, false, null"',
        ):
            self.assertIn(expected, rendered)

        missing = QueueItem("token-item", WorkState.READY, None, (), None, "source", None, "Notes.")
        missing_rendered = render_v2_item(source, missing)
        for expected in ("timing: —", "depends_on: —", "attempt: —", "next_action: —", "resources: —"):
            self.assertIn(expected, missing_rendered)
        self.assertEqual(missing, parse_item(self.write(missing_rendered)).queue_item)

    def test_v2_queue_preserves_reserved_data_while_v1_keeps_legacy_empty_tokens(self) -> None:
        path = Path("queue.md")
        item = QueueItem(
            "token-item",
            WorkState.READY,
            "null",
            ("null", "none", "~"),
            "null",
            "source",
            "none",
            "Notes.",
        )
        queue = Queue(path, {}, (item,), "")

        rendered_v2 = render_queue(queue, queue.items, SCHEMA_V2)
        self.assertEqual(item, parse_queue_text(rendered_v2, path).items[0])

        rendered_v1 = render_queue(queue, queue.items, SCHEMA_V1)
        legacy = parse_queue_text(rendered_v1, path).items[0]
        self.assertIsNone(legacy.timing)
        self.assertEqual(("~",), legacy.depends_on)
        self.assertIsNone(legacy.attempt)
        self.assertIsNone(legacy.next_action)

    def test_v2_current_renderer_distinguishes_reserved_strings_from_absence(self) -> None:
        rendered = render_current("true", "false", "null", SCHEMA_V2)

        current = parse_current(self.write(rendered))

        self.assertEqual(("true", "false", "null"), (current.focus_item, current.focus_attempt, current.next_action))
        self.assertIn('focus_item: "true"', rendered)
        self.assertIn('focus_attempt: "false"', rendered)
        self.assertIn('next_action: "null"', rendered)

        absent = render_current(None, None, "select", SCHEMA_V2)
        self.assertIn("focus_item: null", absent)
        self.assertIn("focus_attempt: null", absent)
