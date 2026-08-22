import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

from charlie_pinboard.legacy import resources as resource_module
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority, write_authority_selector
from charlie_pinboard.legacy.leases import LeaseRecord, acquire_attempt, acquire_coordination, revoke_attempt
from charlie_pinboard.legacy.migration import migrate_to_v2
from charlie_pinboard.legacy.resources import (
    ResourceError,
    ResourceScope,
    claim_resource,
    declare_resource,
    read_resource,
    read_resource_claim,
    release_resource,
    renew_resource,
    require_resource,
    revoke_resource,
)
from charlie_pinboard.legacy.validate import validate_work_state

from .support import create_state

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def v2_work() -> Path:
    work = Path(tempfile.mkdtemp()) / "work"
    root = work / "v2"
    (root / "resources").mkdir(parents=True)
    (root / "leases" / "resources").mkdir(parents=True)
    (root / "attempts").mkdir(parents=True)
    (root / "queue.md").write_text(
        """---
kind: work-queue
schema: repo-work/v2
updated: "2026-08-18"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
""",
        encoding="utf-8",
    )
    write_authority_selector(work, AuthorityVersion.V2, "v2")
    return work


def attempt_lease(work: Path, attempt_id: str, task_id: str, host_id: str) -> LeaseRecord:
    root = resolve_authority(work).work_root
    item_id = f"item-{attempt_id}"
    path = root / "attempts" / attempt_id / "attempt.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""---
kind: work-attempt
schema: repo-work/v2
attempt: {attempt_id}
item: {item_id}
state: active
branch: codex/item
base_revision: abc
provenance: {task_id}
owner_task_id: unclaimed
owner_host_id: unclaimed
lease_id: unclaimed
lease_generation: 0
lease_acquired_at: "2026-08-18T12:00:00Z"
lease_expires_at: "2026-08-18T12:00:00Z"
lease_status: released
---
""",
        encoding="utf-8",
    )
    queue_path = root / "queue.md"
    queue_path.write_text(
        queue_path.read_text(encoding="utf-8")
        + f"| {item_id} | active | — | — | {attempt_id} | test | continue | Active. |\n",
        encoding="utf-8",
    )
    return acquire_attempt(work, attempt_id, task_id, host_id, 300, now=NOW)


class ResourceTest(unittest.TestCase):
    def test_public_resource_strings_round_trip_without_invalidating_authority(self) -> None:
        project, work = create_state(
            ["| feature | active | — | — | feature-1 | design | continue | Active. |"],
            focus_item="feature",
            focus_attempt="feature-1",
            create_active_attempt=True,
        )
        migrate_to_v2(work, project, now=NOW)
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)

        declaration = declare_resource(
            work,
            "true",
            "true",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )

        self.assertEqual(("true", "true"), (declaration.resource_id, declaration.label))
        self.assertTrue(validate_work_state(work, project).valid)

        attempt = acquire_attempt(
            work,
            "feature-1",
            "true",
            "false",
            300,
            now=NOW,
            lease_id="null",
        )
        self.assertTrue(validate_work_state(work, project).valid)

        claim = claim_resource(
            work,
            "true",
            "feature-1",
            "true",
            "false",
            300,
            attempt.lease_id,
            attempt.generation,
            now=NOW,
            lease_id="~",
        )

        self.assertEqual(
            ("true", "feature-1", "true", "false", "~", "null"),
            (
                claim.resource_id,
                claim.attempt_id,
                claim.task_id,
                claim.host_id,
                claim.lease_id,
                claim.attempt_lease_id,
            ),
        )
        self.assertTrue(validate_work_state(work, project).valid)

    def test_resource_writers_reject_outside_declaration_and_claim_symlinks(self) -> None:
        work = v2_work()
        root = resolve_authority(work).work_root
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)
        outside_resources = Path(tempfile.mkdtemp()) / "outside-resources"
        (root / "resources").replace(outside_resources)
        (root / "resources").symlink_to(outside_resources, target_is_directory=True)

        with self.assertRaisesRegex(ResourceError, "RESOURCE_ID_INVALID"):
            declare_resource(
                work,
                "bitwig-live",
                "Bitwig",
                coordination.lease_id,
                coordination.generation,
                scope="host-local",
                now=NOW,
            )
        self.assertEqual([], list(outside_resources.iterdir()))

        (root / "resources").unlink()
        outside_resources.replace(root / "resources")
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        attempt = attempt_lease(work, "attempt-a", "task-a", "host")
        outside_claims = Path(tempfile.mkdtemp()) / "outside-claims"
        (root / "leases" / "resources").replace(outside_claims)
        (root / "leases" / "resources").symlink_to(outside_claims, target_is_directory=True)

        with self.assertRaisesRegex(ResourceError, "RESOURCE_IDENTITY_INVALID"):
            claim_resource(
                work,
                "bitwig-live",
                "attempt-a",
                "task-a",
                "host",
                60,
                attempt.lease_id,
                attempt.generation,
                now=NOW,
            )
        self.assertEqual([], list(outside_claims.iterdir()))

    def test_attempt_revocation_cannot_overtake_claim_revalidation_and_write(self) -> None:
        work = v2_work()
        root = resolve_authority(work).work_root
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        attempt = attempt_lease(work, "attempt-a", "task-a", "host")
        checked = Event()
        continue_claim = Event()

        real_require_attempt = resource_module.require_attempt

        def paused_require_attempt(
            work_root: Path,
            attempt_id: str,
            lease_id: str,
            generation: int,
            *,
            now: datetime | None = None,
        ) -> LeaseRecord:
            record = real_require_attempt(work_root, attempt_id, lease_id, generation, now=now)
            checked.set()
            continue_claim.wait(timeout=5)
            return record

        with (
            patch("charlie_pinboard.legacy.resources.require_attempt", paused_require_attempt),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            claim_future = executor.submit(
                claim_resource,
                work,
                "bitwig-live",
                "attempt-a",
                "task-a",
                "host",
                60,
                attempt.lease_id,
                attempt.generation,
                now=NOW,
            )
            self.assertTrue(checked.wait(timeout=5))
            revoke_future = executor.submit(
                revoke_attempt,
                work,
                "attempt-a",
                coordination.lease_id,
                coordination.generation,
                now=NOW,
            )
            with self.assertRaises(FutureTimeoutError):
                revoke_future.result(timeout=0.1)
            continue_claim.set()
            claim = claim_future.result(timeout=5)
            revoke_future.result(timeout=5)

        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            require_resource(root, "bitwig-live", "host", claim.lease_id, claim.generation, now=NOW)

    def test_resource_markdown_rejects_invalid_declarations_and_claims(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        resources = work / "resources"
        claims = work / "leases" / "resources"
        resources.mkdir(parents=True)
        claims.mkdir(parents=True)
        declaration = resources / "bitwig-live.md"

        declaration.write_text("---\nkind: wrong\nschema: repo-work/v2\n---\n", encoding="utf-8")
        with self.assertRaisesRegex(ResourceError, "RESOURCE_DECLARATION_INVALID"):
            read_resource(work, "bitwig-live")
        declaration.write_text(
            "---\nkind: work-resource\nschema: repo-work/v2\nresource: bitwig-live\n"
            'label: "Bitwig"\nscope: host-local\nmode: shared\n---\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_DECLARATION_INVALID"):
            read_resource(work, "bitwig-live")
        declaration.write_text(
            "---\nkind: work-resource\nschema: repo-work/v2\nresource: bitwig-live\n"
            'label: "Bitwig"\nscope: global\nmode: exclusive\n---\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_SCOPE_INVALID"):
            read_resource(work, "bitwig-live")
        declaration.write_text(
            "---\nkind: work-resource\nschema: repo-work/v2\nresource: other\n"
            'label: "Bitwig"\nscope: host-local\nmode: exclusive\n---\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_IDENTITY_MISMATCH"):
            read_resource(work, "bitwig-live")

        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_REQUIRED"):
            read_resource_claim(work, "bitwig-live", "host")
        claim_path = claims / "bitwig-live--host.md"
        claim_path.write_text("---\nkind: wrong\nschema: repo-work/v2\n---\n", encoding="utf-8")
        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_INVALID"):
            read_resource_claim(work, "bitwig-live", "host")
        claim_path.write_text(
            "---\nkind: resource-claim\nschema: repo-work/v2\nlease_generation: invalid\nlease_status: active\n---\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_INVALID"):
            read_resource_claim(work, "bitwig-live", "host")
        claim_path.write_text(
            "---\nkind: resource-claim\nschema: repo-work/v2\nresource: bitwig-live\nattempt: attempt-a\n"
            "attempt_lease_id: attempt-lease\nattempt_lease_generation: 1\nowner_task_id: task\n"
            "owner_host_id: host\nlease_id: claim\nlease_generation: 1\nlease_acquired_at: invalid\n"
            'lease_expires_at: "2026-08-18T12:01:00Z"\nlease_status: active\n---\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_INVALID"):
            read_resource_claim(work, "bitwig-live", "host")
        claim_path.write_text(
            claim_path.read_text(encoding="utf-8")
            .replace("lease_acquired_at: invalid", 'lease_acquired_at: "2026-08-18T12:02:00Z"')
            .replace('lease_expires_at: "2026-08-18T12:01:00Z"', 'lease_expires_at: "2026-08-18T12:01:00Z"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_INVALID"):
            read_resource_claim(work, "bitwig-live", "host")
        claim_path.write_text(
            claim_path.read_text(encoding="utf-8")
            .replace("resource: bitwig-live", "resource: other")
            .replace('lease_acquired_at: "2026-08-18T12:02:00Z"', 'lease_acquired_at: "2026-08-18T12:00:00Z"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_IDENTITY_MISMATCH"):
            read_resource_claim(work, "bitwig-live", "host")
        with self.assertRaisesRegex(ResourceError, "RESOURCE_IDENTITY_INVALID"):
            read_resource_claim(work, "bitwig-live", "../../escape")
        with self.assertRaisesRegex(ResourceError, "RESOURCE_TIME_INVALID"):
            require_resource(work, "bitwig-live", "host", "claim", 1, now=datetime(2026, 8, 18, 12, 0))

    def test_resource_declarations_are_validated_idempotent_and_coordination_fenced(self) -> None:
        work = v2_work()
        root = resolve_authority(work).work_root
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)

        with self.assertRaisesRegex(ResourceError, "RESOURCE_DECLARATION_INVALID"):
            declare_resource(
                work,
                "not valid",
                "Bitwig",
                coordination.lease_id,
                coordination.generation,
                scope=ResourceScope.HOST_LOCAL,
                now=NOW,
            )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_SCOPE_INVALID"):
            declare_resource(
                work,
                "bitwig-live",
                "Bitwig",
                coordination.lease_id,
                coordination.generation,
                scope="global",
                now=NOW,
            )
        declared = declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope=ResourceScope.HOST_LOCAL,
            now=NOW,
        )
        self.assertEqual(declared, read_resource(root, "bitwig-live"))
        self.assertEqual(
            declared,
            declare_resource(
                work,
                "bitwig-live",
                "Bitwig",
                coordination.lease_id,
                coordination.generation,
                scope="host-local",
                now=NOW,
            ),
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_ALREADY_DECLARED"):
            declare_resource(
                work,
                "bitwig-live",
                "Different label",
                coordination.lease_id,
                coordination.generation,
                scope="host-local",
                now=NOW,
            )
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            declare_resource(work, "other", "Other", "stale", 0, scope="host-local", now=NOW)
        with self.assertRaisesRegex(ResourceError, "RESOURCE_NOT_DECLARED"):
            read_resource(root, "missing")

    def test_resource_claims_require_a_matching_live_attempt_and_are_fenced(self) -> None:
        work = v2_work()
        root = resolve_authority(work).work_root
        coordination = acquire_coordination(work, "coordinator", "host", 300, now=NOW)
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        attempt = attempt_lease(work, "attempt-a", "task-a", "host")

        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_REQUIRED"):
            claim_resource(
                work,
                "bitwig-live",
                "attempt-a",
                "task-a",
                "host",
                0,
                attempt.lease_id,
                attempt.generation,
                now=NOW,
            )
        with self.assertRaisesRegex(ResourceError, "ATTEMPT_LEASE_REQUIRED"):
            claim_resource(
                work,
                "bitwig-live",
                "attempt-a",
                "different-task",
                "host",
                60,
                attempt.lease_id,
                attempt.generation,
                now=NOW,
            )
        claim = claim_resource(
            work,
            "bitwig-live",
            "attempt-a",
            "task-a",
            "host",
            120,
            attempt.lease_id,
            attempt.generation,
            now=NOW,
            lease_id="claim-a",
        )
        self.assertEqual(
            claim,
            require_resource(root, "bitwig-live", "host", claim.lease_id, claim.generation, now=NOW),
        )
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            require_resource(root, "bitwig-live", "host", "stale", claim.generation, now=NOW)
        with self.assertRaisesRegex(ResourceError, "RESOURCE_CLAIM_REQUIRED"):
            require_resource(
                root,
                "bitwig-live",
                "host",
                claim.lease_id,
                claim.generation,
                now=NOW + timedelta(seconds=121),
            )

    def test_host_local_exclusive_resource_has_one_fenced_holder(self) -> None:
        work = v2_work()
        root = resolve_authority(work).work_root
        coordination = acquire_coordination(work, "coordinator", "studio-mac", 300, now=NOW)
        declare_resource(
            work,
            "bitwig-live",
            "Bitwig live application",
            coordination.lease_id,
            coordination.generation,
            scope="host-local",
            now=NOW,
        )
        attempt_a = attempt_lease(work, "attempt-a", "task-a", "studio-mac")
        attempt_b = attempt_lease(work, "attempt-b", "task-b", "studio-mac")
        attempt_laptop = attempt_lease(work, "attempt-laptop", "task-b", "laptop")
        attempt_c = attempt_lease(work, "attempt-c", "task-c", "studio-mac")

        first = claim_resource(
            work,
            "bitwig-live",
            "attempt-a",
            "task-a",
            "studio-mac",
            60,
            attempt_a.lease_id,
            attempt_a.generation,
            now=NOW,
            lease_id="claim-a",
        )
        with self.assertRaisesRegex(ResourceError, "RESOURCE_BUSY") as busy:
            claim_resource(
                work,
                "bitwig-live",
                "attempt-b",
                "task-b",
                "studio-mac",
                60,
                attempt_b.lease_id,
                attempt_b.generation,
                now=NOW,
                lease_id="claim-b",
            )
        self.assertIn("attempt-a", str(busy.exception))

        other_host = claim_resource(
            work,
            "bitwig-live",
            "attempt-laptop",
            "task-b",
            "laptop",
            60,
            attempt_laptop.lease_id,
            attempt_laptop.generation,
            now=NOW,
            lease_id="claim-b",
        )
        self.assertEqual("attempt-laptop", other_host.attempt_id)

        renewed = renew_resource(work, "bitwig-live", "studio-mac", first.lease_id, first.generation, 120, now=NOW)
        self.assertEqual(NOW + timedelta(seconds=120), renewed.expires_at)
        released = release_resource(work, "bitwig-live", "studio-mac", renewed.lease_id, renewed.generation, now=NOW)
        self.assertEqual("released", released.status.value)
        replacement = claim_resource(
            work,
            "bitwig-live",
            "attempt-c",
            "task-c",
            "studio-mac",
            60,
            attempt_c.lease_id,
            attempt_c.generation,
            now=NOW,
            lease_id="claim-c",
        )
        revoked = revoke_resource(
            work,
            "bitwig-live",
            "studio-mac",
            coordination.lease_id,
            coordination.generation,
            now=NOW,
        )
        self.assertGreater(revoked.generation, replacement.generation)
        self.assertEqual("revoked", read_resource_claim(root, "bitwig-live", "studio-mac").status.value)

        revoke_attempt(
            work,
            "attempt-laptop",
            coordination.lease_id,
            coordination.generation,
            now=NOW,
        )
        with self.assertRaisesRegex(ResourceError, "LEASE_FENCED"):
            renew_resource(
                work,
                "bitwig-live",
                "laptop",
                other_host.lease_id,
                other_host.generation,
                60,
                now=NOW,
            )

    def test_offline_attempt_needs_no_resource_claim(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        (work / "resources").mkdir(parents=True)
        (work / "leases" / "resources").mkdir(parents=True)
        self.assertFalse(list((work / "leases" / "resources").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
