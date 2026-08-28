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
    item_status,
    overview_from_state,
    preview_parallel,
)
from charlie_pinboard.application.query_models import ItemStatus, ItemStatusAttempt
from charlie_pinboard.application.stored_state import (
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.errors import DecisionFailureCode
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    ItemId,
    LeaseId,
)
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
            ("intake-work", "work-a", "work-c", "zz-proposal-a"),
            tuple(item.item_id for item in overview.items),
        )
        proposal = overview.items[-1]
        self.assertEqual((4, False, ("work-c",)), (proposal.position, proposal.eligible, proposal.depends_on))
        self.assertEqual("work-c", proposal.dependency_reasons[0].item_id)
        self.assertIn("Follow-up to work-c", proposal.dependency_reasons[0].reason)
        self.assertEqual((), proposal.review_flags)
        self.assertNotIn("zz-proposal-a", overview.immediate_options)
        self.assertEqual("12", preview.revision)

    def test_overview_exposes_duplicate_contradiction_and_clarification_for_review(self) -> None:
        for relation in (
            work_models.DuplicateProposalRelation(ItemId("work-c")),
            work_models.ContradictionProposalRelation(ItemId("work-c")),
            work_models.ClarificationProposalRelation(),
        ):
            state = complete_sqlite_state()
            proposal = state.proposals.proposals[0]
            changed = replace(proposal, relation=relation)
            dependencies = tuple(
                value for value in state.lifecycle.dependencies if value.item_id != ItemId("zz-proposal-a")
            )
            store = self._store(
                replace(
                    state,
                    lifecycle=replace(state.lifecycle, dependencies=dependencies),
                    proposals=replace(state.proposals, proposals=(changed,)),
                )
            )

            with self.subTest(relation=relation.kind.value):
                item = overview_from_state(store.snapshot()).items[-1]
                self.assertEqual((), item.depends_on)
                self.assertTrue(item.eligible)
                self.assertEqual(relation.kind, item.review_flags[0].kind)
                self.assertEqual(None if relation.item is None else "work-c", item.review_flags[0].related_item)

    def test_parallel_preview_reports_the_current_attempt_not_retained_terminal_history(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        historical = replace(active, attempt_id=type(active.attempt_id)("aaa-old"), state=work_models.AttemptState.DONE)
        store = self._store(replace(state, lifecycle=replace(state.lifecycle, attempts=(historical, active))))

        preview = preview_parallel(store, selected=("work-a",), now=SQLITE_NOW)

        selected = (*preview.launchable, *preview.excluded)[0]
        self.assertEqual("work-a-1", selected.attempt_id)

    def test_item_status_returns_exact_live_and_done_shapes_while_overview_stays_live_only(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        done_item = replace(
            state.lifecycle.work_items[2],
            state=StoredWorkItemState.DONE,
            timing=work_models.Timing.SAFE_TO_DEFER,
            outcome_evidence="accepted completion",
            next_action=None,
            notes=None,
        )
        done_attempts = (
            replace(
                active,
                attempt_id=AttemptId("work-b-z"),
                item_id=done_item.item_id,
                state=work_models.AttemptState.DONE,
                candidate_revision="candidate-z",
                candidate_recorded_at=SQLITE_NOW,
            ),
        )
        store = self._store(
            replace(
                state,
                lifecycle=replace(
                    state.lifecycle,
                    work_items=(*state.lifecycle.work_items[:2], done_item, *state.lifecycle.work_items[3:]),
                    attempts=(*state.lifecycle.attempts, *done_attempts),
                ),
            )
        )

        live = item_status(store, ItemId("work-a"))
        done = item_status(store, done_item.item_id)

        self.assertEqual(
            ItemStatus(
                "pinboard-item-status/v1",
                "sqlite-v1",
                "12",
                "work-a",
                "Work work-a",
                StoredWorkItemState.ACTIVE,
                work_models.Timing.MUST_NOW,
                None,
                "continue",
                "Current work remains bounded.",
                (ItemStatusAttempt("work-a-1", work_models.AttemptState.ACTIVE, None),),
            ),
            live,
        )
        self.assertEqual(
            ItemStatus(
                "pinboard-item-status/v1",
                "sqlite-v1",
                "12",
                "work-b",
                "Work work-b",
                StoredWorkItemState.DONE,
                work_models.Timing.SAFE_TO_DEFER,
                "accepted completion",
                None,
                "",
                (ItemStatusAttempt("work-b-z", work_models.AttemptState.DONE, "candidate-z"),),
            ),
            done,
        )
        self.assertNotIn("work-b", tuple(item.item_id for item in overview_from_state(store.snapshot()).items))

    def test_item_status_returns_terminal_siblings_with_non_null_attempt_arrays(self) -> None:
        state = complete_sqlite_state()
        for terminal in (
            StoredWorkItemState.SUPERSEDED,
            StoredWorkItemState.DROPPED,
        ):
            terminal_item = replace(
                state.lifecycle.work_items[2],
                state=terminal,
                outcome_evidence=f"{terminal.value} by decision",
                notes=None,
            )
            store = self._store(
                replace(
                    state,
                    lifecycle=replace(
                        state.lifecycle,
                        work_items=(*state.lifecycle.work_items[:2], terminal_item, *state.lifecycle.work_items[3:]),
                    ),
                )
            )

            with self.subTest(terminal=terminal.value):
                status = item_status(store, terminal_item.item_id)
                self.assertEqual(terminal.value, status.state.value)
                self.assertEqual((), status.attempts)
                self.assertEqual("", status.notes)

    def test_item_status_rejects_an_unknown_canonical_identity(self) -> None:
        store = self._store()

        with self.assertRaises(QueryError) as missing:
            item_status(store, ItemId("missing-item"))

        self.assertEqual(QueryErrorCode.ITEM_NOT_FOUND, missing.exception.code)

    def test_action_and_query_failure_matrix_is_stable_and_read_only(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        store = self._store(state)
        coordination = state.authority.coordination
        assert coordination is not None

        observer = discover_actions(store, decision_models.Role.OBSERVER, now=SQLITE_NOW)
        coordinator = discover_actions(
            store,
            decision_models.Role.COORDINATOR,
            lease_id=coordination.lease_id,
            generation=coordination.generation,
            now=SQLITE_NOW,
        )
        worker = discover_actions(
            store,
            decision_models.Role.WORKER,
            lease_id=LeaseId("attempt-lease-a"),
            generation=3,
            now=SQLITE_NOW,
        )

        self.assertEqual((decision_models.ActionKind.INSPECT,), tuple(action.kind for action in observer))
        self.assertTrue(any(action.kind == decision_models.ActionKind.DISPATCH for action in coordinator))
        self.assertTrue(any(action.kind == decision_models.ActionKind.CONTINUE for action in worker))
        with self.assertRaises(ActionQueryError) as stale_coordination:
            discover_actions(
                store,
                decision_models.Role.COORDINATOR,
                lease_id=LeaseId("wrong"),
                generation=coordination.generation,
                now=SQLITE_NOW,
            )
        self.assertEqual(DecisionFailureCode.COORDINATION_LEASE_REQUIRED, stale_coordination.exception.code)
        with self.assertRaises(ActionQueryError) as missing_worker:
            discover_actions(store, decision_models.Role.WORKER, now=SQLITE_NOW)
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
