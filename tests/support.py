import json
import tempfile
from pathlib import Path

from repo_work.actions import Action
from repo_work.proposals import create_proposal as create_serialized_proposal
from repo_work.transaction_store import CommitFailpoint
from repo_work.transition import apply_action as apply_serialized_action

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

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


def create_state(
    rows: list[str],
    *,
    focus_item: str = "null",
    focus_attempt: str = "null",
    create_active_attempt: bool = False,
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
    if create_active_attempt:
        attempt_dir = work / "attempts" / focus_attempt
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
            ATTEMPT_TEMPLATE.format(attempt=focus_attempt, item=focus_item),
            encoding="utf-8",
        )
    return project, work


def apply_action(
    work: Path,
    project: Path,
    action: Action,
    payload: JsonObject,
    *,
    failpoint: CommitFailpoint | None = None,
) -> None:
    apply_serialized_action(work, project, action, json.dumps(payload), failpoint=failpoint)


def create_proposal(work: Path, project: Path, value: JsonObject) -> Path:
    return create_serialized_proposal(work, project, json.dumps(value))
