import unittest

from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.domain.identifiers import ItemId, LeaseId
from tests.support import complete_sqlite_state


class DecisionProjectionTest(unittest.TestCase):
    def test_stored_state_projects_current_decision_facts(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())

        self.assertEqual("12", snapshot.revision)
        self.assertEqual(
            (ItemId("intake-work"), ItemId("work-a"), ItemId("work-c")),
            tuple(item.item for item in snapshot.items),
        )
        self.assertEqual((ItemId("work-c"),), snapshot.items_by_id()[ItemId("work-a")].depends_on)
        self.assertEqual(LeaseId("attempt-lease-a"), snapshot.attempt_authorities[0].lease_id)
        self.assertEqual(ItemId("work-a"), snapshot.focus_item)

    def test_terminal_work_is_history_not_live_work(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state())

        self.assertNotIn(ItemId("work-b"), snapshot.items_by_id())
        self.assertIn(ItemId("work-b"), snapshot.history_items)


if __name__ == "__main__":
    unittest.main()
