import subprocess
from pathlib import Path

from charlie_pinboard.adapters.files.errors import RootError, RootErrorCode


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
            RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE,
            result.stderr.strip() or f"'{cwd}' is not inside a Git repository.",
        )
    common_directory = Path(result.stdout.strip()).resolve()
    if common_directory.name != ".git":
        raise RootError(
            RootErrorCode.PROJECT_GIT_LAYOUT_UNSUPPORTED,
            f"Expected the shared Git directory to end in '.git', found '{common_directory}'.",
        )
    return common_directory.parent
