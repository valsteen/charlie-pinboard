"""Deferred portable-copy evidence, excluded from the installed package.

The prototype preserves the tested relocation behavior until an accepted product
brief supplies a real installed consumer.
"""

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import msgspec

from pinboard.adapters.files.artifacts import verify_reference, write_revision
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.views import rebuild_state
from pinboard.adapters.sqlite.database import open_database, write_transaction
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.domain.authority_models import AttemptLeaseStatus


class PortableCopyErrorCode(Enum):
    PORTABLE_COPY_DESTINATION_EXISTS = "PORTABLE_COPY_DESTINATION_EXISTS"
    PORTABLE_COPY_DESTINATION_INVALID = "PORTABLE_COPY_DESTINATION_INVALID"
    PORTABLE_COPY_SOURCE_NOT_QUIESCENT = "PORTABLE_COPY_SOURCE_NOT_QUIESCENT"
    STORAGE_INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    STORAGE_IO_ERROR = "STORAGE_IO_ERROR"
    WORK_STATE_INVALID = "WORK_STATE_INVALID"


class PortableCopyError(RuntimeError):
    code: PortableCopyErrorCode

    def __init__(self, code: PortableCopyErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class PortableCopyReceipt:
    source_revision: int
    destination_revision: int
    source_host_epoch: int
    destination_host_epoch: int
    artifacts_copied: int


class _PortableMetadata(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    host_epoch: int


def _backup_database(source: Path, destination: Path) -> None:
    source_connection = open_database(source, OpenMode.READ_ONLY)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _quiescent_source(store: SQLiteWorkStore) -> None:
    state = store.snapshot()
    coordination = state.authority.coordination
    if coordination is not None and coordination.state == work_models.CoordinationLeaseStatus.ACTIVE:
        raise PortableCopyError(
            PortableCopyErrorCode.PORTABLE_COPY_SOURCE_NOT_QUIESCENT,
            "The source has active coordination authority.",
        )
    if any(lease.state == AttemptLeaseStatus.ACTIVE for lease in state.authority.attempt_leases):
        raise PortableCopyError(
            PortableCopyErrorCode.PORTABLE_COPY_SOURCE_NOT_QUIESCENT,
            "The source has active attempt authority.",
        )


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise PortableCopyError(
            PortableCopyErrorCode.STORAGE_IO_ERROR,
            f"Portable-copy path could not be inspected: {path}",
        ) from error
    return True


def _copy_artifacts(source_work_root: Path, staging_roots: DurableRoots) -> int:
    state = SQLiteWorkStore(source_work_root / "state.sqlite3").snapshot()
    for reference in state.artifact_references:
        verify_reference(source_work_root, reference)
        source = source_work_root / reference.selector
        try:
            content = source.read_bytes()
        except OSError as error:
            raise PortableCopyError(
                PortableCopyErrorCode.STORAGE_INVARIANT_VIOLATION,
                f"Referenced artifact could not be copied: {reference.selector}",
            ) from error
        published = write_revision(
            staging_roots,
            NewArtifact(reference.kind, reference.key, reference.revision, source.suffix, content),
        )
        if (
            published.selector,
            published.content_sha256,
            published.size_bytes,
        ) != (
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
        ):
            raise PortableCopyError(
                PortableCopyErrorCode.STORAGE_INVARIANT_VIOLATION,
                f"Copied artifact changed identity: {reference.selector}",
            )
    return len(state.artifact_references)


def _neutralize(database: Path, now: datetime) -> tuple[int, int, int, int]:
    connection = open_database(database, OpenMode.READ_WRITE)
    try:
        row = connection.execute("SELECT revision, host_epoch FROM project_meta").fetchone()
        if row is None:
            raise PortableCopyError(
                PortableCopyErrorCode.WORK_STATE_INVALID,
                "The copied database has no project metadata.",
            )
        try:
            metadata = msgspec.convert(dict(row), type=_PortableMetadata, strict=True)
        except msgspec.ValidationError as error:
            raise PortableCopyError(
                PortableCopyErrorCode.WORK_STATE_INVALID,
                f"The copied database has invalid metadata: {error}",
            ) from error
        source_revision = metadata.revision
        source_host_epoch = metadata.host_epoch
        destination_revision = source_revision
        destination_host_epoch = source_host_epoch + 1
        with write_transaction(connection):
            connection.execute("UPDATE coordination_lease SET status = 'released' WHERE status = 'active'")
            connection.execute("UPDATE attempt_leases SET status = 'released' WHERE status = 'active'")
            connection.execute(
                "UPDATE project_meta SET host_epoch = ?, updated_at = ? WHERE singleton = 1",
                (destination_host_epoch, now.isoformat()),
            )
        return source_revision, destination_revision, source_host_epoch, destination_host_epoch
    finally:
        connection.close()


def _sync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, child_directories, files in os.walk(root):
        directory = Path(current)
        directories.append(directory)
        for name in files:
            descriptor = os.open(directory / name, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        child_directories.sort()
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish(staging: Path, destination: Path) -> None:
    if _exists(destination):
        raise PortableCopyError(
            PortableCopyErrorCode.PORTABLE_COPY_DESTINATION_EXISTS,
            f"Portable-copy destination already exists: {destination}",
        )
    try:
        staging.rename(destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except PortableCopyError:
        raise
    except OSError as error:
        raise PortableCopyError(
            PortableCopyErrorCode.STORAGE_IO_ERROR,
            "The portable-copy directory could not be published.",
        ) from error


def create_portable_copy(source_work_root: Path, destination_work_root: Path) -> PortableCopyReceipt:
    """Publish one neutralized, relocated SQLite work root from a quiescent source."""

    source = source_work_root.absolute()
    destination = destination_work_root.absolute()
    if _exists(destination):
        raise PortableCopyError(
            PortableCopyErrorCode.PORTABLE_COPY_DESTINATION_EXISTS,
            f"Portable-copy destination already exists: {destination}",
        )
    try:
        resolved_source = source.resolve(strict=True)
        parent = destination.parent.resolve(strict=True)
    except OSError as error:
        raise PortableCopyError(
            PortableCopyErrorCode.STORAGE_IO_ERROR,
            "The portable-copy destination parent is unavailable.",
        ) from error
    if not parent.is_dir():
        raise PortableCopyError(
            PortableCopyErrorCode.STORAGE_IO_ERROR,
            "The portable-copy destination parent is not a directory.",
        )
    if parent == resolved_source or resolved_source in parent.parents:
        raise PortableCopyError(
            PortableCopyErrorCode.PORTABLE_COPY_DESTINATION_INVALID,
            "The portable-copy destination must be outside the source work root.",
        )

    source_store = SQLiteWorkStore(source / "state.sqlite3")
    _quiescent_source(source_store)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.portable-stage-", dir=parent))
    staging_roots = DurableRoots(parent, (staging.name,))
    published = False
    try:
        _backup_database(source / "state.sqlite3", staging_roots.database_path)
        artifacts_copied = _copy_artifacts(source, staging_roots)
        source_revision, destination_revision, source_host_epoch, destination_host_epoch = _neutralize(
            staging_roots.database_path,
            datetime.now(UTC),
        )
        destination_store = SQLiteWorkStore(staging_roots.database_path)
        destination_state = destination_store.snapshot()
        for reference in destination_state.artifact_references:
            verify_reference(staging, reference)
        rebuilt = rebuild_state(destination_state, staging, now=datetime.now(UTC))
        if rebuilt.warning is not None:
            raise PortableCopyError(PortableCopyErrorCode.STORAGE_IO_ERROR, rebuilt.warning.message)
        _sync_tree(staging)
        _publish(staging, destination)
        published = True
        return PortableCopyReceipt(
            source_revision,
            destination_revision,
            source_host_epoch,
            destination_host_epoch,
            artifacts_copied,
        )
    except PortableCopyError:
        raise
    except ArtifactError as error:
        raise PortableCopyError(PortableCopyErrorCode(error.code.value), str(error)) from error
    except StorageError as error:
        raise PortableCopyError(PortableCopyErrorCode(error.code.value), str(error)) from error
    except (FileIOError, OSError, sqlite3.Error) as error:
        raise PortableCopyError(PortableCopyErrorCode.STORAGE_IO_ERROR, str(error)) from error
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
