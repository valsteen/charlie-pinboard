import json
import unittest

import msgspec
from hypothesis import given, settings
from hypothesis import strategies as st

from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId
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
            decision_models.ActionKind.ACTIVATE,
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
            work_models.ActivateInput(AttemptId("attempt-1"), "codex/attempt-1", "abc123", "worker", ArtifactRefId(7)),
            activation,
        )
        self.assertEqual(
            work_models.ResumeInput(),
            parse_transition_input(decision_models.ActionKind.RESUME, "{}"),
        )
        self.assertEqual(
            work_models.ResumeInput(ArtifactRefId(8)),
            parse_transition_input(decision_models.ActionKind.RESUME, '{"brief_artifact_ref_id":8}'),
        )

        checkpoint = parse_transition_input(
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            '{"checkpoint":"design-accepted","candidate":"sha256:candidate","evidence":"accepted"}',
        )
        self.assertIsInstance(checkpoint, work_models.AcceptCheckpointInput)

        continuation = parse_transition_input(
            decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
            '{"candidate":"sha256:candidate","evidence":"review accepted"}',
        )
        self.assertEqual(
            work_models.AcceptReviewAndContinueInput(CandidateId("sha256:candidate"), "review accepted"),
            continuation,
        )

    def test_invalid_closed_choices_report_native_paths(self) -> None:
        cases: tuple[tuple[decision_models.ActionKind, JsonObject], ...] = (
            (
                decision_models.ActionKind.ACTIVATE,
                {
                    "attempt": "bad\nvalue",
                    "branch": "branch",
                    "base_revision": "base",
                    "owner": "task",
                    "brief_artifact_ref_id": 1,
                },
            ),
            (
                decision_models.ActionKind.ACCEPT_CHECKPOINT,
                {"checkpoint": "Bad Checkpoint", "candidate": "candidate", "evidence": "accepted"},
            ),
            (decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, {"candidate": "candidate", "evidence": ""}),
            (
                decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
                {"candidate": "candidate", "evidence": "accepted", "unexpected": True},
            ),
            (decision_models.ActionKind.SUBMIT_REVIEW, {"candidate": 1}),
        )
        for kind, value in cases:
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(TransitionInputError, "TRANSITION_INPUT_INVALID") as caught,
            ):
                parse_transition_input(kind, json.dumps(value))
            self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)

        with self.assertRaisesRegex(TransitionInputError, "ACTION_NOT_MUTATING"):
            parse_transition_input(decision_models.ActionKind.REPORT_BLOCKER, "{}")

    def test_every_current_kind_decodes_and_has_a_schema(self) -> None:
        payloads: dict[decision_models.ActionKind, JsonObject] = {
            decision_models.ActionKind.ACCEPT_CHECKPOINT: {
                "checkpoint": "checkpoint-a",
                "candidate": "candidate",
                "evidence": "accepted",
            },
            decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE: {"candidate": "candidate", "evidence": "accepted"},
            decision_models.ActionKind.ACCEPT_PROPOSAL: {
                "item": "work-a",
                "state": "intake",
                "next_action": "review",
                "timing": "must-now",
                "depends_on": [],
            },
            decision_models.ActionKind.ACTIVATE: {
                "attempt": "work-a-1",
                "branch": "codex/work-a",
                "base_revision": "base",
                "owner": "task",
                "brief_artifact_ref_id": 1,
            },
            decision_models.ActionKind.BLOCK: {"reason": "blocked", "depends_on": ["work-b"]},
            decision_models.ActionKind.BLOCK_ITEM: {"reason": "blocked", "depends_on": []},
            decision_models.ActionKind.CLOSE: {"outcome": "done", "reason": "complete"},
            decision_models.ActionKind.COMPLETE: {"evidence": "complete"},
            decision_models.ActionKind.DEFER: {"timing": "safe-to-defer", "reopen_condition": "when needed"},
            decision_models.ActionKind.MARK_READY: {"reason": "ready"},
            decision_models.ActionKind.MERGE_PROPOSAL: {"target": "work-a"},
            decision_models.ActionKind.PAUSE: {"reason": "pause"},
            decision_models.ActionKind.REJECT_PROPOSAL: {"reason": "reject"},
            decision_models.ActionKind.REOPEN: {"evidence": "reopen"},
            decision_models.ActionKind.RESUME: {},
            decision_models.ActionKind.RETURN_FOR_CORRECTION: {"reason": "correct"},
            decision_models.ActionKind.RETURN_PROPOSAL: {"reason": "more evidence"},
            decision_models.ActionKind.SUBMIT_REVIEW: {"candidate": "candidate"},
            decision_models.ActionKind.TRANSFER_COORDINATOR: {"task_id": "task-b", "host_id": "host-b"},
        }
        self.assertEqual(set(TRANSITION_ACTION_KINDS), {kind.value for kind in payloads})
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
            parse_transition_input(decision_models.ActionKind.ACTIVATE, json.dumps(value))


if __name__ == "__main__":
    unittest.main()
