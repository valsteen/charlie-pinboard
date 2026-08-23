from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import cast

from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import StorageError, initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.decision_projection import project_decision_snapshot
from charlie_pinboard.domain.decisions import (
    Action,
    ActionKind,
    ActorAuthority,
    AuthorizationKind,
    Decision,
    Role,
    TransitionCommand,
    available_actions,
    bind_transition,
    decide,
)
from charlie_pinboard.domain.model import ReasonInput
from tests.support import SQLITE_NOW, complete_sqlite_state


def _commit_same_pause(
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    snapshot = project_decision_snapshot(store.snapshot())
    actor = ActorAuthority(Role.COORDINATOR, AuthorizationKind.COORDINATOR, snapshot.generation)
    actions = cast(tuple[Action, ...], available_actions(snapshot, actor))
    action = next(value for value in actions if value.kind == ActionKind.PAUSE)
    command = cast(TransitionCommand, bind_transition(action, ReasonInput("Concurrent pause.")))
    decision = cast(Decision, decide(snapshot, command, SQLITE_NOW))
    barrier.wait()
    try:
        with store.write() as transaction:
            transaction.commit(decision)
    except StorageError as error:
        results.put(error.code.value)
    else:
        results.put("committed")


class SQLiteConcurrencyTest(unittest.TestCase):
    def test_concurrent_same_action_commits_once_and_rejects_stale_writer(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(target=_commit_same_pause, args=(str(roots.database_path), barrier, results))
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        self.assertEqual(13, store.snapshot().lifecycle.project.revision)


if __name__ == "__main__":
    unittest.main()
