import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DurableRoots:
    anchor: Path
    work_components: tuple[str, ...]

    @property
    def work_root(self) -> Path:
        return self.anchor.joinpath(*self.work_components)

    @property
    def artifacts_root(self) -> Path:
        return self.work_root / "artifacts"

    @property
    def database_path(self) -> Path:
        return self.work_root / "state.sqlite3"


@dataclass(frozen=True, slots=True)
class DurableFile:
    sha256: str
    size: int


class FileIOError(RuntimeError):
    pass


def _verified_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise FileIOError(f"{label} could not be verified: {path}") from error
    if not resolved.is_dir():
        raise FileIOError(f"{label} must be an existing directory: {path}")
    return resolved


def _validate_component(component: str) -> None:
    if component in {"", ".", ".."} or "/" in component or os.sep in component:
        raise FileIOError(f"Invalid durable-root component: {component!r}")


def resolve_durable_roots(project_root: Path, external_work_root: Path | None = None) -> DurableRoots:
    local_work_root = project_root.absolute() / ".codex" / "work"
    if external_work_root is None or external_work_root.absolute() == local_work_root:
        anchor = _verified_directory(project_root, label="Project root")
        return DurableRoots(anchor, (".codex", "work"))

    external = external_work_root.absolute()
    _validate_component(external.name)
    anchor = _verified_directory(external.parent, label="External work-root parent")
    return DurableRoots(anchor, (external.name,))


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise FileIOError(f"Directory could not be synchronized: {path}") from error


def ensure_directory_chain(roots: DurableRoots) -> None:
    current = roots.anchor
    for component in (*roots.work_components, "artifacts"):
        _validate_component(component)
        child = current / component
        try:
            child.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise FileIOError(f"Durable-root component could not be created: {child}") from error
        if child.is_symlink() or not child.is_dir():
            raise FileIOError(f"Durable-root component is not a real directory: {child}")
        _sync_directory(current)
        current = child


def ensure_child_directory(parent: Path, component: str) -> Path:
    """Create or re-observe one real child directory and durably record its entry."""

    _validate_component(component)
    verified_parent = _verified_directory(parent, label="Durable parent")
    child = verified_parent / component
    try:
        child.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        raise FileIOError(f"Durable directory could not be created: {child}") from error
    if child.is_symlink() or not child.is_dir():
        raise FileIOError(f"Durable directory is not a real directory: {child}")
    _sync_directory(verified_parent)
    return child


def _write_and_sync(path: Path, content: bytes) -> DurableFile:
    digest = sha256()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        digest.update(content)
    finally:
        os.close(descriptor)
    return DurableFile(digest.hexdigest(), len(content))


def _cleanup_staging(path: Path, parent: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
    with suppress(FileIOError):
        _sync_directory(parent)


def create_immutable(path: Path, content: bytes) -> DurableFile:
    parent = _verified_directory(path.parent, label="Immutable-file parent")
    staging = parent / f".pinboard-stage-{secrets.token_hex(16)}"
    try:
        result = _write_and_sync(staging, content)
        try:
            os.link(staging, path, follow_symlinks=False)
        except FileExistsError as error:
            raise FileIOError(f"Immutable file already exists: {path}") from error
        _sync_directory(parent)
        _cleanup_staging(staging, parent)
        return result
    except FileIOError:
        raise
    except OSError as error:
        raise FileIOError(f"Immutable file could not be published: {path}") from error
    finally:
        _cleanup_staging(staging, parent)


def atomic_replace(path: Path, content: bytes) -> DurableFile:
    parent = _verified_directory(path.parent, label="Replacement-file parent")
    staging = parent / f".pinboard-stage-{secrets.token_hex(16)}"
    try:
        result = _write_and_sync(staging, content)
        staging.replace(path)
        _sync_directory(parent)
        return result
    except FileIOError:
        raise
    except OSError as error:
        raise FileIOError(f"Replacement file could not be published: {path}") from error
    finally:
        _cleanup_staging(staging, parent)
