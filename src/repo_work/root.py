import subprocess
from pathlib import Path


class RootError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def resolve_project_root(cwd: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RootError(
            "PROJECT_GIT_ROOT_UNAVAILABLE",
            result.stderr.strip() or f"'{cwd}' is not inside a Git repository.",
        )
    common_directory = Path(result.stdout.strip()).resolve()
    if common_directory.name != ".git":
        raise RootError(
            "PROJECT_GIT_LAYOUT_UNSUPPORTED",
            f"Expected the shared Git directory to end in '.git', found '{common_directory}'.",
        )
    return common_directory.parent


def resolve_work_root(cwd: Path) -> Path:
    return resolve_project_root(cwd) / ".codex" / "work"
