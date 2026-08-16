from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repo_work.validate import validate_work_state


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
active_item: {active_item}
active_attempt: {active_attempt}
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
    def make_state(
        self,
        rows: list[str],
        *,
        active_item: str = "null",
        active_attempt: str = "null",
    ) -> tuple[Path, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        work = project / ".codex" / "work"
        (work / "items").mkdir(parents=True)
        (work / "attempts").mkdir()
        (work / "inbox").mkdir()
        (work / "history" / "items").mkdir(parents=True)
        (work / "queue.md").write_text(
            QUEUE_TEMPLATE.format(rows="\n".join(rows)), encoding="utf-8"
        )
        (work / "current.md").write_text(
            CURRENT_TEMPLATE.format(
                active_item=active_item,
                active_attempt=active_attempt,
                next_action="continue" if active_item != "null" else "select",
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
        project, work = self.make_state(
            ["| reveal-core | blocked | — | missing-core | — | finding | none | Wait. |"]
        )

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

    def test_rejects_current_pointer_without_active_queue_item(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | ready | — | — | — | accepted design | activate | First item. |"],
            active_item="reveal-core",
            active_attempt="attempt-1",
        )

        self.assertIn("CURRENT_ACTIVE_MISMATCH", self.codes(work, project))

    def test_accepts_matching_active_attempt(self) -> None:
        project, work = self.make_state(
            ["| reveal-core | active | — | — | attempt-1 | accepted design | continue | Active. |"],
            active_item="reveal-core",
            active_attempt="attempt-1",
        )
        attempt_dir = work / "attempts" / "attempt-1"
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
            ATTEMPT_TEMPLATE.format(attempt="attempt-1", item="reveal-core"),
            encoding="utf-8",
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


if __name__ == "__main__":
    unittest.main()
