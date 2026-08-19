import tempfile
import unittest
from pathlib import Path
from typing import override

from repo_work.revisions import subject_revision

ITEM = """---
kind: work-item
schema: repo-work/v2
item: {item}
user_label: "{item}"
state: {state}
timing: —
depends_on: {depends_on}
attempt: {attempt}
source: design
next_action: activate
notes: Ready.
resources: {resources}
updated: "2026-08-18"
---

# {item}
"""


class RevisionTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp()) / "work"
        (self.work / "items").mkdir(parents=True)
        (self.work / "attempts" / "a-1").mkdir(parents=True)
        (self.work / "resources").mkdir()
        (self.work / "leases" / "resources").mkdir(parents=True)
        (self.work / "items" / "a.md").write_text(
            ITEM.format(item="a", state="active", depends_on="foundation", attempt="a-1", resources="bitwig-live"),
            encoding="utf-8",
        )
        (self.work / "items" / "b.md").write_text(
            ITEM.format(item="b", state="ready", depends_on="—", attempt="—", resources="—"),
            encoding="utf-8",
        )
        (self.work / "history" / "items").mkdir(parents=True)
        (self.work / "history" / "items" / "foundation.md").write_text("foundation", encoding="utf-8")
        (self.work / "attempts" / "a-1" / "attempt.md").write_text("attempt", encoding="utf-8")
        (self.work / "resources" / "bitwig-live.md").write_text("resource", encoding="utf-8")
        (self.work / "leases" / "resources" / "bitwig-live--host.md").write_text("claim", encoding="utf-8")

    def test_unrelated_item_does_not_stale_subject_but_owned_scope_does(self) -> None:
        original = subject_revision(self.work, "a")
        b = self.work / "items" / "b.md"
        b.write_text(b.read_text(encoding="utf-8") + "\nunrelated\n", encoding="utf-8")
        self.assertEqual(original, subject_revision(self.work, "a"))

        scoped_paths = (
            self.work / "items" / "a.md",
            self.work / "history" / "items" / "foundation.md",
            self.work / "attempts" / "a-1" / "attempt.md",
            self.work / "resources" / "bitwig-live.md",
            self.work / "leases" / "resources" / "bitwig-live--host.md",
        )
        for path in scoped_paths:
            with self.subTest(path=path):
                before = subject_revision(self.work, "a")
                path.write_text(path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
                self.assertNotEqual(before, subject_revision(self.work, "a"))

    def test_reserved_resource_identity_remains_in_subject_scope(self) -> None:
        item = self.work / "items" / "a.md"
        item.write_text(item.read_text(encoding="utf-8").replace("resources: bitwig-live", 'resources: "null"'))
        resource = self.work / "resources" / "null.md"
        claim = self.work / "leases" / "resources" / "null--host.md"
        resource.write_text("resource", encoding="utf-8")
        claim.write_text("claim", encoding="utf-8")

        before_resource = subject_revision(self.work, "a")
        resource.write_text("resource changed", encoding="utf-8")
        after_resource = subject_revision(self.work, "a")
        self.assertNotEqual(before_resource, after_resource)

        claim.write_text("claim changed", encoding="utf-8")
        self.assertNotEqual(after_resource, subject_revision(self.work, "a"))


if __name__ == "__main__":
    unittest.main()
