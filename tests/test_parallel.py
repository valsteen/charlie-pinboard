import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import patch

from repo_work.actions import actions_for, state_revision
from repo_work.atomic import transition_lock
from repo_work.leases import acquire_attempt, acquire_coordination
from repo_work.migration import migrate_to_v2
from repo_work.parallel import ParallelError, ParallelOutcome, preview_parallel
from repo_work.resources import claim_resource, declare_resource, release_resource
from repo_work.transaction_store import FileChange

from .support import apply_action, create_state

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


class ParallelPreviewTest(unittest.TestCase):
    def snapshot(self, work: Path) -> dict[str, bytes]:
        return {str(path.relative_to(work)): path.read_bytes() for path in work.rglob("*") if path.is_file()}

    def mixed_state(self) -> tuple[Path, Path]:
        project, work = create_state(
            [
                "| alpha | ready | — | — | — | design | activate | Ready. |",
                "| beta | ready | — | — | — | design | activate | Ready. |",
                "| foundation | intake | — | — | — | finding | classify | Intake. |",
                "| dependent | ready | — | foundation | — | design | activate | Waiting. |",
                "| mixer-a | ready | — | — | — | design | activate | Ready. |",
                "| mixer-b | ready | — | — | — | design | activate | Ready. |",
            ]
        )
        migrate_to_v2(work, project, now=NOW)
        root = work / "v2"
        coordination = acquire_coordination(work, "coordinator", "studio", 120, now=NOW)
        declare_resource(
            work,
            "shared-console",
            "Shared console",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        for item_id in ("mixer-a", "mixer-b"):
            path = root / "items" / f"{item_id}.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace("resources: —", "resources: shared-console"),
                encoding="utf-8",
            )
        return project, work

    def test_default_preview_is_read_only_and_explains_safe_choice_and_exclusions(self) -> None:
        project, work = self.mixed_state()
        before = self.snapshot(work)

        result = preview_parallel(work, project, "studio", now=NOW)

        self.assertEqual("repo-work-parallel-preview/v1", result.schema)
        self.assertEqual("all-safe", result.selection.value)
        self.assertTrue(result.safe)
        self.assertEqual(("alpha", "beta"), tuple(item.item_id for item in result.launchable))
        self.assertEqual(("mixer-a", "mixer-b"), tuple(item.item_id for item in result.requires_selection))
        self.assertEqual(
            {"mixer-a": ("resource-selection-required",), "mixer-b": ("resource-selection-required",)},
            {item.item_id: tuple(reason.code.value for reason in item.reasons) for item in result.requires_selection},
        )
        self.assertEqual(
            {"dependent": ("dependency-live",), "foundation": ("state-not-launchable",)},
            {item.item_id: tuple(reason.code.value for reason in item.reasons) for item in result.excluded},
        )
        self.assertEqual(before, self.snapshot(work))

    def test_exact_selection_validates_singleton_and_rejects_pairwise_resource_conflict(self) -> None:
        project, work = self.mixed_state()

        singleton = preview_parallel(work, project, "studio", selected=("mixer-a",), now=NOW)
        conflict = preview_parallel(work, project, "studio", selected=("mixer-a", "mixer-b"), now=NOW)

        self.assertEqual("selected", singleton.selection.value)
        self.assertTrue(singleton.safe)
        self.assertEqual(("mixer-a",), tuple(item.item_id for item in singleton.launchable))
        self.assertFalse(conflict.safe)
        self.assertEqual((), conflict.launchable)
        self.assertEqual(
            {"mixer-a": ("resource-conflict",), "mixer-b": ("resource-conflict",)},
            {item.item_id: tuple(reason.code.value for reason in item.reasons) for item in conflict.excluded},
        )

    def test_live_attempt_owner_and_busy_host_resource_are_not_launchable(self) -> None:
        project, work = create_state(
            [
                "| owner | active | — | — | owner-1 | design | continue | Active. |",
                "| waiting | ready | — | — | — | design | activate | Ready. |",
            ],
            focus_item="owner",
            focus_attempt="owner-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project, now=NOW)
        root = work / "v2"
        coordination = acquire_coordination(work, "coordinator", "studio", 120, now=NOW)
        declare_resource(
            work,
            "shared-console",
            "Shared console",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        for item_id in ("owner", "waiting"):
            path = root / "items" / f"{item_id}.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace("resources: —", "resources: shared-console"),
                encoding="utf-8",
            )
        attempt = acquire_attempt(work, "owner-1", "worker", "studio", 120, now=NOW)
        claim_resource(
            work,
            "shared-console",
            "owner-1",
            "worker",
            "studio",
            120,
            attempt.lease_id,
            attempt.generation,
            now=NOW,
        )

        result = preview_parallel(work, project, "studio", now=NOW)

        self.assertEqual(
            {"owner": ("attempt-owned",), "waiting": ("resource-busy",)},
            {item.item_id: tuple(reason.code.value for reason in item.reasons) for item in result.excluded},
        )

    def test_invalid_selection_and_legacy_authority_are_rejected(self) -> None:
        project, work = self.mixed_state()
        with self.assertRaisesRegex(ParallelError, "PARALLEL_SELECTION_INVALID"):
            preview_parallel(work, project, "studio", selected=("alpha", "alpha"), now=NOW)
        with self.assertRaisesRegex(ParallelError, "PARALLEL_SELECTION_INVALID"):
            preview_parallel(work, project, "studio", selected=("missing",), now=NOW)

        legacy_project, legacy_work = create_state(["| alpha | ready | — | — | — | design | activate | Ready. |"])
        with self.assertRaisesRegex(ParallelError, "MIGRATION_REQUIRED"):
            preview_parallel(legacy_work, legacy_project, "studio", now=NOW)

    def test_outcomes_are_closed_and_stable(self) -> None:
        self.assertEqual(
            {"launchable", "requires-selection", "excluded"},
            {outcome.value for outcome in ParallelOutcome},
        )

    def test_preview_rejects_invalid_host_naive_time_and_a_changed_ledger(self) -> None:
        project, work = self.mixed_state()
        with self.assertRaisesRegex(ParallelError, "PARALLEL_HOST_INVALID"):
            preview_parallel(work, project, "../../escape", now=NOW)
        with self.assertRaisesRegex(ParallelError, "PARALLEL_TIME_INVALID"):
            preview_parallel(work, project, "studio", now=datetime(2026, 8, 19, 10, 0))
        with (
            patch("repo_work.parallel._preview_revision", side_effect=("before", "after")),
            self.assertRaisesRegex(ParallelError, "STATE_REVISION_STALE"),
        ):
            preview_parallel(work, project, "studio", now=NOW)

    def test_preview_revision_includes_resource_claim_changes(self) -> None:
        project, work = create_state(
            ["| owner | active | — | — | owner-1 | design | continue | Active. |"],
            focus_item="owner",
            focus_attempt="owner-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project, now=NOW)
        root = work / "v2"
        coordination = acquire_coordination(work, "coordinator", "studio", 120, now=NOW)
        declare_resource(
            work,
            "shared-console",
            "Shared console",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        item_path = root / "items" / "owner.md"
        item_path.write_text(
            item_path.read_text(encoding="utf-8").replace("resources: —", "resources: shared-console"),
            encoding="utf-8",
        )
        attempt = acquire_attempt(work, "owner-1", "worker", "studio", 120, now=NOW)
        claim = claim_resource(
            work,
            "shared-console",
            "owner-1",
            "worker",
            "studio",
            120,
            attempt.lease_id,
            attempt.generation,
            now=NOW,
        )
        before_state = state_revision(work)
        before_preview = preview_parallel(work, project, "studio", now=NOW)

        release_resource(
            work,
            "shared-console",
            "studio",
            claim.lease_id,
            claim.generation,
            now=NOW,
        )
        after_preview = preview_parallel(work, project, "studio", now=NOW)

        self.assertEqual(before_state, state_revision(work))
        self.assertNotEqual(before_preview.revision, after_preview.revision)

    def test_preview_waits_for_a_failed_transition_to_finish_rollback(self) -> None:
        project, work = create_state(
            [
                "| prerequisite | active | — | — | prerequisite-1 | design | continue | Active. |",
                "| dependent | ready | — | prerequisite | — | design | activate | Waiting. |",
            ],
            focus_item="prerequisite",
            focus_attempt="prerequisite-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project, now=NOW)
        coordination = acquire_coordination(work, "coordinator", "studio", 120, now=NOW)
        action = next(
            candidate
            for candidate in actions_for(
                work,
                project,
                "coordinator",
                lease_id=coordination.lease_id,
                generation=coordination.generation,
            )
            if candidate.action_id == "complete:prerequisite-1"
        )
        transient_state_reached = Event()
        allow_rollback = Event()

        def fail_after_transient_state(boundary: int, _change: FileChange) -> None:
            if boundary != 3:
                return
            transient_state_reached.set()
            self.assertTrue(allow_rollback.wait(2))
            raise RuntimeError("injected completion failure")

        with ThreadPoolExecutor(max_workers=2) as executor:
            transition = executor.submit(
                apply_action,
                work,
                project,
                action,
                {"evidence": "Completion failed after exposing transient files."},
                failpoint=fail_after_transient_state,
            )
            self.assertTrue(transient_state_reached.wait(2))
            preview = executor.submit(preview_parallel, work, project, "studio", selected=("dependent",), now=NOW)
            try:
                with self.assertRaises(FutureTimeoutError):
                    preview.result(timeout=0.1)
            finally:
                allow_rollback.set()
            with self.assertRaisesRegex(RuntimeError, "injected completion failure"):
                transition.result()
            result = preview.result()

        self.assertFalse(result.safe)
        self.assertEqual(("dependent",), tuple(item.item_id for item in result.excluded))
        self.assertEqual("dependency-live", result.excluded[0].reasons[0].code.value)

    def test_duplicate_resource_declarations_are_invalid_instead_of_self_conflicting(self) -> None:
        project, work = self.mixed_state()
        item_path = work / "v2" / "items" / "alpha.md"
        item_path.write_text(
            item_path.read_text(encoding="utf-8").replace("resources: —", "resources: shared-console, shared-console"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ParallelError, "ITEM_RESOURCES_DUPLICATE"):
            preview_parallel(work, project, "studio", selected=("alpha",), now=NOW)

    def test_live_time_is_captured_after_waiting_for_the_transition_lock(self) -> None:
        project, work = self.mixed_state()
        captured = Event()

        def capture_time(value: datetime | None) -> datetime:
            self.assertIsNone(value)
            captured.set()
            return NOW

        with (
            patch("repo_work.parallel._current_time", side_effect=capture_time),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            with transition_lock(work.resolve()):
                preview = executor.submit(preview_parallel, work, project, "studio")
                self.assertFalse(captured.wait(0.1))
            result = preview.result()

        self.assertTrue(captured.is_set())
        self.assertTrue(result.safe)


if __name__ == "__main__":
    unittest.main()
