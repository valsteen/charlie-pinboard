"""Behavior checks for the deferred handover projection prototype.

These tests intentionally exercise test-owned design evidence, not an installed
Pinboard interface. They should move with the prototype if a CLI feature is admitted.
"""

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.application.stored_state import (
    ItemScopeRevision,
    PlanningObligationState,
    StoredWorkItemState,
    StoredWorkState,
)
from charlie_pinboard.domain.history import ItemScopeRecord, item_scope_digest
from charlie_pinboard.domain.identifiers import (
    ItemId,
    PlanningImpactId,
)
from tests.prototypes.handover_projection import (
    PlanQueryError,
    PlanSnapshot,
    UndecidedProposal,
    compare_plan_snapshots,
    read_plan_snapshot,
)
from tests.support import SQLITE_NOW, complete_sqlite_state


class HandoverProjectionPrototypeTest(unittest.TestCase):
    def test_missing_plan_root_is_rejected(self) -> None:
        with self.assertRaises(PlanQueryError) as invalid_root:
            read_plan_snapshot(self._store(), (ItemId("missing"),))
        self.assertEqual("PLAN_SELECTION_INVALID", invalid_root.exception.code)

    def _rehash_snapshot(self, snapshot: PlanSnapshot) -> PlanSnapshot:

        payload = msgspec.json.decode(msgspec.json.encode(snapshot))

        if not isinstance(payload, dict):
            self.fail("Plan snapshot must encode as an object")

        payload.pop("manifest_sha256")

        digest = hashlib.sha256(msgspec.json.encode(payload, order="sorted") + b"\n").hexdigest()

        return msgspec.structs.replace(snapshot, manifest_sha256=digest)

    def _rehash_proposal(self, proposal: UndecidedProposal) -> UndecidedProposal:

        payload = msgspec.json.decode(msgspec.json.encode(proposal))

        if not isinstance(payload, dict):
            self.fail("Undecided proposal must encode as an object")

        payload.pop("proposal_sha256")

        digest = hashlib.sha256(msgspec.json.encode(payload, order="sorted") + b"\n").hexdigest()

        return msgspec.structs.replace(proposal, proposal_sha256=digest)

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

        empty_relation_proposal = self._rehash_proposal(
            msgspec.structs.replace(
                proposal,
                relation=msgspec.structs.replace(proposal.relation, item_id=""),
            )
        )

        duplicate_evidence_proposal = self._rehash_proposal(
            msgspec.structs.replace(
                proposal,
                evidence=(
                    proposal.evidence[0],
                    msgspec.structs.replace(proposal.evidence[0], position=1),
                ),
            )
        )

        duplicate_freshness_proposal = self._rehash_proposal(
            msgspec.structs.replace(
                proposal,
                freshness_assumptions=(
                    proposal.freshness_assumptions[0],
                    msgspec.structs.replace(proposal.freshness_assumptions[0], position=1),
                ),
            )
        )

        sibling_obligation = msgspec.structs.replace(
            resolved,
            target_item_id="work-a",
            target_position=1,
        )

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
            "nonterminal-item-outcome": msgspec.structs.replace(
                valid,
                items=(msgspec.structs.replace(first_item, outcome_evidence="unexpected"), *valid.items[1:]),
            ),
            "terminal-item-outcome": msgspec.structs.replace(
                valid,
                items=(
                    msgspec.structs.replace(
                        first_item,
                        lifecycle_state=StoredWorkItemState.DONE.value,
                        outcome_evidence=None,
                    ),
                    *valid.items[1:],
                ),
            ),
            "root-presence": msgspec.structs.replace(valid, items=valid.items[1:]),
            "dependency-presence": msgspec.structs.replace(valid, items=(first_item,)),
            "exact-closure": msgspec.structs.replace(valid, items=(extra_item, *valid.items[1:])),
            "obligation-order": msgspec.structs.replace(
                valid,
                resolved_obligations=(resolved, resolved),
            ),
            "obligation-shared-facts": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(sibling_obligation, summary="contradictory summary"),
                    resolved,
                ),
            ),
            "obligation-target-position": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(sibling_obligation, target_position=resolved.target_position),
                    resolved,
                ),
            ),
            "obligation-identity": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, target_position=-1),),
            ),
            "resolution-facts": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, replacements=()),),
            ),
            "resolution-disposition": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        disposition="invented",
                        outcome_evidence=None,
                        replacements=(),
                    ),
                ),
            ),
            "resolution-reason": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, reason=""),),
            ),
            "resolution-outcome": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, outcome_evidence=""),),
            ),
            "nonterminal-resolution-outcome": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        disposition="unchanged",
                        outcome_evidence="",
                        replacements=(),
                    ),
                ),
            ),
            "obligation-project-revisions": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        recorded_project_revision=0,
                        resolved_project_revision=0,
                    ),
                ),
            ),
            "obligation-anchor-identity": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        evaluated_scope=msgspec.structs.replace(
                            resolved.evaluated_scope,
                            scope_revision=resolved.target_scope.scope_revision,
                            scope_digest="b" * 64,
                        ),
                    ),
                ),
            ),
            "obligation-anchor-order": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        target_scope=msgspec.structs.replace(
                            resolved.target_scope,
                            scope_revision=resolved.evaluated_scope.scope_revision + 1,
                        ),
                    ),
                ),
            ),
            "resolution-replacement": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        replacements=(msgspec.structs.replace(resolved.replacements[0], item_id=""),),
                    ),
                ),
            ),
            "resolution-resulting-anchor": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        disposition="revised",
                        outcome_evidence=None,
                        replacements=(),
                        resulting_scope=resolved.evaluated_scope,
                    ),
                ),
            ),
            "obligation-scalar": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, summary=""),),
            ),
            "obligation-attempt": msgspec.structs.replace(
                valid,
                resolved_obligations=(msgspec.structs.replace(resolved, source_attempt_id=""),),
            ),
            "obligation-anchor-shape": msgspec.structs.replace(
                valid,
                resolved_obligations=(
                    msgspec.structs.replace(
                        resolved,
                        target_scope=msgspec.structs.replace(resolved.target_scope, scope_digest="not-a-sha"),
                    ),
                ),
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
            "undecided-relation": msgspec.structs.replace(valid, undecided=(empty_relation_proposal,)),
            "undecided-evidence": msgspec.structs.replace(valid, undecided=(duplicate_evidence_proposal,)),
            "undecided-freshness": msgspec.structs.replace(valid, undecided=(duplicate_freshness_proposal,)),
        }

        for inventory, snapshot in invalid_snapshots.items():
            with self.subTest(inventory=inventory):
                with self.assertRaises(PlanQueryError) as rejected:
                    malformed = self._rehash_snapshot(snapshot)

                    compare_plan_snapshots(malformed, malformed)

                self.assertEqual("PLAN_SNAPSHOT_INVALID", rejected.exception.code)

    def test_plan_obligation_phases_validate_separately_and_cannot_be_backdated(self) -> None:

        state = self._valid_scope_digests(complete_sqlite_state())

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

        later_impact_id = PlanningImpactId("z-impact")

        mixed_state = replace(
            state,
            planning=replace(
                state.planning,
                impacts=(*state.planning.impacts, replace(state.planning.impacts[0], impact_id=later_impact_id)),
                obligations=(resolved, replace(unresolved, impact_id=later_impact_id)),
            ),
        )

        mixed = read_plan_snapshot(self._store(mixed_state), (ItemId("work-a"),))

        self.assertEqual("z-impact", mixed.unresolved_obligations[0].impact_id)

        self.assertEqual("impact-a", mixed.resolved_obligations[0].impact_id)

        self.assertEqual((), compare_plan_snapshots(mixed, mixed).changes.obligations_resolved)

        before_state = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=7)),
            planning=replace(state.planning, obligations=(unresolved,), replacements=()),
        )

        after_state = replace(
            state,
            lifecycle=replace(state.lifecycle, project=replace(state.lifecycle.project, revision=8)),
        )

        with self.assertRaises(PlanQueryError) as backdated:
            compare_plan_snapshots(
                read_plan_snapshot(self._store(before_state), (ItemId("work-a"),)),
                read_plan_snapshot(self._store(after_state), (ItemId("work-a"),)),
            )

        self.assertEqual("PLAN_SNAPSHOT_CONTRADICTION", backdated.exception.code)

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
