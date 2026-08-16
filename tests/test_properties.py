import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from repo_work.actions import Action, actions_for
from repo_work.markdown import parse_queue, render_queue
from repo_work.model import Queue, QueueItem, WorkState
from repo_work.transition import apply_action
from repo_work.transition_input import TransitionInputError, parse_transition_input
from repo_work.validate import validate_work_state

from .support import create_state

ITEM_IDS = st.from_regex(r"[a-z][a-z0-9]{0,7}(?:-[a-z0-9]{1,8})?", fullmatch=True)


class ParseRenderProperties(unittest.TestCase):
    @settings(max_examples=50)
    @given(item_ids=st.lists(ITEM_IDS, max_size=12, unique=True))
    def test_queue_parse_render_round_trip(self, item_ids: list[str]) -> None:
        path = Path(tempfile.mkdtemp()) / "queue.md"
        items = tuple(
            QueueItem(
                item=item_id,
                state=WorkState.READY,
                timing=None,
                depends_on=(),
                attempt=None,
                source="property",
                next_action="activate",
                notes="Ready.",
            )
            for item_id in item_ids
        )
        queue = Queue(path=path, header={}, items=items, revision="")

        path.write_text(render_queue(queue, items), encoding="utf-8")

        self.assertEqual(items, parse_queue(path).items)

    @settings(max_examples=50)
    @given(invalid=st.one_of(st.none(), st.integers(), st.lists(st.integers()), st.dictionaries(st.text(), st.text())))
    def test_transition_input_rejects_non_string_attempt(self, invalid: object) -> None:
        value: dict[str, object] = {
            "attempt": invalid,
            "branch": "codex/reveal-core",
            "base_revision": "abc123",
            "owner": "worker",
        }

        with self.assertRaises(TransitionInputError):
            parse_transition_input("activate", value)


class WorkLedgerMachine(RuleBasedStateMachine):
    project: Path
    work: Path
    state: WorkState

    def __init__(self) -> None:
        super().__init__()
        self.project, self.work = create_state(
            ["| reveal-core | intake | — | — | — | finding | review-intake | Review. |"]
        )
        self.state = WorkState.INTAKE

    def action(self, action_id: str) -> Action:
        return next(
            candidate
            for candidate in actions_for(self.work, self.project, "coordinator")
            if candidate.action_id == action_id
        )

    def mark_ready(self) -> None:
        apply_action(
            self.work,
            self.project,
            self.action("mark-ready:reveal-core"),
            {"reason": "Property evidence is sufficient."},
        )
        self.state = WorkState.READY

    def activate(self) -> None:
        apply_action(
            self.work,
            self.project,
            self.action("activate:reveal-core"),
            {
                "attempt": "reveal-core-1",
                "branch": "codex/reveal-core",
                "base_revision": "abc123",
                "owner": "worker",
            },
        )
        self.state = WorkState.ACTIVE

    def pause(self) -> None:
        apply_action(
            self.work,
            self.project,
            self.action("pause:reveal-core-1"),
            {"reason": "Property prerequisite."},
        )
        self.state = WorkState.PAUSED

    def resume(self) -> None:
        apply_action(self.work, self.project, self.action("resume:reveal-core"), {})
        self.state = WorkState.ACTIVE

    @rule()
    def advance_legal_transition(self) -> None:
        if self.state == WorkState.INTAKE:
            self.mark_ready()
        elif self.state == WorkState.READY:
            self.activate()
        elif self.state == WorkState.ACTIVE:
            self.pause()
        elif self.state == WorkState.PAUSED:
            self.resume()
        else:
            raise AssertionError(f"state machine reached unsupported state {self.state}")

    @invariant()
    def state_remains_valid(self) -> None:
        if not validate_work_state(self.work, self.project).valid:
            raise AssertionError("generated legal transition sequence produced invalid state")


WorkLedgerMachine.TestCase.settings = settings(max_examples=20, stateful_step_count=12)
TestWorkLedgerMachine = WorkLedgerMachine.TestCase


if __name__ == "__main__":
    unittest.main()
