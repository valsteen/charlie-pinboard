import json
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from charlie_pinboard.domain.model import WorkState
from charlie_pinboard.interfaces.transition_input import TransitionInputError, parse_transition_input
from charlie_pinboard.legacy.actions import Action, actions_for
from charlie_pinboard.legacy.markdown import Queue, QueueItem, parse_queue, render_queue
from charlie_pinboard.legacy.validate import validate_work_state

from .support import JsonValue, apply_action, create_state

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
    def test_transition_input_rejects_non_string_attempt(self, invalid: JsonValue) -> None:
        value: dict[str, JsonValue] = {
            "attempt": invalid,
            "branch": "codex/reveal-core",
            "base_revision": "abc123",
            "owner": "worker",
        }

        with self.assertRaises(TransitionInputError):
            parse_transition_input("activate", json.dumps(value))


class WorkLedgerLifecycleTest(unittest.TestCase):
    def test_intake_activation_shelving_and_resumption_remain_valid(self) -> None:
        project, work = create_state(["| reveal-core | intake | — | — | — | finding | review-intake | Review. |"])

        def action(action_id: str) -> Action:
            return next(
                candidate for candidate in actions_for(work, project, "coordinator") if candidate.action_id == action_id
            )

        apply_action(
            work,
            project,
            action("mark-ready:reveal-core"),
            {"reason": "Lifecycle evidence is sufficient."},
        )
        self.assertTrue(validate_work_state(work, project).valid)

        apply_action(
            work,
            project,
            action("activate:reveal-core"),
            {
                "attempt": "reveal-core-1",
                "branch": "codex/reveal-core",
                "base_revision": "abc123",
                "owner": "worker",
            },
        )
        self.assertTrue(validate_work_state(work, project).valid)

        for _ in range(2):
            apply_action(
                work,
                project,
                action("pause:reveal-core-1"),
                {"reason": "Lifecycle prerequisite."},
            )
            self.assertTrue(validate_work_state(work, project).valid)
            apply_action(work, project, action("resume:reveal-core"), {})
            self.assertTrue(validate_work_state(work, project).valid)


if __name__ == "__main__":
    unittest.main()
