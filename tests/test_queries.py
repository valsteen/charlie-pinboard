import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import discover_actions
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.errors import ActionQueryError, QueryError, QueryErrorCode
from charlie_pinboard.application.queries import (
    overview_from_state,
    preview_parallel,
)
from charlie_pinboard.application.stored_state import (
    StoredWorkState,
)
from charlie_pinboard.domain.decision_models import (
    ActionKind,
    Role,
)
from charlie_pinboard.domain.errors import DecisionFailureCode
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import (
    ItemId,
    LeaseId,
)
from charlie_pinboard.domain.work_models import AttemptState
from tests.support import SQLITE_NOW, complete_sqlite_state


class SQLiteQueriesTest(unittest.TestCase):
    def _valid_scope_digests(self, state: StoredWorkState) -> StoredWorkState:
        digests: dict[ItemId, str] = {}
        for scope in project_decision_snapshot(state).scopes:
            digest = item_scope_digest(scope.scope)
            if not isinstance(digest, str):
                self.fail(digest.message)
            digests[scope.item] = digest
        items = tuple(
            replace(item, scope_digest=digests.get(item.item_id, item.scope_digest))
            for item in state.lifecycle.work_items
        )
        anchors = tuple(
            replace(anchor, digest=digests.get(anchor.item_id, anchor.digest))
            for anchor in state.lifecycle.scope_revisions
        )
        attempts = tuple(
            replace(attempt, accepted_scope_digest=digests.get(attempt.item_id, attempt.accepted_scope_digest))
            for attempt in state.lifecycle.attempts
        )
        return replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=items, scope_revisions=anchors, attempts=attempts),
        )

    def _store(self, state: StoredWorkState | None = None) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(self._valid_scope_digests(state or complete_sqlite_state()))
        return store

    def test_overview_and_parallel_preview_read_only_sqlite_state(self) -> None:
        store = self._store()

        overview = overview_from_state(store.snapshot())
        preview = preview_parallel(store, now=SQLITE_NOW)

        self.assertEqual("sqlite-v1", overview.authority)
        self.assertEqual("12", overview.revision)
        self.assertEqual(("work-a-1",), overview.active_attempts)
        self.assertEqual(
            tuple(sorted(item.item_id for item in overview.items)), tuple(item.item_id for item in overview.items)
        )
        self.assertEqual("12", preview.revision)

    def test_parallel_preview_reports_the_current_attempt_not_retained_terminal_history(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        historical = replace(active, attempt_id=type(active.attempt_id)("aaa-old"), state=AttemptState.DONE)
        store = self._store(replace(state, lifecycle=replace(state.lifecycle, attempts=(historical, active))))

        preview = preview_parallel(store, selected=("work-a",), now=SQLITE_NOW)

        selected = (*preview.launchable, *preview.excluded)[0]
        self.assertEqual("work-a-1", selected.attempt_id)

    def test_action_and_query_failure_matrix_is_stable_and_read_only(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        store = self._store(state)
        coordination = state.authority.coordination
        assert coordination is not None

        observer = discover_actions(store, Role.OBSERVER, now=SQLITE_NOW)
        coordinator = discover_actions(
            store,
            Role.COORDINATOR,
            lease_id=coordination.lease_id,
            generation=coordination.generation,
            now=SQLITE_NOW,
        )
        worker = discover_actions(
            store,
            Role.WORKER,
            lease_id=LeaseId("attempt-lease-a"),
            generation=3,
            now=SQLITE_NOW,
        )

        self.assertEqual((ActionKind.INSPECT,), tuple(action.kind for action in observer))
        self.assertTrue(any(action.kind == ActionKind.DISPATCH for action in coordinator))
        self.assertTrue(any(action.kind == ActionKind.CONTINUE for action in worker))
        with self.assertRaises(ActionQueryError) as stale_coordination:
            discover_actions(
                store,
                Role.COORDINATOR,
                lease_id=LeaseId("wrong"),
                generation=coordination.generation,
                now=SQLITE_NOW,
            )
        self.assertEqual(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, stale_coordination.exception.code)
        with self.assertRaises(ActionQueryError) as missing_worker:
            discover_actions(store, Role.WORKER, now=SQLITE_NOW)
        self.assertEqual(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, missing_worker.exception.code)
        self.assertEqual(state, store.snapshot())

        with self.assertRaises(QueryError) as invalid_selection:
            preview_parallel(store, selected=("missing",), now=SQLITE_NOW)
        self.assertEqual(QueryErrorCode.PARALLEL_SELECTION_INVALID, invalid_selection.exception.code)
        with self.assertRaises(QueryError) as invalid_time:
            preview_parallel(store, now=SQLITE_NOW.replace(tzinfo=None))
        self.assertEqual(QueryErrorCode.PARALLEL_TIME_INVALID, invalid_time.exception.code)


if __name__ == "__main__":
    unittest.main()
