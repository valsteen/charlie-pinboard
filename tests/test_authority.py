import json
import tempfile
import unittest
from pathlib import Path

from repo_work.authority import (
    AuthorityError,
    AuthorityVersion,
    authority_transaction,
    resolve_authority,
    write_authority_selector,
)


class AuthorityTest(unittest.TestCase):
    def test_selector_writes_and_reads_reject_unsupported_or_missing_roots_and_malformed_json(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        work.mkdir()
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"):
            write_authority_selector(work, AuthorityVersion.V1, "v1")
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_ROOT_MISSING"):
            write_authority_selector(work, AuthorityVersion.V2, "missing")

        selector = work / "authority.json"
        selector.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"):
            resolve_authority(work)
        selector.write_text(
            json.dumps({"schema": "repo-work-authority/v1", "current": "v2", "root": "missing"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_ROOT_MISSING"):
            resolve_authority(work)

    def test_missing_selector_means_legacy_v1_authority(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        work.mkdir()

        authority = resolve_authority(work)

        self.assertEqual(AuthorityVersion.V1, authority.version)
        self.assertEqual(work.resolve(), authority.work_root)

    def test_selector_resolves_one_v2_root_and_rejects_escape(self) -> None:
        work = Path(tempfile.mkdtemp()) / "work"
        current = work / "v2"
        current.mkdir(parents=True)
        write_authority_selector(work, AuthorityVersion.V2, "v2")

        authority = resolve_authority(work)

        self.assertEqual(AuthorityVersion.V2, authority.version)
        self.assertEqual(current.resolve(), authority.work_root)

        (work / "authority.json").write_text(
            json.dumps({"schema": "repo-work-authority/v1", "current": "v2", "root": "../outside"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"):
            resolve_authority(work)

    def test_selector_read_and_write_reject_an_authority_symlink_outside_the_base(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        work = temporary / "work"
        outside = temporary / "outside"
        work.mkdir()
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        (work / "v2").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"):
            write_authority_selector(work, AuthorityVersion.V2, "v2")
        self.assertFalse((work / "authority.json").exists())
        self.assertEqual([sentinel], list(outside.iterdir()))

        (work / "authority.json").write_text(
            json.dumps({"schema": "repo-work-authority/v1", "current": "v2", "root": "v2"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"):
            resolve_authority(work)
        with self.assertRaisesRegex(AuthorityError, "AUTHORITY_SELECTOR_INVALID"), authority_transaction(work):
            (outside / "escaped-write").write_text("unsafe\n", encoding="utf-8")
        self.assertEqual([sentinel], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
