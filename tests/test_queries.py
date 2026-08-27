import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import ActionQueryError, discover_actions
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.queries import (
    DetailLevel,
    QueryError,
    preview_parallel,
    read_overview,
    read_resource_conflict,
)
from charlie_pinboard.application.stored_state import (
    ItemResourceRequirement,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.decisions import ActionKind, Role
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import (
    ActionId,
    HistoryId,
    HistorySubjectId,
    ItemId,
    LeaseId,
    ResourceInstanceId,
)
from charlie_pinboard.domain.model import (
    AttemptState,
    ReservationState,
    UseLeaseGenerationKind,
    UseLeaseState,
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
        impacts = tuple(
            replace(impact, source_scope_digest=digests.get(impact.source_item_id, impact.source_scope_digest))
            for impact in state.planning.impacts
        )
        return replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=items, scope_revisions=anchors, attempts=attempts),
            planning=replace(state.planning, impacts=impacts),
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

        overview = read_overview(store)
        preview = preview_parallel(store, "host-a", now=SQLITE_NOW)

        self.assertEqual("sqlite-v1", overview.authority)
        self.assertEqual("12", overview.revision)
        self.assertEqual(("work-a-1",), overview.active_attempts)
        self.assertEqual(
            tuple(sorted(item.item_id for item in overview.items)), tuple(item.item_id for item in overview.items)
        )
        self.assertEqual("12", preview.revision)
        self.assertEqual("host-a", preview.host_id)

    def test_parallel_preview_classifies_shared_resource_candidates(self) -> None:
        state = complete_sqlite_state()
        items = tuple(
            replace(item, state=StoredWorkItemState.READY)
            if item.item_id in {ItemId("work-a"), ItemId("work-c")}
            else item
            for item in state.lifecycle.work_items
        )
        requirement = state.resources.requirements[0]
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=items,
                attempts=tuple(replace(attempt, state=AttemptState.DONE) for attempt in state.lifecycle.attempts),
                dependencies=(),
            ),
            authority=replace(state.authority, attempt_counters=(), attempt_generations=(), attempt_leases=()),
            resources=replace(
                state.resources,
                requirements=(
                    requirement,
                    ItemResourceRequirement(ItemId("work-c"), requirement.resource_id, 0),
                ),
                reservations=(),
                use_leases=(),
                mutation_intents=(),
            ),
            focus=replace(state.focus, item_id=None, attempt_id=None, next_action="select"),
        )
        store = self._store(state)

        preview = preview_parallel(store, "host-a", now=SQLITE_NOW)
        selected = preview_parallel(store, "host-a", selected=("work-a", "work-c"), now=SQLITE_NOW)

        self.assertEqual(("work-a", "work-c"), tuple(item.item_id for item in preview.requires_selection))
        self.assertFalse(selected.safe)
        self.assertEqual(("work-a", "work-c"), tuple(item.item_id for item in selected.excluded))

    def test_parallel_preview_reports_the_current_attempt_not_retained_terminal_history(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        historical = replace(active, attempt_id=type(active.attempt_id)("aaa-old"), state=AttemptState.DONE)
        store = self._store(replace(state, lifecycle=replace(state.lifecycle, attempts=(historical, active))))

        preview = preview_parallel(store, "host-a", selected=("work-a",), now=SQLITE_NOW)

        selected = (*preview.launchable, *preview.requires_selection, *preview.excluded)[0]
        self.assertEqual("work-a-1", selected.attempt_id)

    def test_resource_conflict_is_compact_by_default_and_detailed_on_request(self) -> None:
        state = complete_sqlite_state()
        intent_receipt = replace(
            state.history.receipts[0],
            history_id=HistoryId(2),
            project_revision=12,
            action_id=ActionId("inspect:register-mutation:intent-a"),
            subject_id=HistorySubjectId("intent-a"),
        )
        store = self._store(
            replace(state, history=replace(state.history, receipts=(*state.history.receipts, intent_receipt)))
        )
        compact = read_resource_conflict(store, ResourceInstanceId("workspace-on-host"), DetailLevel.COMPACT)
        detailed = read_resource_conflict(store, ResourceInstanceId("workspace-on-host"), DetailLevel.DETAILED)

        self.assertIsNone(compact.locator)
        self.assertIsNotNone(detailed.locator)
        self.assertEqual("workspace", detailed.resource_id)
        self.assertTrue(detailed.legal_actions)
        self.assertEqual("workspace", detailed.definition_kind)
        self.assertEqual("host-a", detailed.host_id)
        self.assertEqual(2, detailed.observation_generation)
        self.assertEqual("reservation-a", detailed.reservation_id)
        self.assertTrue(detailed.task_uses)
        self.assertTrue(detailed.mutation_intents)
        self.assertIsNotNone(detailed.attempt_authority)
        self.assertTrue(detailed.history)
        self.assertIn("intent-a", tuple(value.subject_id for value in detailed.history))
        self.assertNotIn("release-reservation", detailed.legal_actions)
        self.assertIn("revoke-reservation", detailed.legal_actions)

    def test_pending_recovery_resource_conflict_offers_only_fenced_intent_resolution(self) -> None:
        state = complete_sqlite_state()
        reservations = tuple(
            replace(value, state=ReservationState.REVOKED_PENDING_RECOVERY) for value in state.resources.reservations
        )
        counters = tuple(
            replace(value, generation_high_water=2) if value.instance_id == reservations[0].instance_id else value
            for value in state.resources.reservation_counters
        )
        revoked_use_leases = tuple(
            replace(value, state=UseLeaseState.REVOKED) if value.state == UseLeaseState.ACTIVE else value
            for value in state.resources.use_leases
        )
        latest_grant = revoked_use_leases[-1]
        use_leases = (
            *revoked_use_leases,
            replace(
                latest_grant,
                lease_id=LeaseId("use-recovery-fence"),
                generation=latest_grant.generation + 1,
                generation_kind=UseLeaseGenerationKind.FENCE,
            ),
        )
        store = self._store(
            replace(
                state,
                resources=replace(
                    state.resources,
                    reservation_counters=counters,
                    reservations=reservations,
                    use_leases=use_leases,
                ),
            )
        )

        detailed = read_resource_conflict(store, ResourceInstanceId("workspace-on-host"), DetailLevel.DETAILED)

        self.assertEqual("revoked-pending-recovery", detailed.reservation_state)
        self.assertEqual(("inspect", "resolve-fenced-resource-intent"), detailed.legal_actions)

    def test_active_resource_without_planned_intent_omits_preservation(self) -> None:
        state = complete_sqlite_state()
        store = self._store(replace(state, resources=replace(state.resources, mutation_intents=())))

        detailed = read_resource_conflict(store, ResourceInstanceId("workspace-on-host"), DetailLevel.DETAILED)

        self.assertEqual("active", detailed.reservation_state)
        self.assertEqual(("inspect", "release-reservation", "revoke-reservation"), detailed.legal_actions)

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
        self.assertEqual("COORDINATION_LEASE_REQUIRED", stale_coordination.exception.code)
        with self.assertRaises(ActionQueryError) as missing_worker:
            discover_actions(store, Role.WORKER, now=SQLITE_NOW)
        self.assertEqual("ATTEMPT_LEASE_REQUIRED", missing_worker.exception.code)
        self.assertEqual(state, store.snapshot())

        with self.assertRaises(QueryError) as missing_instance:
            read_resource_conflict(store, ResourceInstanceId("missing"))
        self.assertEqual("RESOURCE_INSTANCE_REQUIRED", missing_instance.exception.code)
        with self.assertRaises(QueryError) as invalid_selection:
            preview_parallel(store, "host-a", selected=("missing",), now=SQLITE_NOW)
        self.assertEqual("PARALLEL_SELECTION_INVALID", invalid_selection.exception.code)
        with self.assertRaises(QueryError) as invalid_time:
            preview_parallel(store, "host-a", now=SQLITE_NOW.replace(tzinfo=None))
        self.assertEqual("PARALLEL_TIME_INVALID", invalid_time.exception.code)


if __name__ == "__main__":
    unittest.main()
