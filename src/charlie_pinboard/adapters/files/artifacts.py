import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from charlie_pinboard.adapters.files.file_io import (
    DurableRoots,
    FileIOError,
    create_immutable,
    ensure_child_directory,
    ensure_directory_chain,
)
from charlie_pinboard.application.artifacts import ArtifactRef, NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind, ArtifactReference

_DIRECTORIES: dict[ArtifactKind, str] = {
    ArtifactKind.REQUIREMENTS: "requirements",
    ArtifactKind.PLAN: "plans",
    ArtifactKind.DESIGN: "designs",
    ArtifactKind.BRIEF: "briefs",
    ArtifactKind.RESULT: "results",
    ArtifactKind.BLOCKER: "blockers",
    ArtifactKind.EVIDENCE: "evidence",
}


class ArtifactError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _identity(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or os.sep in value or "\x00" in value:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", f"Artifact {label} is not a stable path component.")
    return value


def _suffix(value: str) -> str:
    if not value.startswith(".") or value in {".", ".."} or "/" in value or os.sep in value or "\x00" in value:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact suffix is not canonical.")
    return value


def _selector(kind: ArtifactKind, key: str, revision: int, suffix: str) -> str:
    if revision < 1:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact revision must be positive.")
    return PurePosixPath(
        "artifacts", _DIRECTORIES[kind], _identity(key, label="key"), f"{revision}{_suffix(suffix)}"
    ).as_posix()


def _reference_values(reference: ArtifactRef | ArtifactReference) -> tuple[ArtifactKind, str, int, str, str, int]:
    return (
        reference.kind,
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
    )


def _canonical_reference(reference: ArtifactRef | ArtifactReference) -> Path:
    kind, key, revision, selector, _digest, _size = _reference_values(reference)
    pure = PurePosixPath(selector)
    parts = pure.parts
    if pure.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact selector is not canonical.")
    if len(parts) != 4 or parts[:3] != ("artifacts", _DIRECTORIES[kind], _identity(key, label="key")):
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact selector does not match its identity.")
    filename = parts[3]
    prefix, separator, suffix = filename.partition(".")
    if not separator or prefix != str(revision) or not suffix:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact selector does not match its revision.")
    return Path(*parts)


def _read_regular_no_follow(work_root: Path, relative: Path) -> bytes:
    current = work_root
    try:
        root_stat = current.lstat()
    except OSError as error:
        raise ArtifactError("STORAGE_IO_ERROR", "Artifact work root could not be inspected.") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact work root is not a real directory.")
    for component in relative.parts[:-1]:
        current = current / component
        try:
            status = current.lstat()
        except OSError as error:
            raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact directory chain is incomplete.") from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact directory chain is not real.")
    path = current / relative.name
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact selector is not a regular file.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(descriptor)
    except ArtifactError:
        raise
    except OSError as error:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact bytes could not be read safely.") from error


def verify_reference(work_root: Path, reference: ArtifactRef | ArtifactReference) -> None:
    relative = _canonical_reference(reference)
    _kind, _key, _revision, _selector_value, expected_digest, expected_size = _reference_values(reference)
    data = _read_regular_no_follow(work_root, relative)
    if len(data) != expected_size or sha256(data).hexdigest() != expected_digest:
        raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Artifact size or digest does not match its reference.")


def write_revision(roots: DurableRoots, artifact: NewArtifact) -> ArtifactRef:
    selector = _selector(artifact.kind, artifact.key, artifact.revision, artifact.suffix)
    digest = sha256(artifact.content).hexdigest()
    reference = ArtifactRef(artifact.kind, artifact.key, artifact.revision, selector, digest, len(artifact.content))
    try:
        ensure_directory_chain(roots)
        kind_root = ensure_child_directory(roots.artifacts_root, _DIRECTORIES[artifact.kind])
        ensure_child_directory(kind_root, artifact.key)
        path = roots.work_root / selector
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                created = create_immutable(path, artifact.content)
            except FileIOError:
                try:
                    verify_reference(roots.work_root, reference)
                except ArtifactError as collision:
                    raise ArtifactError(
                        "STORAGE_INVARIANT_VIOLATION",
                        "Artifact revision could not be published immutably.",
                    ) from collision
            else:
                if (created.sha256, created.size) != (reference.content_sha256, reference.size_bytes):
                    raise ArtifactError("STORAGE_INVARIANT_VIOLATION", "Published artifact facts changed unexpectedly.")
        else:
            verify_reference(roots.work_root, reference)
        return reference
    except ArtifactError:
        raise
    except FileIOError as error:
        raise ArtifactError("STORAGE_IO_ERROR", str(error)) from error


@dataclass(frozen=True, slots=True)
class ArtifactRepository:
    """Concrete durable artifact access used by interface composition."""

    roots: DurableRoots

    @property
    def work_root(self) -> Path:
        return self.roots.work_root

    def verify(self, reference: ArtifactReference) -> None:
        verify_reference(self.work_root, reference)

    def path(self, reference: ArtifactReference) -> Path:
        return self.work_root / _canonical_reference(reference)

    def publish(self, artifact: NewArtifact) -> ArtifactRef:
        return write_revision(self.roots, artifact)
