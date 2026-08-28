import json
import unittest

import msgspec
from hypothesis import given, settings
from hypothesis import strategies as st

from charlie_pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId
from charlie_pinboard.domain.work_models import (
    AcceptCheckpointInput,
    AcceptReviewAndContinueInput,
    ActivateInput,
    ResumeInput,
)
from charlie_pinboard.interfaces.errors import TransitionInputError
from charlie_pinboard.interfaces.transition_input import (
    TRANSITION_ACTION_KINDS,
    encoded_transition_input_schema,
    parse_transition_input,
)
from tests.support import JsonObject, JsonValue


class TransitionInputTest(unittest.TestCase):
    def test_current_inputs_decode_exact_models(self) -> None:
        activation = parse_transition_input(
            "activate",
            json.dumps(
                {
                    "attempt": "attempt-1",
                    "branch": "codex/attempt-1",
                    "base_revision": "abc123",
                    "owner": "worker",
                    "brief_artifact_ref_id": 7,
                }
            ),
        )
        self.assertEqual(
            ActivateInput(AttemptId("attempt-1"), "codex/attempt-1", "abc123", "worker", ArtifactRefId(7)),
            activation,
        )
        self.assertEqual(ResumeInput(), parse_transition_input("resume", "{}"))
        self.assertEqual(ResumeInput(ArtifactRefId(8)), parse_transition_input("resume", '{"brief_artifact_ref_id":8}'))

        checkpoint = parse_transition_input(
            "accept-checkpoint",
            '{"checkpoint":"design-accepted","candidate":"sha256:candidate","evidence":"accepted"}',
        )
        self.assertIsInstance(checkpoint, AcceptCheckpointInput)

        continuation = parse_transition_input(
            "accept-review-and-continue",
            '{"candidate":"sha256:candidate","evidence":"review accepted"}',
        )
        self.assertEqual(
            AcceptReviewAndContinueInput(CandidateId("sha256:candidate"), "review accepted"),
            continuation,
        )

    def test_invalid_closed_choices_report_native_paths(self) -> None:
        cases: tuple[tuple[str, JsonObject], ...] = (
            (
                "activate",
                {
                    "attempt": "bad\nvalue",
                    "branch": "branch",
                    "base_revision": "base",
                    "owner": "task",
                    "brief_artifact_ref_id": 1,
                },
            ),
            ("accept-checkpoint", {"checkpoint": "Bad Checkpoint", "candidate": "candidate", "evidence": "accepted"}),
            ("accept-review-and-continue", {"candidate": "candidate", "evidence": ""}),
            (
                "accept-review-and-continue",
                {"candidate": "candidate", "evidence": "accepted", "unexpected": True},
            ),
            ("submit-review", {"candidate": 1}),
        )
        for kind, value in cases:
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(TransitionInputError, "TRANSITION_INPUT_INVALID") as caught,
            ):
                parse_transition_input(kind, json.dumps(value))
            self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)

        with self.assertRaisesRegex(TransitionInputError, "ACTION_NOT_MUTATING"):
            parse_transition_input("unknown", "{}")

    def test_every_current_kind_decodes_and_has_a_schema(self) -> None:
        payloads: dict[str, JsonObject] = {
            "accept-checkpoint": {"checkpoint": "checkpoint-a", "candidate": "candidate", "evidence": "accepted"},
            "accept-review-and-continue": {"candidate": "candidate", "evidence": "accepted"},
            "accept-proposal": {
                "item": "work-a",
                "state": "intake",
                "next_action": "review",
                "timing": "must-now",
                "depends_on": [],
            },
            "activate": {
                "attempt": "work-a-1",
                "branch": "codex/work-a",
                "base_revision": "base",
                "owner": "task",
                "brief_artifact_ref_id": 1,
            },
            "block": {"reason": "blocked", "depends_on": ["work-b"]},
            "block-item": {"reason": "blocked", "depends_on": []},
            "close": {"outcome": "done", "reason": "complete"},
            "complete": {"evidence": "complete"},
            "defer": {"timing": "safe-to-defer", "reopen_condition": "when needed"},
            "mark-ready": {"reason": "ready"},
            "merge-proposal": {"target": "work-a"},
            "pause": {"reason": "pause"},
            "reject-proposal": {"reason": "reject"},
            "reopen": {"evidence": "reopen"},
            "resume": {},
            "return-for-correction": {"reason": "correct"},
            "return-proposal": {"reason": "more evidence"},
            "submit-review": {"candidate": "candidate"},
            "transfer-coordinator": {"task_id": "task-b", "host_id": "host-b"},
        }
        self.assertEqual(set(TRANSITION_ACTION_KINDS), set(payloads))
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                self.assertIsNotNone(parse_transition_input(kind, json.dumps(payload)))
                schema = encoded_transition_input_schema(kind)
                self.assertIn(b'"type":"object"', schema)

    @settings(max_examples=50)
    @given(invalid=st.one_of(st.none(), st.integers(), st.lists(st.integers()), st.dictionaries(st.text(), st.text())))
    def test_activate_rejects_non_string_attempt(self, invalid: JsonValue) -> None:
        value: dict[str, JsonValue] = {
            "attempt": invalid,
            "branch": "codex/reveal-core",
            "base_revision": "abc123",
            "owner": "worker",
            "brief_artifact_ref_id": 1,
        }
        with self.assertRaises(TransitionInputError):
            parse_transition_input("activate", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
