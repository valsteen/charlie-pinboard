import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from charlie_pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.files.models import AffectedViews
from charlie_pinboard.adapters.files.views import rebuild, refresh
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from tests.support import SQLITE_NOW, complete_sqlite_state


class GeneratedViewsTest(unittest.TestCase):
    def _state(self) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(complete_sqlite_state())
        return roots.work_root, store

    def test_rebuild_creates_revision_stamped_non_authoritative_views(self) -> None:
        work_root, store = self._state()

        result = rebuild(store, work_root)

        self.assertIsNone(result.warning)
        for selector in (
            "views/queue.md",
            "views/current.md",
            "views/items/work-a.md",
            "views/attempts/work-a-1.md",
            "views/history.md",
        ):
            text = (work_root / selector).read_text(encoding="utf-8")
            self.assertIn("database_revision: 12", text)
            self.assertIn("Generated projection; SQLite is authoritative.", text)

    def test_post_commit_refresh_failure_is_a_repairable_warning(self) -> None:
        work_root, store = self._state()
        with patch(
            "charlie_pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "disk full"),
        ):
            result = refresh(store, work_root, AffectedViews(current_focus=True))

        self.assertEqual(12, result.database_revision)
        self.assertIsNotNone(result.warning)
        assert result.warning is not None
        self.assertIn("generated views need repair", result.warning.message)
        self.assertIn("pinboard views rebuild", result.warning.repair)


if __name__ == "__main__":
    unittest.main()
