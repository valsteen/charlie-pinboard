import subprocess
import tempfile
import unittest
from pathlib import Path

from repo_work.root import RootError, resolve_project_root, resolve_work_root


class RootResolutionTest(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )

    def test_primary_checkout_and_linked_worktree_share_root(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        repository = temporary / "repository"
        linked = temporary / "linked"
        repository.mkdir()
        self.run_git(repository, "init", "-b", "main")
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.run_git(repository, "add", "tracked.txt")
        self.run_git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        self.run_git(repository, "worktree", "add", "-b", "linked", str(linked))

        self.assertEqual(repository.resolve(), resolve_project_root(repository))
        self.assertEqual(repository.resolve(), resolve_project_root(linked))
        self.assertEqual(repository.resolve() / ".codex" / "work", resolve_work_root(linked))

    def test_rejects_non_git_directory(self) -> None:
        directory = Path(tempfile.mkdtemp())

        with self.assertRaisesRegex(RootError, "PROJECT_GIT_ROOT_UNAVAILABLE"):
            resolve_project_root(directory)


if __name__ == "__main__":
    unittest.main()
