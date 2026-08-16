import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_work.atomic import PlatformNotSupportedError, transition_lock
from repo_work.coordinator import CoordinatorError, parse_coordinator, read_coordinator
from repo_work.json_values import JsonObjectError, parse_json_object, read_json_object
from repo_work.markdown import (
    ParseError,
    parse_attempt,
    parse_current,
    parse_header,
    parse_item,
    render_queue,
    replace_header_fields,
)
from repo_work.model import Queue, QueueItem, WorkState
from repo_work.proposals import ProposalError, parse_proposal, read_proposal
from repo_work.transition_input import TransitionInputError, parse_transition_input

from .test_transition import proposal


class JsonBoundaryTest(unittest.TestCase):
    def test_json_reader_reports_syntax_root_and_io_failures(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing.json"

        with self.assertRaisesRegex(JsonObjectError, "Cannot parse"):
            parse_json_object("{", code="INVALID", subject="document")
        with self.assertRaisesRegex(JsonObjectError, "root must be an object"):
            parse_json_object("[]", code="INVALID", subject="document")
        with self.assertRaisesRegex(JsonObjectError, "Cannot read"):
            read_json_object(missing, code="INVALID", subject="document")

    def test_coordinator_parser_rejects_invalid_exact_fields(self) -> None:
        valid: dict[str, object] = {
            "schema": "repo-work/v1",
            "project_root": "/project",
            "task_id": "task",
            "host_id": "local",
            "generation": 1,
            "registered_at": "2026-08-16T12:00:00Z",
        }
        cases = (
            ({**valid, "schema": "repo-work/v2"}, "COORDINATOR_SCHEMA_INVALID"),
            ({**valid, "generation": 0}, "COORDINATOR_GENERATION_INVALID"),
            ({**valid, "generation": True}, "COORDINATOR_GENERATION_INVALID"),
            ({**valid, "task_id": ""}, "COORDINATOR_FIELD_INVALID"),
        )

        for value, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(CoordinatorError, code):
                parse_coordinator(value)

        path = Path(tempfile.mkdtemp()) / "coordinator.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(CoordinatorError, "COORDINATOR_INVALID"):
            read_coordinator(path)

    def test_proposal_parser_rejects_invalid_boundary_shapes(self) -> None:
        valid = proposal()
        cases = (
            ({**valid, "schema": "repo-work/v2"}, "PROPOSAL_SCHEMA_INVALID"),
            ({**valid, "proposal_id": "Not Valid"}, "PROPOSAL_ID_INVALID"),
            ({**valid, "trigger": ""}, "PROPOSAL_FIELD_REQUIRED"),
            ({**valid, "trigger": "line\nbreak"}, "PROPOSAL_FIELD_INVALID"),
            ({**valid, "evidence": "source"}, "PROPOSAL_FIELD_INVALID"),
            ({**valid, "relation": None}, "PROPOSAL_RELATION_INVALID"),
            ({**valid, "relation": {"kind": "invented", "item": None}}, "PROPOSAL_RELATION_INVALID"),
            ({**valid, "relation": {"kind": "duplicate", "item": "Bad Item"}}, "PROPOSAL_RELATION_INVALID"),
        )

        for value, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ProposalError, code):
                parse_proposal(value)

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
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            ("activate", {"attempt": "line\nbreak"}, "TRANSITION_INPUT_INVALID"),
            ("block", {"reason": "blocked", "depends_on": "item"}, "TRANSITION_INPUT_INVALID"),
            (
                "accept-proposal",
                {"item": "new", "state": "invented", "next_action": "review", "depends_on": list[str]()},
                "TRANSITION_INPUT_INVALID",
            ),
            (
                "accept-proposal",
                {"item": "new", "state": "active", "next_action": "review", "depends_on": list[str]()},
                "TRANSITION_INPUT_INVALID",
            ),
            (
                "accept-proposal",
                {
                    "item": "new",
                    "state": "intake",
                    "timing": 1,
                    "next_action": "review",
                    "depends_on": list[str](),
                },
                "TRANSITION_INPUT_INVALID",
            ),
            ("unknown", {}, "ACTION_NOT_MUTATING"),
        )
        for kind, value, code in cases:
            with self.subTest(kind=kind), self.assertRaisesRegex(TransitionInputError, code):
                parse_transition_input(kind, value)


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

        cases = (
            (valid_item.replace("kind: work-item", "kind: other"), parse_item, "DOCUMENT_KIND_INVALID"),
            (valid_item.replace("repo-work/v1", "repo-work/v2"), parse_item, "DOCUMENT_SCHEMA_INVALID"),
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
