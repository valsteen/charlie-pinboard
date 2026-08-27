import subprocess
from pathlib import Path

from charlie_pinboard.adapters.files.errors import RootError, RootErrorCode

PINBOARD_GIT_EXCLUDE = b"/.codex/pinboard/"


def _resolve_git_common_directory(cwd: Path) -> Path:
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
    return common_directory


def resolve_project_root(cwd: Path) -> Path:
    return _resolve_git_common_directory(cwd).parent


def ensure_default_git_exclude(project_root: Path) -> None:
    """Exclude only the default Pinboard root from one repository's local status."""

    try:
        common_directory = _resolve_git_common_directory(project_root)
    except RootError as error:
        if error.code == RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE:
            return
        raise
    exclude = common_directory / "info" / "exclude"
    try:
        content = exclude.read_bytes() if exclude.exists() else b""
        if PINBOARD_GIT_EXCLUDE in content.splitlines():
            return
        separator = b"" if not content or content.endswith((b"\n", b"\r")) else b"\n"
        exclude.write_bytes(content + separator + PINBOARD_GIT_EXCLUDE + b"\n")
    except OSError as error:
        raise RootError(
            RootErrorCode.PROJECT_GIT_EXCLUDE_UNAVAILABLE,
            f"Repository-local Git exclude could not be updated: {exclude}",
        ) from error
