import json
import unittest

import msgspec

from charlie_pinboard.interfaces.errors import ProposalError
from charlie_pinboard.interfaces.proposals import parse_proposal
from tests.support import JsonObject


def proposal() -> JsonObject:
    return {
        "schema": "pinboard-proposal/v1",
        "proposal_id": "finding-1",
        "created_at": "2026-08-25T00:00:00Z",
        "source_task_id": "task",
        "user_label": "Finding",
        "trigger": "A current boundary exposed a missing behavior.",
        "evidence": ["source:test"],
        "why_it_matters": "The behavior must persist through SQLite.",
        "relation": {"kind": "independent", "item": None},
        "effect": "The proposal appears as visible intake.",
        "unlock": "Current proposal intake remains usable.",
        "urgency_evidence": "The installed command exercises this boundary.",
        "freshness_assumptions": ["SQLite remains authoritative."],
    }


class ProposalInputTest(unittest.TestCase):
    def test_current_proposal_decodes_exact_model(self) -> None:
        value = proposal()
        self.assertEqual("finding-1", parse_proposal(json.dumps(value)).proposal_id)
        self.assertEqual(2, parse_proposal(json.dumps({**value, "position": 2})).position)
        with self.assertRaises(ProposalError):
            parse_proposal(json.dumps({**value, "unexpected": True}))

    def test_decoder_rejects_invalid_shapes_and_reports_paths(self) -> None:
        valid = proposal()
        cases = (
            ({**valid, "schema": "repo" + "-work/v1"}, "schema"),
            ({**valid, "schema": "pinboard" + "-proposal/v2"}, "schema"),
            ({**valid, "proposal_id": "Not Valid"}, "proposal_id"),
            ({**valid, "trigger": ""}, "trigger"),
            ({**valid, "position": 0}, "position"),
            ({**valid, "evidence": [""]}, "evidence[0]"),
            ({**valid, "relation": {"kind": "invented", "item": None}}, "relation.kind"),
            ({**valid, "relation": {"kind": "independent", "item": "work-a"}}, "relation.item"),
            ({**valid, "relation": {"kind": "prerequisite", "item": None}}, "relation.item"),
        )
        for value, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(ProposalError, "PROPOSAL_INVALID") as caught:
                parse_proposal(json.dumps(value))
            self.assertIsInstance(caught.exception.__cause__, msgspec.ValidationError)
            self.assertIn(field, str(caught.exception.__cause__))


if __name__ == "__main__":
    unittest.main()
