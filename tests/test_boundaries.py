import json
import tempfile
import unittest
from pathlib import Path

import msgspec

from charlie_pinboard.domain.identifiers import ArtifactRefId, AttemptId
from charlie_pinboard.domain.model import AcceptCheckpointInput, ActivateInput, ResumeInput
from charlie_pinboard.interfaces.proposals import ProposalError, parse_proposal, read_proposal
from charlie_pinboard.interfaces.transition_input import (
    TRANSITION_ACTION_KINDS,
    TransitionInputError,
    encoded_transition_input_schema,
    parse_transition_input,
)
from tests.support import JsonObject


def proposal() -> JsonObject:
    return {
        "schema": "repo-work/v1",
        "proposal_id": "finding-1",
        "created_at": "2026-08-25T00:00:00Z",
        "source_task_id": "task",
        "user_label": "Finding",
        "trigger": "A current boundary exposed a missing behavior.",
        "evidence": ["source:test"],
        "why_it_matters": "The behavior must persist through SQLite.",
        "relation": {"kind": "independent", "item": None},
        "effect": "The proposal appears in the inbox.",
        "unlock": "Current proposal intake remains usable.",
        "urgency_evidence": "The installed command exercises this boundary.",
        "freshness_assumptions": ["SQLite remains authoritative."],
    }


class JsonBoundaryTest(unittest.TestCase):
    def test_current_json_boundaries_decode_exact_models(self) -> None:
        value = proposal()
        self.assertEqual("finding-1", parse_proposal(json.dumps(value)).proposal_id)
        with self.assertRaises(ProposalError):
            parse_proposal(json.dumps({**value, "unexpected": True}))

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

    def test_proposal_decoder_rejects_invalid_shapes_and_reports_paths(self) -> None:
        valid = proposal()
        cases = (
            ({**valid, "schema": "repo-work/v2"}, "schema"),
            ({**valid, "proposal_id": "Not Valid"}, "proposal_id"),
            ({**valid, "trigger": ""}, "trigger"),
            ({**valid, "evidence": [""]}, "evidence[0]"),
            ({**valid, "relation": {"kind": "invented", "item": None}}, "relation.kind"),
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
        path.write_text(json.dumps(valid), encoding="utf-8")
        self.assertEqual("finding-1", read_proposal(path).proposal_id)
        with self.assertRaisesRegex(ProposalError, "PROPOSAL_INVALID"):
            read_proposal(path.parent / "missing.json")

    def test_transition_input_rejects_invalid_closed_choices_with_native_paths(self) -> None:
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

    def test_every_current_transition_input_kind_decodes_and_has_a_schema(self) -> None:
        payloads: dict[str, JsonObject] = {
            "accept-checkpoint": {"checkpoint": "checkpoint-a", "candidate": "candidate", "evidence": "accepted"},
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


if __name__ == "__main__":
    unittest.main()
