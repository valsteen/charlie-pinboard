import re
from pathlib import Path
from typing import Final

PATH_IDENTITY_PATTERN: Final = re.compile(r"^[A-Za-z0-9]+(?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class PathIdentityError(ValueError):
    pass


def confined_path(work_root: Path, path: Path) -> Path:
    root = work_root.resolve(strict=False)
    target = path.resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise PathIdentityError(f"Path '{path}' escapes '{work_root}'.") from error
    return path


def identity_child(work_root: Path, directory: Path, identity: str) -> Path:
    if PATH_IDENTITY_PATTERN.fullmatch(identity) is None:
        raise PathIdentityError(f"Unsafe path identity '{identity}'.")
    confined_path(work_root, directory)
    root = directory.resolve(strict=False)
    target = (directory / identity).resolve(strict=False)
    if target.parent != root:
        raise PathIdentityError(f"Path identity '{identity}' escapes '{directory}'.")
    return confined_path(work_root, directory / identity)


def lock_path_for(work_root: Path) -> Path:
    return work_root.parent / f".{work_root.name}.repo-work.lock"


def journal_path_for(work_root: Path) -> Path:
    return work_root.parent / f".{work_root.name}.repo-work-journal"
