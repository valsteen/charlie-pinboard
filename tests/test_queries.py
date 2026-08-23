import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import ActionQueryError, discover_actions
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.queries import (
    DetailLevel,
    PlanQueryError,
    PlanSnapshot,
    QueryError,
    compare_plan_snapshots,
    preview_parallel,
    read_overview,
    read_plan_snapshot,
    read_resource_conflict,
)
from charlie_pinboard.application.stored_state import (
    ItemResourceRequirement,
    ItemScopeRevision,
    PlanningObligationState,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.decisions import ActionKind, Role
from charlie_pinboard.domain.history import ItemScopeRecord, item_scope_digest
from charlie_pinboard.domain.identifiers import ItemId, LeaseId, ResourceInstanceId
from charlie_pinboard.domain.model import AttemptState
from tests.support import SQLITE_NOW, complete_sqlite_state


class SQLiteQueriesTest(unittest.TestCase):
    def _rehash_snapshot(self, snapshot: PlanSnapshot) -> PlanSnapshot:
        payload = msgspec.json.decode(msgspec.json.encode(snapshot))
        if not isinstance(payload, dict):
            self.fail("Plan snapshot must encode as an object")
        payload.pop("manifest_sha256")
        digest = hashlib.sha256(msgspec.json.encode(payload, order="sorted") + b"\n").hexdigest()
        return msgspec.structs.replace(snapshot, manifest_sha256=digest)

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

    def _exact_store(self, state: StoredWorkState) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)
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

    def test_plan_snapshot_closes_dependencies_and_compares_without_store(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        before = read_plan_snapshot(self._store(state), (ItemId("work-a"),), include_undecided=True)
        changed_items = tuple(
            replace(item, state=StoredWorkItemState.DEFERRED) if item.item_id == ItemId("work-c") else item
            for item in state.lifecycle.work_items
        )
        after_state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                project=replace(state.lifecycle.project, revision=13),
                work_items=changed_items,
            ),
        )
        after_store = self._store(after_state)
        after = read_plan_snapshot(after_store, (ItemId("work-a"),), include_undecided=True)

        self.assertEqual(("work-a", "work-c"), tuple(item.item_id for item in before.items))
        self.assertEqual("reconciled", before.status)
        change_set = compare_plan_snapshots(before, after)
        self.assertEqual(("work-c",), tuple(change.item_id for change in change_set.changes.lifecycle_only))
        self.assertEqual((), change_set.changes.scope_changed)

        with self.assertRaises(PlanQueryError) as mismatch:
            compare_plan_snapshots(
                before,
                read_plan_snapshot(after_store, (ItemId("work-a"),), include_undecided=False),
            )
        self.assertEqual("PLAN_SELECTION_MISMATCH", mismatch.exception.code)

    def test_resource_conflict_is_compact_by_default_and_detailed_on_request(self) -> None:
        store = self._store()
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

        with self.assertRaises(PlanQueryError) as invalid_root:
            read_plan_snapshot(store, (ItemId("missing"),))
        self.assertEqual("PLAN_SELECTION_INVALID", invalid_root.exception.code)
        with self.assertRaises(QueryError) as missing_instance:
            read_resource_conflict(store, ResourceInstanceId("missing"))
        self.assertEqual("RESOURCE_INSTANCE_REQUIRED", missing_instance.exception.code)
        with self.assertRaises(QueryError) as invalid_selection:
            preview_parallel(store, "host-a", selected=("missing",), now=SQLITE_NOW)
        self.assertEqual("PARALLEL_SELECTION_INVALID", invalid_selection.exception.code)
        with self.assertRaises(QueryError) as invalid_time:
            preview_parallel(store, "host-a", now=SQLITE_NOW.replace(tzinfo=None))
        self.assertEqual("PARALLEL_TIME_INVALID", invalid_time.exception.code)

    def test_plan_snapshot_reconciliation_direction_and_contradiction_matrix(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        before = read_plan_snapshot(self._store(state), (ItemId("work-a"),))
        later_state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                project=replace(state.lifecycle.project, revision=13),
            ),
        )
        later = read_plan_snapshot(self._store(later_state), (ItemId("work-a"),))
        with self.assertRaises(PlanQueryError) as reverse:
            compare_plan_snapshots(later, before)
        self.assertEqual("PLAN_COMPARISON_DIRECTION_INVALID", reverse.exception.code)

        changed_items = tuple(
            replace(item, state=StoredWorkItemState.DEFERRED) if item.item_id == ItemId("work-c") else item
            for item in state.lifecycle.work_items
        )
        contradictory_state = replace(
            state,
            lifecycle=replace(state.lifecycle, work_items=changed_items),
        )
        contradictory = read_plan_snapshot(self._store(contradictory_state), (ItemId("work-a"),))
        with self.assertRaises(PlanQueryError) as contradiction:
            compare_plan_snapshots(before, contradictory)
        self.assertEqual("PLAN_SNAPSHOT_CONTRADICTION", contradiction.exception.code)

        obligation = state.planning.obligations[0]
        unresolved = replace(
            obligation,
            state=PlanningObligationState.UNRESOLVED,
            disposition=None,
            evaluated_scope_revision=None,
            evaluated_scope_digest=None,
            resulting_scope_revision=None,
            resulting_scope_digest=None,
            primary_replacement_item_id=None,
            outcome_evidence=None,
            reason=None,
            resolved_project_revision=None,
            resolved_at=None,
        )
        unresolved_state = replace(
            state,
            planning=replace(state.planning, obligations=(unresolved,), replacements=()),
        )
        with self.assertRaises(PlanQueryError) as unreconciled:
            read_plan_snapshot(
                self._store(unresolved_state),
                (ItemId("work-a"),),
                require_reconciled=True,
            )
        self.assertEqual("PLAN_UNRECONCILED", unreconciled.exception.code)

    def test_plan_comparison_rejects_invalid_manifests_and_relevant_obligation_disappearance(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        before = read_plan_snapshot(self._store(state), (ItemId("work-a"),), include_undecided=True)
        invalid = self._rehash_snapshot(
            msgspec.structs.replace(
                before,
                application="wrong-application",
                database_schema_version=999,
                requested_roots=("missing",),
                items=(),
            )
        )
        with self.assertRaises(PlanQueryError) as malformed:
            compare_plan_snapshots(invalid, invalid)
        self.assertEqual("PLAN_SNAPSHOT_INVALID", malformed.exception.code)

        after = self._rehash_snapshot(
            msgspec.structs.replace(
                before,
                project_revision=before.project_revision + 1,
                resolved_obligations=(),
            )
        )
        with self.assertRaises(PlanQueryError) as disappeared:
            compare_plan_snapshots(before, after)
        self.assertEqual("PLAN_SNAPSHOT_CONTRADICTION", disappeared.exception.code)

    def test_plan_manifest_validation_covers_every_canonical_inventory(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        valid = read_plan_snapshot(self._store(state), (ItemId("work-a"),), include_undecided=True)
        first_item = valid.items[0]
        resolved = valid.resolved_obligations[0]
        proposal = valid.undecided[0]

        semantic = msgspec.json.decode(bytes(first_item.semantic), type=ItemScopeRecord)
        without_dependency = msgspec.structs.replace(semantic, dependencies=())
        encoded = msgspec.json.encode(without_dependency, order="sorted") + b"\n"
        semantic_digest = hashlib.sha256(encoded).hexdigest()
        extra_item = msgspec.structs.replace(
            first_item,
            scope_digest=semantic_digest,
            semantic=msgspec.Raw(encoded.rstrip(b"\n")),
        )

        invalid_snapshots = {
            "roots": msgspec.structs.replace(valid, requested_roots=()),
            "item-order": msgspec.structs.replace(valid, items=tuple(reversed(valid.items))),
            "semantic-json": msgspec.structs.replace(
                valid,
                items=(msgspec.structs.replace(first_item, semantic=msgspec.Raw(b"{}")), *valid.items[1:]),
            ),
            "item-facts": msgspec.structs.replace(
                valid,
                items=(msgspec.structs.replace(first_item, scope_revision=0), *valid.items[1:]),
            ),
            "root-presence": msgspec.structs.replace(valid, items=valid.items[1:]),
            "dependency-presence": msgspec.structs.replace(valid, items=(first_item,)),
            "exact-closure": msgspec.structs.replace(valid, items=(extra_item, *valid.items[1:])),
            "obligation-order": msgspec.structs.replace(
                valid,
                resolved_obligations=(resolved, resolved),
            ),
            "obligation-identity": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, target_position=-1),),
            ),
            "resolution-facts": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, replacements=()),),
            ),
            "obligation-relevance": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        source_item_id="outside-source",
                        target_item_id="outside-target",
                        disposition="dropped",
                        replacements=(),
                    ),
                ),
            ),
            "reconciliation-status": msgspec.structs.replace(valid, status="unreconciled"),
            "undecided-option": msgspec.structs.replace(valid, include_undecided=False),
            "undecided-order": msgspec.structs.replace(valid, undecided=(proposal, proposal)),
            "undecided-facts": msgspec.structs.replace(
                valid,
                undecided=(msgspec.structs.replace(proposal, user_label=""),),
            ),
        }
        for inventory, snapshot in invalid_snapshots.items():
            with self.subTest(inventory=inventory), self.assertRaises(PlanQueryError) as rejected:
                malformed = self._rehash_snapshot(snapshot)
                compare_plan_snapshots(malformed, malformed)
            self.assertEqual("PLAN_SNAPSHOT_INVALID", rejected.exception.code)

    def test_plan_change_set_carries_exact_components_lifecycle_and_obligation_records(self) -> None:
        state = self._valid_scope_digests(complete_sqlite_state())
        before = read_plan_snapshot(self._store(state), (ItemId("work-a"),))
        without_artifact = replace(state, lifecycle=replace(state.lifecycle, item_artifacts=()))
        changed_scope = next(
            value.scope
            for value in project_decision_snapshot(without_artifact).scopes
            if value.item == ItemId("work-a")
        )
        changed_digest = item_scope_digest(changed_scope)
        if not isinstance(changed_digest, str):
            self.fail(changed_digest.message)
        changed_items = tuple(
            replace(item, scope_revision=2, scope_digest=changed_digest)
            if item.item_id == ItemId("work-a")
            else replace(item, state=StoredWorkItemState.DEFERRED)
            if item.item_id == ItemId("work-c")
            else item
            for item in state.lifecycle.work_items
        )
        changed_state = replace(
            without_artifact,
            lifecycle=replace(
                without_artifact.lifecycle,
                project=replace(state.lifecycle.project, revision=13),
                work_items=changed_items,
                scope_revisions=(
                    *state.lifecycle.scope_revisions,
                    ItemScopeRevision(ItemId("work-a"), 2, changed_digest, 13, SQLITE_NOW),
                ),
            ),
        )
        after = read_plan_snapshot(self._exact_store(changed_state), (ItemId("work-a"),))

        changes = compare_plan_snapshots(before, after).changes
        self.assertEqual(("work-a-design",), tuple(value.key for value in changes.artifacts_changed[0].before))
        self.assertEqual((), changes.artifacts_changed[0].after)
        lifecycle = changes.lifecycle_only[0]
        self.assertEqual("ready", lifecycle.before_state)
        self.assertEqual("deferred", lifecycle.after_state)
        self.assertIsNone(lifecycle.after_outcome_evidence)

        resolved = state.planning.obligations[0]
        unresolved = replace(
            resolved,
            state=PlanningObligationState.UNRESOLVED,
            disposition=None,
            evaluated_scope_revision=None,
            evaluated_scope_digest=None,
            resulting_scope_revision=None,
            resulting_scope_digest=None,
            primary_replacement_item_id=None,
            outcome_evidence=None,
            reason=None,
            resolved_project_revision=None,
            resolved_at=None,
        )
        unresolved_state = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=6)),
            planning=replace(state.planning, obligations=(unresolved,), replacements=()),
        )
        resolved_state = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=7)),
        )
        obligation_changes = compare_plan_snapshots(
            read_plan_snapshot(self._store(unresolved_state), (ItemId("work-a"),)),
            read_plan_snapshot(self._store(resolved_state), (ItemId("work-a"),)),
        ).changes
        self.assertEqual("impact-a", obligation_changes.obligations_resolved[0].after.impact_id)
        self.assertEqual("added", obligation_changes.replacements[0].change)
        self.assertEqual("work-c", obligation_changes.replacements[0].replacements[0].item_id)


if __name__ == "__main__":
    unittest.main()
