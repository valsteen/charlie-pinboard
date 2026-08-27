import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from charlie_pinboard.adapters.files.errors import RootError
from charlie_pinboard.adapters.files.root import resolve_project_root
from charlie_pinboard.interfaces.cli import main


class RootResolutionTest(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

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

        original_exclude = (repository / ".git" / "info" / "exclude").read_bytes()
        with chdir(linked):
            self.assertEqual(0, main(("init",)))
        self.assertEqual(0, main(("--project-root", str(linked), "init")))
        self.assertTrue((repository / ".codex" / "pinboard" / "state.sqlite3").is_file())
        self.assertFalse((linked / ".codex" / "pinboard").exists())
        self.assertEqual(
            original_exclude + b"/.codex/pinboard/\n",
            (repository / ".git" / "info" / "exclude").read_bytes(),
        )
        (linked / ".codex").mkdir()
        (linked / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
        self.assertEqual("?? .codex/config.toml\n", self.run_git(linked, "status", "--short", "--untracked-files=all"))

    def test_rejects_non_git_directory(self) -> None:
        directory = Path(tempfile.mkdtemp())

        with self.assertRaisesRegex(RootError, "PROJECT_GIT_ROOT_UNAVAILABLE"):
            resolve_project_root(directory)


if __name__ == "__main__":
    unittest.main()
