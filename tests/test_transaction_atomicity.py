import contextlib
import io
import json
import multiprocessing
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_work import transaction_store
from repo_work.actions import Action, actions_for
from repo_work.authority import resolve_authority
from repo_work.cli import main
from repo_work.leases import acquire_coordination
from repo_work.markdown import parse_queue
from repo_work.migration import migrate_to_v2
from repo_work.model import WorkState
from repo_work.transaction_store import CommitFailpoint, FileChange, journal_path_for
from repo_work.transition import TransitionError
from repo_work.validate import validate_work_state

from .support import JsonObject, apply_action, create_state

ACTIVATE_PAYLOAD: JsonObject = {
    "attempt": "reveal-core-1",
    "branch": "codex/reveal-core",
    "base_revision": "abc123",
    "owner": "worker-task",
}


def _snapshot(work: Path) -> dict[str, bytes]:
    return {str(path.relative_to(work)): path.read_bytes() for path in work.rglob("*") if path.is_file()}


def _authoritative_snapshot(work: Path) -> dict[str, bytes]:
    return {path: data for path, data in _snapshot(work).items() if ".repo-work-journal.retired." not in path}


def _action(work: Path, project: Path, action_id: str) -> Action:
    return next(
        candidate for candidate in actions_for(work, project, role="coordinator") if candidate.action_id == action_id
    )


def _race_transition(work: str, project: str, action: Action, result: str) -> None:
    try:
        apply_action(Path(work), Path(project), action, ACTIVATE_PAYLOAD)
        outcome = "success"
    except TransitionError as error:
        outcome = error.code
    Path(result).write_text(outcome, encoding="utf-8")


def _crash_transition(work: str, project: str, action: Action, boundary: int) -> None:
    def crashpoint(current: int, _change: FileChange) -> None:
        if current == boundary:
            os._exit(73)

    apply_action(Path(work), Path(project), action, ACTIVATE_PAYLOAD, failpoint=crashpoint)


def _crash_journal_retirement(work: str, project: str, action: Action, recover: bool) -> None:
    def crash_after_retirement(_path: Path) -> None:
        os._exit(74)

    with patch("repo_work.transaction_store._delete_retired_journal", side_effect=crash_after_retirement):
        if recover:
            transaction_store.recover_pending_commit(resolve_authority(Path(work)).work_root)
        else:
            apply_action(Path(work), Path(project), action, ACTIVATE_PAYLOAD)


def _fail_at(selected: int) -> CommitFailpoint:
    def failpoint(boundary: int, _change: FileChange) -> None:
        if boundary == selected:
            raise RuntimeError("injected commit failure")

    return failpoint


class TransactionAtomicityTest(unittest.TestCase):
    def test_activate_failpoint_restores_every_write_boundary(self) -> None:
        for selected_boundary in range(1, 4):
            with self.subTest(boundary=selected_boundary):
                project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
                action = _action(work, project, "activate:reveal-core")
                before = _snapshot(work)

                with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
                    apply_action(work, project, action, ACTIVATE_PAYLOAD, failpoint=_fail_at(selected_boundary))

                self.assertEqual(before, _snapshot(work))
                self.assertFalse(journal_path_for(work).exists())

    def test_complete_failpoint_restores_every_write_and_delete_boundary(self) -> None:
        for selected_boundary in range(1, 6):
            with self.subTest(boundary=selected_boundary):
                project, work = create_state(
                    ["| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |"],
                    focus_item="reveal-core",
                    focus_attempt="reveal-core-1",
                    create_active_attempt=True,
                )
                action = _action(work, project, "complete:reveal-core-1")
                before = _snapshot(work)

                with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
                    apply_action(
                        work,
                        project,
                        action,
                        {"evidence": "accepted review"},
                        failpoint=_fail_at(selected_boundary),
                    )

                self.assertEqual(before, _snapshot(work))
                self.assertFalse(journal_path_for(work).exists())

    def test_v2_complete_failpoint_restores_active_history_and_dependency_boundaries(self) -> None:
        for selected_boundary in range(1, 6):
            with self.subTest(boundary=selected_boundary):
                project, work = create_state(
                    [
                        "| reveal-core | active | — | — | reveal-core-1 | design | continue | Active. |",
                        "| dependent | blocked | — | reveal-core | — | design | none | Waiting. |",
                    ],
                    focus_item="reveal-core",
                    focus_attempt="reveal-core-1",
                    create_active_attempt=True,
                )
                migrate_to_v2(work, project)
                coordination = acquire_coordination(work, "reviewer", "host", 60)
                action = next(
                    candidate
                    for candidate in actions_for(
                        work,
                        project,
                        "coordinator",
                        lease_id=coordination.lease_id,
                        generation=coordination.generation,
                    )
                    if candidate.action_id == "complete:reveal-core-1"
                )
                before = _snapshot(work)

                with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
                    apply_action(
                        work,
                        project,
                        action,
                        {"evidence": "accepted review"},
                        failpoint=_fail_at(selected_boundary),
                    )

                self.assertEqual(before, _snapshot(work))
                self.assertFalse(journal_path_for(work / "v2").exists())

    def test_interrupted_process_is_recovered_from_durable_journal(self) -> None:
        for authority in ("v1", "v2"):
            for boundary in range(1, 4):
                with self.subTest(authority=authority, boundary=boundary):
                    project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
                    if authority == "v2":
                        migrate_to_v2(work, project)
                        lease = acquire_coordination(work, "task", "host", 60)
                        action = next(
                            candidate
                            for candidate in actions_for(
                                work,
                                project,
                                "coordinator",
                                lease_id=lease.lease_id,
                                generation=lease.generation,
                            )
                            if candidate.action_id == "activate:reveal-core"
                        )
                        active_root = work / "v2"
                    else:
                        action = _action(work, project, "activate:reveal-core")
                        active_root = work
                    before = _snapshot(work)
                    process = multiprocessing.get_context("fork").Process(
                        target=_crash_transition, args=(str(work), str(project), action, boundary)
                    )

                    process.start()
                    process.join(timeout=10)

                    self.assertEqual(73, process.exitcode)
                    self.assertTrue(journal_path_for(active_root).exists())
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        result = main(
                            (
                                "--project-root",
                                str(project),
                                "--work-root",
                                str(work),
                                "recover",
                                "--json",
                            )
                        )
                    self.assertEqual(0, result)
                    self.assertTrue(json.loads(stdout.getvalue())["recovered"])
                    self.assertEqual(before, _snapshot(work))
                    self.assertFalse(journal_path_for(active_root).exists())

    def test_journal_retirement_is_atomic_after_commit_and_recovery(self) -> None:
        for authority in ("v1", "v2"):
            for phase in ("commit", "recovery"):
                with self.subTest(authority=authority, phase=phase):
                    project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
                    if authority == "v2":
                        migrate_to_v2(work, project)
                        lease = acquire_coordination(work, "task", "host", 60)
                        action = next(
                            candidate
                            for candidate in actions_for(
                                work,
                                project,
                                "coordinator",
                                lease_id=lease.lease_id,
                                generation=lease.generation,
                            )
                            if candidate.action_id == "activate:reveal-core"
                        )
                        active_root = work / "v2"
                    else:
                        action = _action(work, project, "activate:reveal-core")
                        active_root = work
                    before = _snapshot(work)
                    if phase == "recovery":
                        interrupted = multiprocessing.get_context("fork").Process(
                            target=_crash_transition,
                            args=(str(work), str(project), action, 1),
                        )
                        interrupted.start()
                        interrupted.join(timeout=10)
                        self.assertEqual(73, interrupted.exitcode)
                        self.assertTrue(journal_path_for(active_root).exists())

                    retiring = multiprocessing.get_context("fork").Process(
                        target=_crash_journal_retirement,
                        args=(str(work), str(project), action, phase == "recovery"),
                    )
                    retiring.start()
                    retiring.join(timeout=10)

                    self.assertEqual(74, retiring.exitcode)
                    self.assertFalse(journal_path_for(active_root).exists())
                    self.assertTrue(validate_work_state(work, project).valid)
                    expected = WorkState.READY if phase == "recovery" else WorkState.ACTIVE
                    self.assertEqual(expected, parse_queue(active_root / "queue.md").items[0].state)
                    if phase == "recovery":
                        self.assertEqual(before, _authoritative_snapshot(work))
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        result = main(
                            (
                                "--project-root",
                                str(project),
                                "--work-root",
                                str(work),
                                "recover",
                                "--json",
                            )
                        )
                    self.assertEqual(0, result)
                    self.assertFalse(json.loads(stdout.getvalue())["recovered"])

    def test_two_processes_race_one_revision_and_one_receives_stale_outcome(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = _action(work, project, "activate:reveal-core")
        result_one = project / "result-one.txt"
        result_two = project / "result-two.txt"
        context = multiprocessing.get_context("fork")
        processes = (
            context.Process(
                target=_race_transition,
                args=(str(work), str(project), action, str(result_one)),
            ),
            context.Process(
                target=_race_transition,
                args=(str(work), str(project), action, str(result_two)),
            ),
        )

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(0, process.exitcode)

        outcomes = {result_one.read_text(encoding="utf-8"), result_two.read_text(encoding="utf-8")}
        self.assertEqual({"success", "STATE_REVISION_STALE"}, outcomes)
        self.assertTrue(validate_work_state(work, project).valid)


if __name__ == "__main__":
    unittest.main()
