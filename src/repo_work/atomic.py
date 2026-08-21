import fcntl
import os
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from repo_work.storage_layout import lock_path_for

SUPPORTED_PLATFORMS: Final = frozenset({"darwin", "linux"})


class PlatformNotSupportedError(RuntimeError):
    pass


def _require_supported_platform() -> None:
    if sys.platform not in SUPPORTED_PLATFORMS:
        supported = ", ".join(sorted(SUPPORTED_PLATFORMS))
        raise PlatformNotSupportedError(f"PLATFORM_NOT_SUPPORTED: Charlie supports {supported}; found {sys.platform}.")


@contextmanager
def transition_lock(work_root: Path) -> Generator[None]:
    _require_supported_platform()
    lock_path = lock_path_for(work_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write(path, text.encode())


def atomic_create(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
