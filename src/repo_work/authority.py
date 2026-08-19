from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal

import msgspec

from repo_work.atomic import atomic_write, transition_lock


class AuthorityError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class AuthorityVersion(Enum):
    V1 = "v1"
    V2 = "v2"


class AuthoritySelector(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["repo-work-authority/v1"]
    current: Literal["v2"]
    root: str


@dataclass(frozen=True, slots=True)
class Authority:
    base_work_root: Path
    work_root: Path
    version: AuthorityVersion


def _relative_root(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise AuthorityError("AUTHORITY_SELECTOR_INVALID", f"Authority root '{value}' must stay inside the work root.")
    return relative


def _selected_root(base_work_root: Path, relative: PurePosixPath) -> Path:
    base = base_work_root.resolve()
    candidate = base.joinpath(*relative.parts)
    try:
        selected = candidate.resolve(strict=True)
    except OSError as error:
        raise AuthorityError("AUTHORITY_ROOT_MISSING", f"Authority root '{candidate}' does not exist.") from error
    try:
        selected.relative_to(base)
    except ValueError as error:
        raise AuthorityError(
            "AUTHORITY_SELECTOR_INVALID",
            f"Authority root '{candidate}' resolves outside the work root.",
        ) from error
    if candidate != selected:
        raise AuthorityError("AUTHORITY_SELECTOR_INVALID", f"Authority root '{candidate}' must not be a symlink.")
    if not selected.is_dir():
        raise AuthorityError("AUTHORITY_ROOT_MISSING", f"Authority root '{candidate}' does not exist.")
    return selected


def write_authority_selector(base_work_root: Path, version: AuthorityVersion, root: str) -> None:
    if version != AuthorityVersion.V2:
        raise AuthorityError("AUTHORITY_SELECTOR_INVALID", "Only schema v2 may be selected explicitly.")
    base = base_work_root.resolve()
    relative = _relative_root(root)
    _selected_root(base, relative)
    selector = AuthoritySelector(schema="repo-work-authority/v1", current="v2", root=str(relative))
    encoded = msgspec.json.encode(selector, order="sorted")
    atomic_write(base / "authority.json", msgspec.json.format(encoded, indent=2) + b"\n")


def resolve_authority(base_work_root: Path) -> Authority:
    base = base_work_root.resolve()
    selector_path = base / "authority.json"
    if not selector_path.is_file():
        return Authority(base, base, AuthorityVersion.V1)
    try:
        selector = msgspec.json.decode(selector_path.read_bytes(), type=AuthoritySelector)
    except (OSError, msgspec.DecodeError) as error:
        raise AuthorityError("AUTHORITY_SELECTOR_INVALID", f"Cannot read '{selector_path}': {error}") from error
    relative = _relative_root(selector.root)
    current = _selected_root(base, relative)
    return Authority(base, current, AuthorityVersion.V2)


@contextmanager
def authority_transaction(base_work_root: Path) -> Generator[Authority]:
    base = base_work_root.resolve()
    with transition_lock(base):
        yield resolve_authority(base)
