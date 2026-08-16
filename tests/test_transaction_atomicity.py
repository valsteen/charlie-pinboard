import multiprocessing
import os
import unittest
from pathlib import Path

from repo_work.actions import Action, actions_for
from repo_work.atomic import transition_lock
from repo_work.transaction_store import CommitFailpoint, FileChange, journal_path_for, recover_pending_commit
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

    def test_interrupted_process_is_recovered_from_durable_journal(self) -> None:
        project, work = create_state(["| reveal-core | ready | — | — | — | design | activate | Ready. |"])
        action = _action(work, project, "activate:reveal-core")
        before = _snapshot(work)
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_crash_transition, args=(str(work), str(project), action, 2))

        process.start()
        process.join(timeout=10)

        self.assertEqual(73, process.exitcode)
        self.assertTrue(journal_path_for(work).exists())
        with transition_lock(work):
            self.assertTrue(recover_pending_commit(work))
        self.assertEqual(before, _snapshot(work))
        self.assertFalse(journal_path_for(work).exists())

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
