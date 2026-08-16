import base64
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from attrs import frozen
from cattrs.errors import BaseValidationError

from repo_work import validate as work_validation
from repo_work.atomic import atomic_write
from repo_work.json_codec import JsonCodecError, encode_json, nested_exception, read_json, validation_message
from repo_work.storage_layout import journal_path_for


class AtomicCommitError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@frozen
class FileChange:
    path: PurePosixPath
    data: bytes | None


@frozen
class ChangeSet:
    changes: tuple[FileChange, ...]

    def __attrs_post_init__(self) -> None:
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


@frozen
class OriginalFile:
    path: PurePosixPath
    existed: bool
    data: bytes


@frozen
class JournalOriginal:
    path: str
    existed: bool
    data: str


@frozen
class JournalManifest:
    schema: str
    originals: tuple[JournalOriginal, ...]

    def __attrs_post_init__(self) -> None:
        if self.schema != "repo-work-journal/v1":
            raise AtomicCommitError("COMMIT_JOURNAL_INVALID", "Unsupported transaction journal schema.")


type CommitFailpoint = Callable[[int, FileChange], None]


def write_change(path: str, text: str) -> FileChange:
    return FileChange(PurePosixPath(path), text.encode())


def write_bytes_change(path: str, data: bytes) -> FileChange:
    return FileChange(PurePosixPath(path), data)


def delete_change(path: str) -> FileChange:
    return FileChange(PurePosixPath(path), None)


def _target(work_root: Path, relative: PurePosixPath) -> Path:
    return work_root.joinpath(*relative.parts)


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
                data=base64.b64encode(original.data).decode("ascii"),
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
        atomic_write(temporary / "manifest.json", encode_json(_journal_manifest(originals)))
        temporary.replace(journal)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return journal


def _parse_original(value: JournalOriginal) -> OriginalFile:
    if not value.path:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", "Journal original entry has invalid fields.")
    relative = PurePosixPath(value.path)
    if relative.is_absolute() or ".." in relative.parts:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", f"Journal path '{value.path}' escapes the work root.")
    try:
        decoded = base64.b64decode(value.data, validate=True)
    except ValueError as error:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", f"Journal data for '{value.path}' is not base64.") from error
    return OriginalFile(relative, value.existed, decoded)


def _read_journal(journal: Path) -> tuple[OriginalFile, ...]:
    try:
        manifest = read_json(journal / "manifest.json", JournalManifest)
    except JsonCodecError as error:
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", error.message) from error
    except BaseValidationError as error:
        domain_error = nested_exception(error, AtomicCommitError)
        if domain_error is not None:
            raise domain_error from error
        raise AtomicCommitError("COMMIT_JOURNAL_INVALID", validation_message(error)) from error
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


def validate_change_set(work_root: Path, project_root: Path, changes: ChangeSet) -> None:
    with tempfile.TemporaryDirectory(prefix="repo-work-prospective-") as temporary:
        prospective = Path(temporary) / "work"
        shutil.copytree(work_root, prospective)
        for change in changes.changes:
            _apply_change(prospective, change)
        report = work_validation.validate_work_state(prospective, project_root)
        if not report.valid:
            raise AtomicCommitError("TRANSITION_POSTCONDITION_FAILED", report.render())


def commit_change_set(
    work_root: Path,
    project_root: Path,
    changes: ChangeSet,
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
        report = work_validation.validate_work_state_during_commit(work_root, project_root)
        if not report.valid:
            raise AtomicCommitError("TRANSITION_POSTCONDITION_FAILED", report.render())
    except Exception, KeyboardInterrupt, SystemExit:
        _rollback(work_root, originals)
        shutil.rmtree(journal)
        raise
    shutil.rmtree(journal)
