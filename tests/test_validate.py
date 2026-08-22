import json
import shutil
import tempfile
import unittest
from pathlib import Path

from charlie_pinboard.legacy.authority import AuthorityVersion, write_authority_selector
from charlie_pinboard.legacy.validate import validate_work_state

QUEUE_TEMPLATE = """\
---
kind: work-queue
schema: repo-work/v1
updated: "2026-08-16"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
{rows}
"""

CURRENT_TEMPLATE = """\
---
kind: work-current
schema: repo-work/v1
updated: "2026-08-16"
focus_item: {focus_item}
focus_attempt: {focus_attempt}
next_action: {next_action}
---

# Current Work
"""

ITEM_TEMPLATE = """\
---
kind: work-item
schema: repo-work/v1
item: {item}
user_label: "{label}"
updated: "2026-08-16"
---

# {label}

## Context arc

Before and trigger. Why it matters. After and trajectory.
"""

ATTEMPT_TEMPLATE = """\
---
kind: work-attempt
schema: repo-work/v1
attempt: {attempt}
item: {item}
state: active
branch: codex/{item}
base_revision: abc123
owner: worker-task
updated: "2026-08-16"
---

# Attempt
"""


class WorkStateValidationTest(unittest.TestCase):
    def test_selected_v2_authority_rejects_a_coherent_v1_ledger(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )
        root = work / "v2"
        root.mkdir()
        for name in ("items", "attempts", "inbox", "history"):
            shutil.move(work / name, root / name)
        for name in ("queue.md", "current.md", "coordinator.json"):
            shutil.move(work / name, root / name)
        (root / "migration-complete.md").write_text(
            "---\nkind: migration-complete\nschema: repo-work/v2\n---\n",
            encoding="utf-8",
        )
        write_authority_selector(work, AuthorityVersion.V2, "v2")

        report = validate_work_state(work, project)

        self.assertFalse(report.valid)
        self.assertIn("DOCUMENT_SCHEMA_INVALID", {diagnostic.code for diagnostic in report.diagnostics})

    def make_state(
        self,
        rows: list[str],
        *,
        focus_item: str = "null",
        focus_attempt: str = "null",
    ) -> tuple[Path, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        (work / "items").mkdir(parents=True)
        (work / "attempts").mkdir()
        (work / "inbox").mkdir()
        (work / "history" / "items").mkdir(parents=True)
        (work / "queue.md").write_text(QUEUE_TEMPLATE.format(rows="\n".join(rows)), encoding="utf-8")
        (work / "current.md").write_text(
            CURRENT_TEMPLATE.format(
                focus_item=focus_item,
                focus_attempt=focus_attempt,
                next_action="continue" if focus_item != "null" else "select",
            ),
            encoding="utf-8",
        )
        for row in rows:
            item = row.split("|")[1].strip()
            (work / "items" / f"{item}.md").write_text(
                ITEM_TEMPLATE.format(item=item, label=item.replace("-", " ").title()),
                encoding="utf-8",
            )
        (work / "coordinator.json").write_text(
            json.dumps(
                {
                    "schema": "repo-work/v1",
                    "project_root": str(project),
                    "task_id": "coordinator-task",
                    "host_id": "local-host",
                    "generation": 1,
                    "registered_at": "2026-08-16T12:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return project, work

    def codes(self, work: Path, project: Path) -> set[str]:
        return {diagnostic.code for diagnostic in validate_work_state(work, project).diagnostics}

    def test_accepts_coherent_idle_state(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )

        report = validate_work_state(work, project)

        self.assertTrue(report.valid, report.render())

    def test_rejects_missing_item_record(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )
        (work / "items" / "reveal-core.md").unlink()

        self.assertIn("ITEM_RECORD_MISSING", self.codes(work, project))

    def test_rejects_unknown_dependency(self) -> None:
        project, work = self.make_state(["| reveal-core | blocked | — | missing-core | — | finding | none | Wait. |"])

        self.assertIn("DEPENDENCY_UNKNOWN", self.codes(work, project))

    def test_accepts_dependency_completed_in_history(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | old-foundation | — | accepted design | activate | Ready. |"]
        )
        (work / "history" / "items" / "old-foundation.md").write_text(
            "---\nkind: work-history\nschema: repo-work/v1\nitem: old-foundation\nstate: done\n---\n",
            encoding="utf-8",
        )

        report = validate_work_state(work, project)

        self.assertTrue(report.valid, report.render())

    def test_rejects_dependency_cycle(self) -> None:
        project, work = self.make_state(
            [
                "| first | blocked | — | second | — | finding | none | Wait. |",
                "| second | blocked | — | first | — | finding | none | Wait. |",
            ]
        )

        self.assertIn("DEPENDENCY_CYCLE", self.codes(work, project))

    def test_rejects_focus_pointer_without_active_queue_item(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"],
            focus_item="reveal-core",
            focus_attempt="attempt-1",
        )

        self.assertIn("CURRENT_FOCUS_MISMATCH", self.codes(work, project))

    def test_rejects_legacy_active_pointer_fields(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )
        (work / "current.md").write_text(
            CURRENT_TEMPLATE.format(
                focus_item="null",
                focus_attempt="null",
                next_action="select",
            )
            .replace("focus_item", "active_item")
            .replace("focus_attempt", "active_attempt"),
            encoding="utf-8",
        )

        self.assertIn("HEADER_FIELD_REQUIRED", self.codes(work, project))

    def test_accepts_matching_active_attempt(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | active | — | — | attempt-1 | accepted design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="attempt-1",
        )
        attempt_dir = work / "attempts" / "attempt-1"
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
            ATTEMPT_TEMPLATE.format(attempt="attempt-1", item="reveal-core"),
            encoding="utf-8",
        )

        report = validate_work_state(work, project)

        self.assertTrue(report.valid, report.render())

    def test_accepts_multiple_active_attempts_with_one_optional_focus(self) -> None:
        project, work = self.make_state(
            [
                "| reveal-core | active | — | — | attempt-1 | design | continue | Active. |",
                "| mapping-create | active | — | — | attempt-2 | design | continue | Active. |",
            ],
            focus_item="mapping-create",
            focus_attempt="attempt-2",
        )
        for attempt, item in (("attempt-1", "reveal-core"), ("attempt-2", "mapping-create")):
            attempt_dir = work / "attempts" / attempt
            attempt_dir.mkdir()
            (attempt_dir / "attempt.md").write_text(
                ATTEMPT_TEMPLATE.format(attempt=attempt, item=item), encoding="utf-8"
            )

        report = validate_work_state(work, project)

        self.assertTrue(report.valid, report.render())

    def test_rejects_coordinator_project_mismatch(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )
        coordinator = json.loads((work / "coordinator.json").read_text(encoding="utf-8"))
        coordinator["project_root"] = "/another/project"
        (work / "coordinator.json").write_text(json.dumps(coordinator), encoding="utf-8")

        self.assertIn("COORDINATOR_PROJECT_MISMATCH", self.codes(work, project))

    def test_rejects_queue_contract_and_orphaned_item_records(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"]
        )
        (work / "items" / "orphan.md").write_text(ITEM_TEMPLATE.format(item="orphan", label="Orphan"), encoding="utf-8")
        queue_text = (work / "queue.md").read_text(encoding="utf-8").replace("kind: work-queue", "kind: wrong")
        (work / "queue.md").write_text(queue_text, encoding="utf-8")

        codes = self.codes(work, project)

        self.assertIn("DOCUMENT_KIND_INVALID", codes)
        self.assertIn("ITEM_RECORD_ORPHANED", codes)

    def test_reports_unreadable_queue_and_current_documents(self) -> None:
        project, work = self.make_state([])
        (work / "queue.md").unlink()
        self.assertIn("QUEUE_UNREADABLE", self.codes(work, project))

        project, work = self.make_state([])
        (work / "current.md").unlink()
        self.assertIn("CURRENT_UNREADABLE", self.codes(work, project))

        project, work = self.make_state([])
        (work / "queue.md").write_text("not a queue", encoding="utf-8")
        self.assertIn("HEADER_MISSING", self.codes(work, project))

    def test_rejects_invalid_history_and_coordinator_records(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | complete-item | — | accepted design | activate | Ready. |"]
        )
        (work / "history" / "items" / "complete-item.md").write_text("not a header", encoding="utf-8")
        (work / "coordinator.json").write_text("[]", encoding="utf-8")

        codes = self.codes(work, project)

        self.assertIn("HEADER_MISSING", codes)
        self.assertIn("COORDINATOR_INVALID", codes)

    def test_rejects_attempt_presence_record_and_state_mismatches(self) -> None:
        cases = (
            (
                "| reveal-core | active | — | — | — | design | continue | Active. |",
                None,
                "QUEUE_ATTEMPT_MISSING",
            ),
            (
                "| reveal-core | ready | — | — | attempt-1 | design | activate | Ready. |",
                None,
                "QUEUE_ATTEMPT_UNEXPECTED",
            ),
            (
                "| reveal-core | active | — | — | attempt-1 | design | continue | Active. |",
                None,
                "ATTEMPT_RECORD_MISSING",
            ),
            (
                "| reveal-core | active | — | — | attempt-1 | design | continue | Active. |",
                "invalid",
                "HEADER_MISSING",
            ),
            (
                "| reveal-core | active | — | — | attempt-1 | design | continue | Active. |",
                "mismatch",
                "ATTEMPT_QUEUE_MISMATCH",
            ),
        )
        for row, attempt_kind, expected in cases:
            with self.subTest(expected=expected):
                project, work = self.make_state([row])
                if attempt_kind is not None:
                    attempt_dir = work / "attempts" / "attempt-1"
                    attempt_dir.mkdir()
                    text = (
                        "invalid"
                        if attempt_kind == "invalid"
                        else ATTEMPT_TEMPLATE.format(attempt="attempt-1", item="different-item")
                    )
                    (attempt_dir / "attempt.md").write_text(text, encoding="utf-8")
                self.assertIn(expected, self.codes(work, project))

    def test_rejects_current_attempt_without_item_and_attempt_mismatch(self) -> None:
        project, work = self.make_state([], focus_attempt="attempt-1")
        self.assertIn("CURRENT_ATTEMPT_WITHOUT_ITEM", self.codes(work, project))

        project, work = self.make_state(
            ["| reveal-core | active | — | — | attempt-1 | design | continue | Active. |"],
            focus_item="reveal-core",
            focus_attempt="attempt-2",
        )
        attempt_dir = work / "attempts" / "attempt-1"
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
            ATTEMPT_TEMPLATE.format(attempt="attempt-1", item="reveal-core"), encoding="utf-8"
        )
        self.assertIn("CURRENT_FOCUS_MISMATCH", self.codes(work, project))

    def test_rejects_schema_item_record_and_missing_coordinator_contracts(self) -> None:
        project, work = self.make_state(["| reveal-core | ready | — | — | — | accepted design | activate | Ready. |"])
        (work / "queue.md").write_text(
            (work / "queue.md").read_text(encoding="utf-8").replace("schema: repo-work/v1", "schema: repo-work/v2"),
            encoding="utf-8",
        )
        item_path = work / "items" / "reveal-core.md"
        item_path.write_text(
            item_path.read_text(encoding="utf-8").replace("item: reveal-core", "item: other"), encoding="utf-8"
        )
        (work / "coordinator.json").unlink()

        codes = self.codes(work, project)

        self.assertIn("DOCUMENT_SCHEMA_INVALID", codes)
        self.assertIn("ITEM_RECORD_MISMATCH", codes)
        self.assertIn("COORDINATOR_NOT_REGISTERED", codes)

        item_path.write_text("not a record", encoding="utf-8")
        self.assertIn("HEADER_MISSING", self.codes(work, project))

    def test_detects_pending_transaction_without_mutating_it(self) -> None:
        project, work = self.make_state([])
        journal = work.parent / ".work.repo-work-journal"
        journal.mkdir()

        self.assertIn("COMMIT_RECOVERY_REQUIRED", self.codes(work, project))
        self.assertTrue(journal.is_dir())

    def test_accepts_state_without_optional_history_directory(self) -> None:
        project, work = self.make_state([])
        (work / "history" / "items").rmdir()
        (work / "history").rmdir()

        self.assertTrue(validate_work_state(work, project).valid)


if __name__ == "__main__":
    unittest.main()
