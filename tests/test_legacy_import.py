import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.stored_state import TransitionHistoryActionKind
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.legacy.legacy_import import LegacyImportError, dry_run_ledger, import_ledger

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode() if isinstance(value, str) else value)


def _fixture() -> tuple[Path, Path]:
    project = Path(tempfile.mkdtemp()).resolve()
    work = project / ".codex" / "work"
    selected = work / "v2"
    selected.mkdir(parents=True)
    _write(
        work / "authority.json",
        json.dumps({"schema": "repo-work-authority/v1", "current": "v2", "root": "v2"}, indent=2) + "\n",
    )
    _write(work / "queue.md", "inactive v1 queue\n")
    _write(
        selected / "items" / "work-a.md",
        """---
kind: "work-item"
schema: "repo-work/v2"
item: "work-a"
user_label: "Work A"
updated: "2026-08-20"
state: "active"
timing: "must-now"
depends_on: "work-b"
attempt: "work-a-1"
source: "proposal:work-a"
next_action: "continue"
notes: "bounded"
resources: —
---

Requirements body.\n""",
    )
    _write(
        selected / "history" / "items" / "work-b.md",
        """---
kind: "work-history"
schema: "repo-work/v2"
item: "work-b"
user_label: "Work B"
updated: "2026-08-19"
state: "done"
timing: "safe-to-defer"
depends_on: "—"
attempt: "work-b-1"
source: "proposal:work-b"
next_action: "review"
notes: "finished"
resources: "—"
evidence: "accepted evidence"
---

Terminal body.\n""",
    )
    for item_id, state in (("work-c", "superseded"), ("work-d", "dropped")):
        _write(
            selected / "history" / "items" / f"{item_id}.md",
            f'''---
kind: "work-history"
schema: "repo-work/v2"
item: "{item_id}"
user_label: "{item_id.upper()}"
updated: "2026-08-18"
state: "{state}"
timing: "safe-to-defer"
depends_on: "—"
attempt: "—"
source: "—"
next_action: "—"
notes: "terminal"
resources: "—"
evidence: "{state} evidence"
---

{state} body.
''',
        )
    _write(
        selected / "attempts" / "work-a-1" / "attempt.md",
        """---
kind: "work-attempt"
schema: "repo-work/v2"
attempt: "work-a-1"
item: "work-a"
state: "active"
branch: "codex/work-a"
base_revision: "abc123"
provenance: "source-task"
owner_task_id: "task-a"
owner_host_id: "host-a"
lease_id: "lease-a"
lease_generation: 2
lease_acquired_at: "2026-08-24T08:00:00Z"
lease_expires_at: "2026-08-24T10:00:00Z"
lease_status: "active"
updated: "2026-08-24"
---

Brief bytes.\n""",
    )
    _write(
        selected / "attempts" / "work-b-1" / "attempt.md",
        """---
kind: "work-attempt"
schema: "repo-work/v2"
attempt: "work-b-1"
item: "work-b"
state: "done"
branch: "codex/work-b"
base_revision: "def456"
provenance: "source-task"
owner_task_id: "unclaimed"
owner_host_id: "unclaimed"
lease_id: "unclaimed"
lease_generation: 0
lease_acquired_at: "2026-08-20T00:00:00Z"
lease_expires_at: "2026-08-20T00:00:00Z"
lease_status: "released"
updated: "2026-08-20"
---
""",
    )
    _write(selected / "attempts" / "work-a-1" / "result.md", b"result bytes\r\n")
    _write(selected / "attempts" / "work-a-1" / "review.md", b"review bytes\n")
    _write(selected / "attempts" / "work-a-1" / "checkpoints" / "proof.txt", b"proof bytes\n")
    _write(
        selected / "inbox" / "later.json",
        json.dumps(
            {
                "schema": "repo-work/v1",
                "proposal_id": "later",
                "created_at": "2026-08-21T12:00:00Z",
                "source_task_id": "task-later",
                "user_label": "Later",
                "trigger": "Observed",
                "evidence": ["evidence-a"],
                "why_it_matters": "It matters",
                "relation": {"kind": "follow-up", "item": "work-a"},
                "effect": "Preserve it",
                "unlock": "Review later",
                "urgency_evidence": "None",
                "freshness_assumptions": ["still true"],
            },
            indent=2,
        )
        + "\n",
    )
    _write(
        selected / "leases" / "coordination.md",
        """---
kind: "coordination-lease"
schema: "repo-work/v2"
owner_task_id: "coordinator-task"
owner_host_id: "host-a"
lease_id: "coordination-a"
lease_generation: 3
lease_acquired_at: "2026-08-24T07:00:00Z"
lease_expires_at: "2026-08-24T08:00:00Z"
lease_status: "released"
---

# Coordination Lease
""",
    )
    (selected / "resources").mkdir()
    (selected / "leases" / "resources").mkdir()
    _write(
        selected / "queue.md",
        """---
kind: "work-queue"
schema: "repo-work/v2"
updated: "2026-08-24"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| work-a | active | must-now | work-b | work-a-1 | proposal:work-a | continue | bounded |
""",
    )
    _write(
        selected / "current.md",
        """---
kind: "work-current"
schema: "repo-work/v2"
updated: "2026-08-24"
focus_item: "work-a"
focus_attempt: "work-a-1"
next_action: "continue"
---

# Current Work
""",
    )
    _write(
        selected / "migration-complete.md",
        """---
kind: "migration-complete"
schema: "repo-work/v2"
---

# Migration Complete
""",
    )
    return project, work


class LegacyImportTest(unittest.TestCase):
    def test_import_maps_typed_state_artifacts_and_revision_one_history(self) -> None:
        project, work = _fixture()
        staged = Path(tempfile.mkdtemp()).resolve()
        destination = staged / "state.sqlite3"

        receipt = import_ledger(project, work, destination, NOW)
        state = SQLiteWorkStore(destination).snapshot()

        self.assertEqual(1, receipt.destination_revision)
        self.assertEqual(4, receipt.counts.items)
        self.assertEqual((0, 0, 0), (receipt.counts.resources, receipt.counts.item_resources, receipt.counts.claims))
        self.assertEqual(
            {"work-a", "work-b", "work-c", "work-d"},
            {str(item.item_id) for item in state.lifecycle.work_items},
        )
        self.assertEqual(4, len(state.lifecycle.scope_revisions))
        self.assertEqual(2, len(state.lifecycle.attempts))
        self.assertEqual(1, len(state.proposals.proposals))
        self.assertEqual(("evidence-a",), tuple(value.selector for value in state.proposals.evidence))
        self.assertEqual(("still true",), tuple(value.assumption for value in state.proposals.freshness))
        self.assertEqual((), state.planning.impacts)
        self.assertEqual((), state.resources.definitions)
        self.assertEqual((2, 0), tuple(value.generation_high_water for value in state.authority.attempt_counters))
        self.assertEqual(1, len(state.authority.attempt_generations))
        self.assertEqual(1, len(state.authority.attempt_leases))
        self.assertIsNotNone(state.authority.coordination)
        self.assertEqual(1, len(state.history.receipts))
        self.assertEqual(TransitionHistoryActionKind.LEGACY_IMPORT, state.history.receipts[0].action_kind)
        self.assertEqual(
            receipt.manifest_selector,
            next(ref.selector for ref in state.artifacts.references if ref.kind.value == "evidence"),
        )
        brief = next(ref for ref in state.artifacts.references if ref.kind.value == "brief" and ref.key == "work-a-1")
        self.assertEqual(b"Brief bytes.\n", (staged / brief.selector).read_bytes())
        zero_brief = next(
            ref for ref in state.artifacts.references if ref.kind.value == "brief" and ref.key == "work-b-1"
        )
        self.assertEqual(b"", (staged / zero_brief.selector).read_bytes())
        result = next(ref for ref in state.artifacts.references if ref.kind.value == "result")
        self.assertEqual(b"result bytes\r\n", (staged / result.selector).read_bytes())
        evidence = tuple(ref for ref in state.artifacts.references if ref.kind.value == "evidence")
        self.assertEqual(
            {b"review bytes\n", b"proof bytes\n"},
            {(staged / ref.selector).read_bytes() for ref in evidence if ref.selector != receipt.manifest_selector},
        )
        terminal = next(item for item in state.lifecycle.work_items if item.item_id == "work-b")
        self.assertEqual("accepted evidence", terminal.outcome_evidence)
        self.assertEqual(
            {"done", "superseded", "dropped"},
            {item.state.value for item in state.lifecycle.work_items if item.item_id in {"work-b", "work-c", "work-d"}},
        )
        outcome = json.loads(bytes(state.history.receipts[0].outcome_payload))
        self.assertEqual("repo-work/v2", outcome["source_schema"])
        self.assertEqual("0.1.0", outcome["importer_version"])
        manifest = json.loads((staged / receipt.manifest_selector).read_bytes())
        self.assertEqual("repo-work/v2", manifest["source_schema"])
        self.assertEqual(receipt.source_revision, manifest["source_revision"])
        self.assertTrue((staged / "views" / "queue.md").is_file())
        self.assertFalse((work / "state.sqlite3").exists())

    def test_resource_state_is_rejected_before_destination_creation(self) -> None:
        for resource_state in ("resource-file", "live-item-resource-header", "terminal-item-resource-header"):
            with self.subTest(resource_state=resource_state):
                project, work = _fixture()
                if resource_state == "resource-file":
                    _write(
                        work / "v2" / "resources" / "workspace.md",
                        """---
kind: "work-resource"
schema: "repo-work/v2"
resource: "workspace"
label: "Workspace"
scope: "host-local"
mode: "exclusive"
---
""",
                    )
                elif resource_state == "live-item-resource-header":
                    item = work / "v2" / "items" / "work-a.md"
                    item.write_text(item.read_text().replace("resources: —", "resources: workspace"))
                else:
                    item = work / "v2" / "history" / "items" / "work-b.md"
                    item.write_text(item.read_text().replace('resources: "—"', 'resources: "workspace"'))
                destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

                with self.assertRaises(LegacyImportError) as raised:
                    import_ledger(project, work, destination, NOW)

                self.assertEqual("LEGACY_RESOURCE_STATE_UNSUPPORTED", raised.exception.code)
                self.assertFalse(destination.exists())

    def test_proposal_item_targets_reject_before_destination_creation(self) -> None:
        for target_kind in ("relation", "disposition"):
            with self.subTest(target_kind=target_kind):
                project, work = _fixture()
                selected = work / "v2"
                inbox = selected / "inbox" / "later.json"
                value = json.loads(inbox.read_bytes())
                if target_kind == "relation":
                    value["relation"]["item"] = "missing-item"
                    _write(inbox, json.dumps(value, indent=2) + "\n")
                else:
                    value.update({"disposition": "accepted", "target": "missing-item"})
                    _write(selected / "history" / "proposals" / "later.json", json.dumps(value, indent=2) + "\n")
                    inbox.unlink()
                destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

                with self.assertRaises(LegacyImportError) as raised:
                    import_ledger(project, work, destination, NOW)

                self.assertEqual("LEGACY_SOURCE_INVALID", raised.exception.code)
                self.assertFalse(destination.exists())

    def test_terminal_item_accepts_its_completed_attempt(self) -> None:
        project, work = _fixture()
        selected = work / "v2"
        item = selected / "history" / "items" / "work-d.md"
        item.write_text(item.read_text().replace('attempt: "—"', 'attempt: "work-d-1"'))
        source = selected / "attempts" / "work-b-1" / "attempt.md"
        attempt = source.read_text().replace('attempt: "work-b-1"', 'attempt: "work-d-1"')
        attempt = attempt.replace('item: "work-b"', 'item: "work-d"')
        _write(selected / "attempts" / "work-d-1" / "attempt.md", attempt)

        receipt = dry_run_ledger(project, work, NOW)

        self.assertEqual(3, receipt.counts.attempts)

    def test_temporary_cli_exposes_dry_run_without_source_mutation(self) -> None:
        project, work = _fixture()
        before = (work / "authority.json").read_bytes()
        output = StringIO()

        with redirect_stdout(output):
            result = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "legacy-import",
                    "dry-run",
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        value = json.loads(output.getvalue())
        self.assertEqual(0, value["counts"]["resources"])
        self.assertEqual(64, len(value["cutover_id"]))
        self.assertEqual(before, (work / "authority.json").read_bytes())

    def test_staged_database_is_not_ordinary_authority_before_tombstone(self) -> None:
        project, work = _fixture()
        import_ledger(project, work, work / "state.sqlite3", NOW)
        error = StringIO()

        with redirect_stderr(error):
            result = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "overview",
                    "--json",
                ]
            )

        self.assertEqual(12, result)
        self.assertIn("CUTOVER_NOT_ACTIVE", error.getvalue())

    def test_flat_disposed_proposal_and_missing_coordination_are_preserved(self) -> None:
        project, work = _fixture()
        inbox = work / "v2" / "inbox" / "later.json"
        value = json.loads(inbox.read_bytes())
        value.update({"disposition": "accepted", "target": "work-a"})
        history = work / "v2" / "history" / "proposals" / "later.json"
        _write(history, json.dumps(value, indent=2) + "\n")
        inbox.unlink()
        (work / "v2" / "leases" / "coordination.md").unlink()
        destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

        import_ledger(project, work, destination, NOW)
        state = SQLiteWorkStore(destination).snapshot()

        proposal = state.proposals.proposals[0]
        self.assertEqual("accepted", proposal.disposition.value if proposal.disposition is not None else None)
        self.assertEqual("work-a", proposal.disposition_target_item_id)
        self.assertIsNone(proposal.origin_disposed_at)
        self.assertEqual(NOW, proposal.disposition_recorded_at)
        self.assertIsNone(state.authority.coordination)

    def test_wrapped_proposal_history_is_preserved(self) -> None:
        project, work = _fixture()
        inbox = work / "v2" / "inbox" / "later.json"
        proposal = json.loads(inbox.read_bytes())
        history = work / "v2" / "history" / "proposals" / "later.json"
        _write(
            history,
            json.dumps(
                {
                    "proposal": proposal,
                    "disposition": "accepted",
                    "target": "work-a",
                    "coordinator_reason": "accepted reason",
                },
                indent=2,
            )
            + "\n",
        )
        inbox.unlink()
        destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

        import_ledger(project, work, destination, NOW)
        stored = SQLiteWorkStore(destination).snapshot().proposals.proposals[0]

        self.assertEqual("accepted", stored.disposition.value if stored.disposition is not None else None)
        self.assertEqual("work-a", stored.disposition_target_item_id)
        self.assertEqual("accepted reason", stored.disposition_reason)

    def test_blocker_artifact_is_preserved_without_inventing_a_result(self) -> None:
        project, work = _fixture()
        attempt = work / "v2" / "attempts" / "work-a-1"
        (attempt / "result.md").replace(attempt / "blocker.md")
        destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

        import_ledger(project, work, destination, NOW)
        state = SQLiteWorkStore(destination).snapshot()
        stored = next(value for value in state.lifecycle.attempts if value.attempt_id == "work-a-1")

        self.assertIsNone(stored.result_artifact_ref_id)
        self.assertIsNotNone(stored.blocker_artifact_ref_id)
        blocker = next(ref for ref in state.artifacts.references if ref.kind.value == "blocker")
        self.assertEqual(b"result bytes\r\n", (destination.parent / blocker.selector).read_bytes())

    def test_artifact_target_collision_rejects_before_destination_creation(self) -> None:
        project, work = _fixture()
        attempt = work / "v2" / "attempts" / "work-a-1"
        _write(attempt / "a-b.md", b"first")
        _write(attempt / "a" / "b.md", b"second")
        destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"

        with self.assertRaises(LegacyImportError) as raised:
            import_ledger(project, work, destination, NOW)

        self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)
        self.assertFalse(destination.exists())

    def test_unclassified_source_and_invalid_generation_zero_fail_closed(self) -> None:
        for mutation in (
            "unexpected-root",
            "unknown-nested-selector",
            "orphan-attempt-evidence",
            "partial-sentinel",
        ):
            with self.subTest(mutation=mutation):
                project, work = _fixture()
                if mutation == "unexpected-root":
                    _write(work / "unexpected.txt", "unexpected")
                elif mutation == "unknown-nested-selector":
                    _write(work / "v2" / "items" / "unclassified.bin", "unexpected")
                elif mutation == "orphan-attempt-evidence":
                    _write(work / "v2" / "attempts" / "orphan" / "proof.txt", "unexpected")
                else:
                    attempt = work / "v2" / "attempts" / "work-b-1" / "attempt.md"
                    attempt.write_text(
                        attempt.read_text().replace('owner_task_id: "unclaimed"', 'owner_task_id: "task"')
                    )

                with self.assertRaises(LegacyImportError):
                    dry_run_ledger(project, work, NOW)

    def test_supported_document_contract_rejections_are_typed(self) -> None:
        cases = (
            "naive-import-time",
            "invalid-migration-marker",
            "live-item-queue-contradiction",
            "missing-live-item",
            "nonterminal-history-item",
            "overlapping-item-identity",
        )
        for case in cases:
            with self.subTest(case=case):
                project, work = _fixture()
                selected = work / "v2"
                now = NOW
                if case == "naive-import-time":
                    now = NOW.replace(tzinfo=None)
                elif case == "invalid-migration-marker":
                    marker = selected / "migration-complete.md"
                    marker.write_text(marker.read_text().replace('kind: "migration-complete"', 'kind: "other"'))
                elif case == "live-item-queue-contradiction":
                    item = selected / "items" / "work-a.md"
                    item.write_text(item.read_text().replace('notes: "bounded"', 'notes: "changed"'))
                elif case == "missing-live-item":
                    (selected / "items" / "work-a.md").unlink()
                elif case == "nonterminal-history-item":
                    item = selected / "history" / "items" / "work-b.md"
                    item.write_text(item.read_text().replace('state: "done"', 'state: "active"'))
                elif case == "overlapping-item-identity":
                    item = selected / "history" / "items" / "work-b.md"
                    item.write_text(item.read_text().replace('item: "work-b"', 'item: "work-a"'))

                with self.assertRaises(LegacyImportError) as raised:
                    dry_run_ledger(project, work, now)

                self.assertEqual("LEGACY_SOURCE_INVALID", raised.exception.code)

    def test_supported_graph_and_proposal_rejections_are_typed(self) -> None:
        cases = (
            "missing-dependency",
            "invalid-proposal",
            "duplicate-proposal",
            "invalid-updated-time",
            "outside-project-root",
            "mismatched-current-attempt",
            "mismatched-terminal-attempt",
            "mismatched-terminal-attempt-state",
        )
        for case in cases:
            with self.subTest(case=case):
                project, work = _fixture()
                selected = work / "v2"
                passed_project = project
                if case == "missing-dependency":
                    item = selected / "items" / "work-a.md"
                    item.write_text(item.read_text().replace('depends_on: "work-b"', 'depends_on: "missing"'))
                    queue = selected / "queue.md"
                    queue.write_text(queue.read_text().replace("| work-b | work-a-1 |", "| missing | work-a-1 |"))
                elif case == "invalid-proposal":
                    (selected / "inbox" / "later.json").write_bytes(b"not json")
                elif case == "duplicate-proposal":
                    source = selected / "inbox" / "later.json"
                    (selected / "inbox" / "duplicate.json").write_bytes(source.read_bytes())
                elif case == "invalid-updated-time":
                    attempt = selected / "attempts" / "work-a-1" / "attempt.md"
                    attempt.write_text(attempt.read_text().replace('updated: "2026-08-24"', 'updated: "2026-13-40"'))
                elif case == "outside-project-root":
                    passed_project = project / "nested"
                elif case == "mismatched-current-attempt":
                    item = selected / "items" / "work-a.md"
                    item.write_text(item.read_text().replace('attempt: "work-a-1"', 'attempt: "work-b-1"'))
                    queue = selected / "queue.md"
                    queue.write_text(queue.read_text().replace("| work-b | work-a-1 |", "| work-b | work-b-1 |"))
                elif case == "mismatched-terminal-attempt":
                    item = selected / "history" / "items" / "work-b.md"
                    item.write_text(item.read_text().replace('attempt: "work-b-1"', 'attempt: "work-a-1"'))
                elif case == "mismatched-terminal-attempt-state":
                    attempt = selected / "attempts" / "work-b-1" / "attempt.md"
                    attempt.write_text(attempt.read_text().replace('state: "done"', 'state: "closed"'))

                with self.assertRaises(LegacyImportError) as raised:
                    dry_run_ledger(passed_project, work, NOW)

                self.assertEqual("LEGACY_SOURCE_INVALID", raised.exception.code)

    def test_import_destination_must_be_absent_and_canonical(self) -> None:
        project, work = _fixture()
        parent = Path(tempfile.mkdtemp()).resolve()
        existing = parent / "state.sqlite3"
        existing.write_bytes(b"existing")

        for destination in (existing, parent / "other.sqlite3"):
            with self.subTest(destination=destination.name):
                with self.assertRaises(LegacyImportError) as raised:
                    import_ledger(project, work, destination, NOW)

                self.assertEqual("STORAGE_INVARIANT_VIOLATION", raised.exception.code)

    def test_temporary_cli_stage_and_plain_text_paths_are_explicit(self) -> None:
        project, work = _fixture()
        destination = Path(tempfile.mkdtemp()).resolve() / "state.sqlite3"
        output = StringIO()

        with redirect_stdout(output):
            staged = main(
                [
                    "--project-root",
                    str(project),
                    "--work-root",
                    str(work),
                    "legacy-import",
                    "stage",
                    "--destination",
                    str(destination),
                ]
            )

        self.assertEqual(0, staged)
        self.assertIn("OK LEGACY_IMPORT", output.getvalue())
        self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
