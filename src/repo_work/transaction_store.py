import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import msgspec

from repo_work import validate as work_validation
from repo_work.atomic import atomic_write
from repo_work.authority import AuthorityVersion
from repo_work.storage_layout import PathIdentityError, confined_path, journal_path_for


class AtomicCommitError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FileChange:
    path: PurePosixPath
    data: bytes | None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    changes: tuple[FileChange, ...]

    def __post_init__(self) -> None:
        paths: set[PurePosixPath] = set()
        for change in self.changes:
            if change.path.is_absolute() or ".." in change.path.parts or not change.path.parts:
                raise ValueError(f"CHANGE_PATH_INVALID: '{change.path}' must stay inside the work root.")
            if change.path in paths:
                raise ValueError(f"CHANGE_PATH_DUPLICATE: '{change.path}' is changed more than once.")
            paths.add(change.path)

    @classmethod
    def of(cls, *changes: FileChange) -> ChangeSet:
        return cls(tuple(changes))


@dataclass(frozen=True, slots=True)
class OriginalFile:
    path: PurePosixPath
    existed: bool
    data: bytes


class JournalOriginal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    path: Annotated[str, msgspec.Meta(min_length=1)]
    existed: bool
    data: bytes


class JournalManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["repo-work-journal/v1"]
    originals: tuple[JournalOriginal, ...]


type CommitFailpoint = Callable[[int, FileChange], None]


def write_change(path: str, text: str) -> FileChange:
    return FileChange(PurePosixPath(path), text.encode())


def write_bytes_change(path: str, data: bytes) -> FileChange:
    return FileChange(PurePosixPath(path), data)


def delete_change(path: str) -> FileChange:
    return FileChange(PurePosixPath(path), None)


def _target(work_root: Path, relative: PurePosixPath) -> Path:
    path = work_root.joinpath(*relative.parts)
    try:
        return confined_path(work_root, path)
    except PathIdentityError as error:
        raise AtomicCommitError("CHANGE_PATH_INVALID", f"Change path '{relative}' escapes the work root.") from error


def _apply_change(work_root: Path, change: FileChange) -> None:
    path = _target(work_root, change.path)
    if change.data is None:
        path.unlink()
    else:
        atomic_write(path, change.data)


def _prune_empty_parents(path: Path, work_root: Path) -> None:
    parent = path.parent
    while parent != work_root and parent.is_relative_to(work_root):
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _capture_originals(work_root: Path, changes: ChangeSet) -> tuple[OriginalFile, ...]:
    originals: list[OriginalFile] = []
    for change in changes.changes:
        path = _target(work_root, change.path)
        originals.append(OriginalFile(change.path, path.is_file(), path.read_bytes() if path.is_file() else b""))
    return tuple(originals)


def _journal_manifest(originals: tuple[OriginalFile, ...]) -> JournalManifest:
    return JournalManifest(
        schema="repo-work-journal/v1",
        originals=tuple(
            JournalOriginal(
                path=str(original.path),
                existed=original.existed,
                data=original.data,
            )
            for original in originals
        ),
    )


def _write_journal(work_root: Path, originals: tuple[OriginalFile, ...]) -> Path:
    journal = journal_path_for(work_root)
    if journal.exists():
        raise AtomicCommitError("COMMIT_RECOVERY_REQUIRED", f"Pending transaction journal exists at '{journal}'.")
    temporary = Path(tempfile.mkdtemp(prefix=f".{journal.name}.", dir=journal.parent))
    try:
        encoded = msgspec.json.encode(_journal_manifest(originals), order="sorted")
        atomic_write(temporary / "manifest.json", msgspec.json.format(encoded, indent=2) + b"\n")
        temporary.replace(journal)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return journal


def _parse_original(value: JournalOriginal) -> OriginalFile:
    relative = PurePosixPath(value.path)
    if relative.is_absolute() or ".." in relative.parts:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", f"Journal path '{value.path}' escapes the work root.")
    return OriginalFile(relative, value.existed, value.data)


def _read_journal(journal: Path) -> tuple[OriginalFile, ...]:
    try:
        data = (journal / "manifest.json").read_bytes()
        manifest = msgspec.json.decode(data, type=JournalManifest)
    except OSError as error:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", f"Cannot read transaction journal: {error}") from error
    except msgspec.DecodeError as error:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", f"Cannot parse transaction journal: {error}") from error
    return tuple(_parse_original(entry) for entry in manifest.originals)


def _rollback(work_root: Path, originals: tuple[OriginalFile, ...]) -> None:
    for original in reversed(originals):
        path = _target(work_root, original.path)
        if original.existed:
            atomic_write(path, original.data)
        else:
            path.unlink(missing_ok=True)
            _prune_empty_parents(path, work_root)


def recover_pending_commit(work_root: Path) -> bool:
    journal = journal_path_for(work_root)
    if not journal.exists():
        return False
    originals = _read_journal(journal)
    _rollback(work_root, originals)
    shutil.rmtree(journal)
    return True


def validate_change_set(
    work_root: Path,
    project_root: Path,
    changes: ChangeSet,
    version: AuthorityVersion,
) -> None:
    with tempfile.TemporaryDirectory(prefix="repo-work-prospective-") as temporary:
        prospective = Path(temporary) / "work"
        shutil.copytree(work_root, prospective)
        for change in changes.changes:
            _apply_change(prospective, change)
        report = work_validation.validate_work_state_during_commit(prospective, project_root, version)
        if not report.valid:
            raise AtomicCommitError("TRANSITION_POSTCONDITION_FAILED", report.render())


def commit_change_set(
    work_root: Path,
    project_root: Path,
    changes: ChangeSet,
    version: AuthorityVersion,
    *,
    failpoint: CommitFailpoint | None = None,
) -> None:
    originals = _capture_originals(work_root, changes)
    journal = _write_journal(work_root, originals)
    try:
        for boundary, change in enumerate(changes.changes, start=1):
            _apply_change(work_root, change)
            if failpoint is not None:
                failpoint(boundary, change)
        report = work_validation.validate_work_state_during_commit(work_root, project_root, version)
        if not report.valid:
            raise AtomicCommitError("TRANSITION_POSTCONDITION_FAILED", report.render())
    except Exception, KeyboardInterrupt, SystemExit:
        _rollback(work_root, originals)
        shutil.rmtree(journal)
        raise
    shutil.rmtree(journal)
