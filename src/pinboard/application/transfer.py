"""Portable-copy production workflow awaiting a future installed CLI route.

This module is intentionally unreachable from the current command surface. Treat
``create_portable_copy`` and the production code that supports it as retained
work in progress: lack of callers is not grounds to delete or relocate it. The
implementation may be refactored or simplified as normal production code while
its tested portable-copy behavior remains intact.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import msgspec

from pinboard.adapters.files.artifacts import verify_reference, write_revision
from pinboard.adapters.files.errors import ArtifactError, FileIOError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.views import rebuild
from pinboard.adapters.sqlite.database import (
    backup_database,
    open_database,
    write_transaction,
)
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.application.errors import PortableCopyError, PortableCopyErrorCode
from pinboard.domain import work_models
from pinboard.domain.authority_models import AttemptLeaseStatus


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


def _canonical_json(value: dict[str, int]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
        destination_revision = source_revision + 1
        destination_host_epoch = source_host_epoch + 1
        history_row = connection.execute("SELECT COALESCE(MAX(history_id), 0) FROM transition_history").fetchone()
        if history_row is None:
            raise PortableCopyError(
                PortableCopyErrorCode.WORK_STATE_INVALID,
                "The copied database history could not be read.",
            )
        history_value = cast("int | float | str | bytes | None", history_row[0])
        try:
            history_id = msgspec.convert(history_value, type=int, strict=True) + 1
        except msgspec.ValidationError as error:
            raise PortableCopyError(
                PortableCopyErrorCode.WORK_STATE_INVALID,
                f"The copied database has invalid history metadata: {error}",
            ) from error
        with write_transaction(connection):
            connection.execute("UPDATE coordination_lease SET status = 'released' WHERE status = 'active'")
            connection.execute("UPDATE attempt_leases SET status = 'released' WHERE status = 'active'")
            connection.execute(
                "UPDATE project_meta SET revision = ?, host_epoch = ?, updated_at = ? WHERE singleton = 1",
                (destination_revision, destination_host_epoch, now.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO transition_history (
                    history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                    artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
                    input_json, outcome_schema, outcome_json, committed_at
                ) VALUES (?, ?, ?, 'portable-copy', 'ledger', NULL, NULL, 'portable-copy',
                          'portable-copy', NULL, 'portable-copy-input/v1', ?,
                          'portable-copy-outcome/v1', ?, ?)
                """,
                (
                    history_id,
                    destination_revision,
                    f"portable-copy:{destination_host_epoch}",
                    _canonical_json(
                        {
                            "source_host_epoch": source_host_epoch,
                            "source_revision": source_revision,
                        }
                    ),
                    _canonical_json(
                        {
                            "destination_host_epoch": destination_host_epoch,
                            "destination_revision": destination_revision,
                        }
                    ),
                    now.isoformat(),
                ),
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
        backup_database(source / "state.sqlite3", staging_roots.database_path)
        artifacts_copied = _copy_artifacts(source, staging_roots)
        source_revision, destination_revision, source_host_epoch, destination_host_epoch = _neutralize(
            staging_roots.database_path,
            datetime.now(UTC),
        )
        destination_store = SQLiteWorkStore(staging_roots.database_path)
        destination_state = destination_store.snapshot()
        for reference in destination_state.artifact_references:
            verify_reference(staging, reference)
        rebuilt = rebuild(destination_store, staging)
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
    except (FileIOError, OSError) as error:
        raise PortableCopyError(PortableCopyErrorCode.STORAGE_IO_ERROR, str(error)) from error
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
