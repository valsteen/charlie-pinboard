import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from charlie_pinboard.legacy.authority import AuthorityVersion, write_authority_selector
from charlie_pinboard.legacy.leases import (
    LeaseError,
    acquire_attempt,
    acquire_coordination,
    read_attempt_lease,
    read_coordination_lease,
    release_attempt,
    release_coordination,
    renew_attempt,
    renew_coordination,
    require_attempt,
    require_coordination,
    revoke_attempt,
    revoke_coordination,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def work_root() -> Path:
    work = Path(tempfile.mkdtemp()) / "work"
    root = work / "v2"
    (root / "leases").mkdir(parents=True)
    (root / "attempts" / "feature-1").mkdir(parents=True)
    (root / "queue.md").write_text(
        """---
kind: work-queue
schema: repo-work/v2
updated: "2026-08-18"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| feature | active | — | — | feature-1 | design | continue | Active. |
""",
        encoding="utf-8",
    )
    (root / "attempts" / "feature-1" / "attempt.md").write_text(
        """---
kind: work-attempt
schema: repo-work/v2
attempt: feature-1
item: feature
state: active
branch: codex/feature
base_revision: abc123
provenance: test
owner_task_id: unclaimed
owner_host_id: unclaimed
lease_id: unclaimed
lease_generation: 0
lease_acquired_at: "2026-08-18T12:00:00Z"
lease_expires_at: "2026-08-18T12:00:00Z"
lease_status: released
updated: "2026-08-18"
---

# Attempt
""",
        encoding="utf-8",
    )
    write_authority_selector(work, AuthorityVersion.V2, "v2")
    return work


def active_root(work: Path) -> Path:
    return work / "v2"


class CoordinationLeaseTest(unittest.TestCase):
    def test_coordination_writer_rejects_an_outside_leases_symlink(self) -> None:
        work = work_root()
        root = active_root(work)
        outside = Path(tempfile.mkdtemp()) / "outside-leases"
        (root / "leases").replace(outside)
        (root / "leases").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(LeaseError, "LEASE_IDENTITY_INVALID"):
            acquire_coordination(work, "task", "host", 60, now=NOW)

        self.assertFalse((outside / "coordination.md").exists())

    def test_coordination_markdown_rejects_naive_inverted_and_unsafe_identity_values(self) -> None:
        work = work_root()
        root = active_root(work)
        path = root / "leases" / "coordination.md"
        path.write_text(
            "---\nkind: coordination-lease\nschema: repo-work/v2\nowner_task_id: task\n"
            "owner_host_id: host\nlease_id: lease\nlease_generation: 1\n"
            'lease_acquired_at: "2026-08-18T12:00:00"\n'
            'lease_expires_at: "2026-08-18T12:01:00Z"\nlease_status: active\n---\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LeaseError, "LEASE_INVALID"):
            read_coordination_lease(root)
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace('"2026-08-18T12:00:00"', '"2026-08-18T12:02:00Z"')
            .replace('"2026-08-18T12:01:00Z"', '"2026-08-18T12:01:00Z"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LeaseError, "LEASE_INVALID"):
            read_coordination_lease(root)
        path.write_text(
            path.read_text(encoding="utf-8").replace("owner_host_id: host", "owner_host_id: ../../escape"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LeaseError, "LEASE_IDENTITY_INVALID"):
            read_coordination_lease(root)

    def test_coordination_rejects_invalid_and_missing_leases(self) -> None:
        work = work_root()
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_REQUIRED"):
            acquire_coordination(work, "", "host", 60, now=NOW)
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_REQUIRED"):
            require_coordination(active_root(work), "missing", 0, now=NOW)
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_REQUIRED"):
            renew_coordination(work, "missing", 0, 0, now=NOW)
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_REQUIRED"):
            revoke_coordination(work, now=NOW)
        with self.assertRaisesRegex(LeaseError, "LEASE_TIME_INVALID"):
            acquire_coordination(work, "task", "host", 60, now=datetime(2026, 8, 18, 12, 0))

    def test_two_tasks_racing_produce_one_holder(self) -> None:
        work = work_root()
        barrier = Barrier(2)

        def compete(task_id: str) -> str:
            barrier.wait()
            try:
                return acquire_coordination(work, task_id, "host", 60, now=NOW).task_id
            except LeaseError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(compete, ("task-a", "task-b")))

        self.assertEqual(1, outcomes.count("COORDINATION_LEASE_BUSY"))
        self.assertEqual(1, len(set(outcomes) & {"task-a", "task-b"}))

    def test_contention_renewal_expiry_release_and_revocation_are_fenced(self) -> None:
        work = work_root()
        first = acquire_coordination(work, "task-a", "host", 60, now=NOW, lease_id="lease-a")
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_BUSY") as busy:
            acquire_coordination(work, "task-b", "host", 60, now=NOW, lease_id="lease-b")
        self.assertIn("task-a", str(busy.exception))
        self.assertIn(first.expires_at.isoformat(), str(busy.exception))

        renewed = renew_coordination(work, first.lease_id, first.generation, 120, now=NOW)
        self.assertEqual(NOW + timedelta(seconds=120), renewed.expires_at)
        require_coordination(active_root(work), renewed.lease_id, renewed.generation, now=NOW)

        released = release_coordination(work, renewed.lease_id, renewed.generation, now=NOW)
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            require_coordination(active_root(work), released.lease_id, released.generation, now=NOW)

        second = acquire_coordination(work, "task-b", "host", 30, now=NOW, lease_id="lease-b")
        self.assertGreater(second.generation, first.generation)
        revoked = revoke_coordination(work, now=NOW)
        self.assertGreater(revoked.generation, second.generation)
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            require_coordination(active_root(work), second.lease_id, second.generation, now=NOW)

        replacement = acquire_coordination(work, "task-c", "host", 30, now=NOW, lease_id="lease-c")
        self.assertGreater(replacement.generation, revoked.generation)
        with self.assertRaisesRegex(LeaseError, "COORDINATION_LEASE_REQUIRED"):
            require_coordination(
                active_root(work), replacement.lease_id, replacement.generation, now=NOW + timedelta(seconds=31)
            )


class AttemptLeaseTest(unittest.TestCase):
    def test_attempt_writer_rejects_an_outside_attempts_symlink(self) -> None:
        work = work_root()
        root = active_root(work)
        outside = Path(tempfile.mkdtemp()) / "outside-attempts"
        (root / "attempts").replace(outside)
        (root / "attempts").symlink_to(outside, target_is_directory=True)
        original = (outside / "feature-1" / "attempt.md").read_bytes()

        with self.assertRaisesRegex(LeaseError, "ATTEMPT_ID_INVALID"):
            acquire_attempt(work, "feature-1", "task", "host", 60, now=NOW)

        self.assertEqual(original, (outside / "feature-1" / "attempt.md").read_bytes())

    def test_attempt_markdown_rejects_partial_negative_and_mismatched_lease_shapes(self) -> None:
        work = work_root()
        root = active_root(work)
        path = root / "attempts" / "feature-1" / "attempt.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("owner_host_id: unclaimed\n", ""), encoding="utf-8")
        with self.assertRaisesRegex(LeaseError, "LEASE_INVALID"):
            read_attempt_lease(root, "feature-1")
        path.write_text(original.replace("lease_generation: 0", "lease_generation: -1"), encoding="utf-8")
        with self.assertRaisesRegex(LeaseError, "LEASE_INVALID"):
            read_attempt_lease(root, "feature-1")
        path.write_text(original.replace("attempt: feature-1", "attempt: different"), encoding="utf-8")
        with self.assertRaisesRegex(LeaseError, "LEASE_IDENTITY_MISMATCH"):
            read_attempt_lease(root, "feature-1")
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_ID_INVALID"):
            acquire_attempt(work, "../../escape", "task", "host", 60, now=NOW)

    def test_attempt_lease_rejects_invalid_ttl_contention_expiry_and_stale_renewal(self) -> None:
        work = work_root()
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_LEASE_REQUIRED"):
            acquire_attempt(work, "feature-1", "task-a", "host", 0, now=NOW)
        first = acquire_attempt(work, "feature-1", "task-a", "host", 60, now=NOW, lease_id="attempt-a")
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_LEASE_REQUIRED") as busy:
            acquire_attempt(work, "feature-1", "task-b", "host", 60, now=NOW)
        self.assertIn("task-a", str(busy.exception))
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_LEASE_REQUIRED"):
            renew_attempt(work, "feature-1", first.lease_id, first.generation, 0, now=NOW)
        with self.assertRaisesRegex(LeaseError, "ATTEMPT_LEASE_EXPIRED"):
            require_attempt(
                active_root(work),
                "feature-1",
                first.lease_id,
                first.generation,
                now=NOW + timedelta(seconds=61),
            )

    def test_attempt_claim_is_renewable_releasable_and_revocable(self) -> None:
        work = work_root()
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)
        first = acquire_attempt(work, "feature-1", "task-a", "host", 60, now=NOW, lease_id="attempt-a")
        require_attempt(active_root(work), "feature-1", first.lease_id, first.generation, now=NOW)
        renewed = renew_attempt(work, "feature-1", first.lease_id, first.generation, 120, now=NOW)
        self.assertEqual(NOW + timedelta(seconds=120), renewed.expires_at)

        released = release_attempt(work, "feature-1", renewed.lease_id, renewed.generation, now=NOW)
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            require_attempt(active_root(work), "feature-1", released.lease_id, released.generation, now=NOW)

        first = acquire_attempt(work, "feature-1", "task-a", "host", 60, now=NOW, lease_id="attempt-a-2")

        revoked = revoke_attempt(work, "feature-1", coordination.lease_id, coordination.generation, now=NOW)
        self.assertGreater(revoked.generation, first.generation)
        with self.assertRaisesRegex(LeaseError, "LEASE_FENCED"):
            require_attempt(active_root(work), "feature-1", first.lease_id, first.generation, now=NOW)

        replacement = acquire_attempt(work, "feature-1", "task-b", "host", 60, now=NOW, lease_id="attempt-b")
        self.assertGreater(replacement.generation, revoked.generation)
        self.assertEqual("task-b", read_attempt_lease(active_root(work), "feature-1").task_id)


if __name__ == "__main__":
    unittest.main()
