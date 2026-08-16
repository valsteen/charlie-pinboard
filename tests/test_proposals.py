from __future__ import annotations

import json
import unittest

from repo_work.actions import actions_for
from repo_work.markdown import parse_queue
from repo_work.model import WorkState
from repo_work.proposals import ProposalError, create_proposal
from repo_work.transition import apply_action
from repo_work.validate import validate_work_state
from tests.support import create_state


def proposal(proposal_id: str = "finding-1") -> dict[str, object]:
    return {
        "schema": "repo-work/v1",
        "proposal_id": proposal_id,
        "created_at": "2026-08-16T12:30:00Z",
        "source_task_id": "investigation-task",
        "user_label": "make Reveal independent of mappings",
        "trigger": "The mapping command owns a generic device selection operation.",
        "evidence": ["client/src/mappings.ts#reveal"],
        "why_it_matters": "The current owner widens changes and hides reuse.",
        "relation": {"kind": "independent", "item": None},
        "effect": "One provider owns Reveal semantics.",
        "unlock": "Mappings and the project tree can use one capability.",
        "urgency_evidence": "Required by the current product objective.",
        "freshness_assumptions": ["The mapping command still owns Reveal."],
    }


class ProposalTest(unittest.TestCase):
    def test_requires_registered_coordinator_before_persisting(self) -> None:
        project, work = create_state([])
        (work / "coordinator.json").unlink()

        with self.assertRaisesRegex(ProposalError, "COORDINATOR_NOT_REGISTERED"):
            create_proposal(work, project, proposal())

        self.assertEqual([], list((work / "inbox").glob("*.json")))

    def test_creates_immutable_unique_proposal(self) -> None:
        project, work = create_state([])

        path = create_proposal(work, project, proposal())
        original = path.read_bytes()

        with self.assertRaisesRegex(ProposalError, "PROPOSAL_ALREADY_EXISTS"):
            create_proposal(work, project, proposal())
        self.assertEqual(original, path.read_bytes())

    def test_idle_coordinator_sees_closed_proposal_choices(self) -> None:
        project, work = create_state([])
        create_proposal(work, project, proposal())

        action_ids = {action.action_id for action in actions_for(work, project, role="coordinator")}

        self.assertTrue(
            {
                "accept-proposal:finding-1",
                "merge-proposal:finding-1",
                "return-proposal:finding-1",
                "reject-proposal:finding-1",
            }.issubset(action_ids)
        )

    def test_accept_moves_proposal_into_canonical_intake(self) -> None:
        project, work = create_state([])
        create_proposal(work, project, proposal())
        action = next(
            action
            for action in actions_for(work, project, role="coordinator")
            if action.action_id == "accept-proposal:finding-1"
        )

        apply_action(
            work,
            project,
            action,
            {
                "item": "universal-reveal-core",
                "state": "intake",
                "timing": None,
                "depends_on": [],
                "next_action": "review-intake",
            },
        )

        queue = parse_queue(work / "queue.md")
        self.assertEqual("universal-reveal-core", queue.items[0].item)
        self.assertEqual(WorkState.INTAKE, queue.items[0].state)
        self.assertFalse((work / "inbox" / "finding-1.json").exists())
        disposition = json.loads(
            (work / "history" / "proposals" / "finding-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual("accepted", disposition["disposition"])
        self.assertTrue(validate_work_state(work, project).valid)


if __name__ == "__main__":
    unittest.main()
