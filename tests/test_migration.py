import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from charlie_pinboard.interfaces.transitions import TransitionError
from charlie_pinboard.legacy.actions import actions_for
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority
from charlie_pinboard.legacy.leases import acquire_coordination
from charlie_pinboard.legacy.markdown import parse_attempt, parse_item, parse_queue
from charlie_pinboard.legacy.migration import MigrationBoundary, MigrationError, MigrationWriteKind, migrate_to_v2
from charlie_pinboard.legacy.validate import validate_work_state

from .support import JsonObject, apply_action, create_proposal, create_state

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def proposal() -> JsonObject:
    return {
        "schema": "repo-work/v1",
        "proposal_id": "late-finding",
        "created_at": "2026-08-18T12:00:00Z",
        "source_task_id": "task",
        "user_label": "Late finding",
        "trigger": "Migration is crossing the authority boundary.",
        "evidence": ["observed concurrently"],
        "why_it_matters": "The acknowledged proposal must remain current.",
        "relation": {"kind": "independent", "item": None},
        "effect": "The proposal lands in the selected authority.",
        "unlock": "No intake is lost.",
        "urgency_evidence": "Concurrent cutover.",
        "freshness_assumptions": ["The cutover is still in progress."],
    }


def fail_at(selected: MigrationBoundary) -> Callable[[MigrationBoundary], None]:
    def fail(boundary: MigrationBoundary) -> None:
        if boundary == selected:
            raise RuntimeError("injected migration failure")

    return fail


class MigrationTest(unittest.TestCase):
    def test_v1_human_text_round_trips_through_v2_scalar_headers(self) -> None:
        for notes in ("Café.", 'Say "go".', r"Open C:\Audio\Take."):
            with self.subTest(notes=notes):
                project, work = create_state([f"| ready-item | ready | — | — | — | design | activate | {notes} |"])

                result = migrate_to_v2(work, project, now=NOW)

                root = resolve_authority(work).work_root
                self.assertTrue(result.cutover)
                self.assertEqual(notes, parse_queue(root / "queue.md").items[0].notes)
                queue_item = parse_item(root / "items" / "ready-item.md").queue_item
                self.assertIsNotNone(queue_item)
                if queue_item is None:
                    self.fail("Migrated v2 item has no generated queue fields.")
                self.assertEqual(notes, queue_item.notes)
                self.assertTrue(validate_work_state(work, project).valid)

    def test_v1_reserved_scalar_tokens_remain_strings_through_migration(self) -> None:
        project, work = create_state(
            [
                "| true | ready | true | — | — | true | true | True token. |",
                "| false | ready | false | — | — | false | false | False token. |",
                "| null | ready | ~ | — | — | null | ~ | Null token. |",
                "| blocked-item | blocked | ~ | true, false, null | — | ~ | false | Wait. |",
                "| active-item | active | — | — | true | null | ~ | Active. |",
            ],
            focus_item="active-item",
            focus_attempt="true",
            create_active_attempt=True,
        )
        for identity in ("true", "false", "null"):
            path = work / "items" / f"{identity}.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(f"item: {identity}\n", f"item: '{identity}'\n"),
                encoding="utf-8",
            )
        current = work / "current.md"
        current.write_text(
            current.read_text(encoding="utf-8").replace("focus_attempt: true\n", "focus_attempt: 'true'\n"),
            encoding="utf-8",
        )
        attempt = work / "attempts" / "true" / "attempt.md"
        attempt.write_text(
            attempt.read_text(encoding="utf-8")
            .replace("attempt: true\n", "attempt: 'true'\n")
            .replace("owner: worker-task\n", "owner: 'true'\n"),
            encoding="utf-8",
        )
        before = parse_queue(work / "queue.md")
        self.assertTrue(validate_work_state(work, project).valid)

        result = migrate_to_v2(work, project, now=NOW)

        root = resolve_authority(work).work_root
        self.assertTrue(result.cutover)
        for expected in before.items:
            record = parse_item(root / "items" / f"{expected.item}.md")
            self.assertEqual(expected, record.queue_item)
        migrated_attempt = parse_attempt(root / "attempts" / "true" / "attempt.md")
        self.assertEqual("true", migrated_attempt.provenance)
        self.assertTrue(validate_work_state(work, project).valid)

    def test_v1_empty_source_and_notes_remain_present_and_empty_through_migration(self) -> None:
        project, work = create_state(["| empty-fields | ready | — | — | — |  | activate |  |"])
        before = parse_queue(work / "queue.md").items[0]
        self.assertEqual(("", ""), (before.source, before.notes))
        self.assertTrue(validate_work_state(work, project).valid)

        result = migrate_to_v2(work, project, now=NOW)

        root = resolve_authority(work).work_root
        migrated = parse_item(root / "items" / "empty-fields.md").queue_item
        self.assertTrue(result.cutover)
        self.assertIsNotNone(migrated)
        if migrated is None:
            self.fail("Migrated schema-v2 item has no queue fields.")
        self.assertEqual(("", ""), (migrated.source, migrated.notes))
        self.assertTrue(validate_work_state(work, project).valid)

    def test_every_migration_write_rename_and_selector_boundary_is_recoverable(self) -> None:
        def state() -> tuple[Path, Path]:
            project, work = create_state(
                ["| active-item | active | — | — | active-item-1 | design | continue | Active. |"],
                focus_item="active-item",
                focus_attempt="active-item-1",
                create_active_attempt=True,
            )
            attempt = work / "attempts" / "active-item-1"
            (attempt / "result.md").write_text("result\n", encoding="utf-8")
            (attempt / "review.md").write_text("review\n", encoding="utf-8")
            create_proposal(work, project, proposal())
            return project, work

        project, work = state()
        observed: list[MigrationBoundary] = []
        migrate_to_v2(work, project, now=NOW, failpoint=observed.append)
        self.assertIn(MigrationBoundary(MigrationWriteKind.SHADOW_RENAME, "v2"), observed)
        self.assertIn(MigrationBoundary(MigrationWriteKind.SELECTOR_WRITE, "authority.json"), observed)

        for selected in observed:
            with self.subTest(kind=selected.kind.value, path=selected.path):
                project, work = state()
                with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                    migrate_to_v2(work, project, now=NOW, failpoint=fail_at(selected))
                authority = resolve_authority(work)
                expected = (
                    AuthorityVersion.V2 if selected.kind == MigrationWriteKind.SELECTOR_WRITE else AuthorityVersion.V1
                )
                self.assertEqual(expected, authority.version)
                self.assertTrue(validate_work_state(work, project).valid)
                migrate_to_v2(work, project, now=NOW)
                self.assertEqual(AuthorityVersion.V2, resolve_authority(work).version)
                self.assertTrue(validate_work_state(work, project).valid)

    def test_cutover_serializes_transition_proposal_and_lease_writers_on_the_base_root(self) -> None:
        project, work = create_state(["| ready-item | ready | — | — | — | design | activate | Ready. |"])
        action = next(
            candidate
            for candidate in actions_for(work, project, "coordinator")
            if candidate.action_id == "activate:ready-item"
        )
        migration_paused = Event()
        continue_migration = Event()

        def pause_after_shadow(boundary: MigrationBoundary) -> None:
            if boundary == MigrationBoundary(MigrationWriteKind.SHADOW_WRITE, "migration-complete.md"):
                migration_paused.set()
                continue_migration.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=4) as executor:
            migration = executor.submit(migrate_to_v2, work, project, now=NOW, failpoint=pause_after_shadow)
            self.assertTrue(migration_paused.wait(timeout=5))
            transition = executor.submit(
                apply_action,
                work,
                project,
                action,
                {
                    "attempt": "ready-item-1",
                    "branch": "codex/ready-item",
                    "base_revision": "abc123",
                    "owner": "worker",
                },
            )
            intake = executor.submit(create_proposal, work, project, proposal())
            lease = executor.submit(acquire_coordination, work, "task", "host", 60, now=NOW)
            continue_migration.set()

            self.assertTrue(migration.result(timeout=5).cutover)
            with self.assertRaises(TransitionError):
                transition.result(timeout=5)
            proposal_path = intake.result(timeout=5)
            lease_record = lease.result(timeout=5)

        authority = resolve_authority(work)
        self.assertEqual(work / "v2" / "inbox" / "late-finding.json", proposal_path)
        self.assertTrue((authority.work_root / "leases" / "coordination.md").is_file())
        self.assertFalse((work / "leases" / "coordination.md").exists())
        self.assertEqual("task", lease_record.task_id)
        self.assertEqual("ready", parse_queue(work / "queue.md").by_id()["ready-item"].state.value)
        self.assertEqual("ready", parse_queue(authority.work_root / "queue.md").by_id()["ready-item"].state.value)

    def test_v1_inventory_migrates_once_with_equivalent_live_state(self) -> None:
        project, work = create_state(
            [
                "| active-item | active | — | — | active-item-1 | design | continue | Active. |",
                "| blocked-item | blocked | — | active-item | — | finding | none | Wait. |",
                "| deferred-item | deferred | safe-to-defer | — | — | finding | none | Later. |",
            ],
            focus_item="active-item",
            focus_attempt="active-item-1",
            create_active_attempt=True,
        )
        before = parse_queue(work / "queue.md")
        attempt_directory = work / "attempts" / "active-item-1"
        (attempt_directory / "result.md").write_text("result evidence\n", encoding="utf-8")
        (attempt_directory / "blocker.md").write_text("blocker evidence\n", encoding="utf-8")
        (attempt_directory / "review").mkdir()
        (attempt_directory / "review" / "verdict.md").write_text("review evidence\n", encoding="utf-8")

        result = migrate_to_v2(work, project, now=NOW)

        authority = resolve_authority(work)
        self.assertEqual(AuthorityVersion.V2, authority.version)
        self.assertEqual(
            [(item.item, item.state, item.depends_on, item.attempt) for item in before.items],
            [
                (item.item, item.state, item.depends_on, item.attempt)
                for item in parse_queue(authority.work_root / "queue.md").items
            ],
        )
        self.assertTrue(validate_work_state(work, project).valid)
        self.assertEqual(3, result.live_items)
        self.assertTrue(result.cutover)
        for relative in ("result.md", "blocker.md", "review/verdict.md"):
            self.assertEqual(
                (attempt_directory / relative).read_bytes(),
                (authority.work_root / "attempts" / "active-item-1" / relative).read_bytes(),
            )
        migrated_attempt = authority.work_root / "attempts" / "active-item-1" / "attempt.md"
        self.assertNotIn("\nowner:", migrated_attempt.read_text(encoding="utf-8"))
        self.assertIn('\nprovenance: "worker-task"', migrated_attempt.read_text(encoding="utf-8"))

        repeated = migrate_to_v2(work, project, now=NOW)
        self.assertFalse(repeated.cutover)

    def test_failure_before_cutover_leaves_v1_current_and_retryable(self) -> None:
        for selected_boundary in (
            MigrationBoundary(MigrationWriteKind.SHADOW_WRITE, "items/ready-item.md"),
            MigrationBoundary(MigrationWriteKind.SHADOW_RENAME, "v2"),
        ):
            with self.subTest(boundary=selected_boundary):
                project, work = create_state(["| ready-item | ready | — | — | — | design | activate | Ready. |"])

                with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                    migrate_to_v2(work, project, now=NOW, failpoint=fail_at(selected_boundary))

                self.assertEqual(AuthorityVersion.V1, resolve_authority(work).version)
                self.assertTrue(validate_work_state(work, project).valid)

                result = migrate_to_v2(work, project, now=NOW)
                self.assertTrue(result.cutover)
                self.assertEqual(AuthorityVersion.V2, resolve_authority(work).version)

    def test_retry_after_shadow_rename_preserves_valid_v1_work_written_between_attempts(self) -> None:
        project, work = create_state(["| ready-item | ready | — | — | — | design | activate | Ready. |"])
        with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            migrate_to_v2(
                work,
                project,
                now=NOW,
                failpoint=fail_at(MigrationBoundary(MigrationWriteKind.SHADOW_RENAME, "v2")),
            )
        self.assertEqual(AuthorityVersion.V1, resolve_authority(work).version)

        proposal_path = create_proposal(work, project, proposal())
        self.assertEqual(work / "inbox" / "late-finding.json", proposal_path)

        result = migrate_to_v2(work, project, now=NOW)

        authority = resolve_authority(work)
        self.assertTrue(result.cutover)
        self.assertEqual(AuthorityVersion.V2, authority.version)
        self.assertEqual(1, result.proposals)
        self.assertTrue((authority.work_root / "inbox" / "late-finding.json").is_file())
        self.assertTrue(validate_work_state(work, project).valid)

    def test_incomplete_shadow_never_cuts_over(self) -> None:
        project, work = create_state(["| ready-item | ready | — | — | — | design | activate | Ready. |"])
        (work / "v2").mkdir()
        (work / "v2" / "broken").write_text("broken", encoding="utf-8")

        with self.assertRaisesRegex(MigrationError, "MIGRATION_INCOMPLETE"):
            migrate_to_v2(work, project, now=NOW)

        self.assertEqual(AuthorityVersion.V1, resolve_authority(work).version)

    def test_failure_after_selector_flip_leaves_complete_v2_current(self) -> None:
        project, work = create_state(["| ready-item | ready | — | — | — | design | activate | Ready. |"])

        with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            migrate_to_v2(
                work,
                project,
                now=NOW,
                failpoint=fail_at(MigrationBoundary(MigrationWriteKind.SELECTOR_WRITE, "authority.json")),
            )

        self.assertEqual(AuthorityVersion.V2, resolve_authority(work).version)
        self.assertTrue(validate_work_state(work, project).valid)
        self.assertFalse(migrate_to_v2(work, project, now=NOW).cutover)


if __name__ == "__main__":
    unittest.main()
