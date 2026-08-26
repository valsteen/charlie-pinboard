import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from functools import cache
from pathlib import Path
from urllib.parse import quote

from charlie_pinboard.adapters.files.errors import FileIOError
from charlie_pinboard.adapters.files.file_io import DurableRoots, ensure_directory_chain
from charlie_pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from charlie_pinboard.adapters.sqlite.models import OpenMode

APPLICATION = "charlie-pinboard"
SCHEMA_VERSION = 1
SCHEMA_ID = "sqlite-v1"
BUSY_TIMEOUT_MS = 2_000


def _database_uri(path: Path, mode: OpenMode, *, immutable: bool = False) -> str:
    immutable_query = "&immutable=1" if immutable else ""
    return f"file:{quote(str(path.absolute()), safe='/')}?mode={mode.value}{immutable_query}"


def _translate_database_error(error: sqlite3.Error, *, opening: bool = False) -> StorageError:
    message = str(error).lower()
    if isinstance(error, sqlite3.IntegrityError):
        return StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            "SQLite rejected a relational invariant while persisting a domain decision.",
        )
    if "locked" in message or "busy" in message:
        return StorageError(
            StorageErrorCode.BUSY,
            "SQLite remained busy for the bounded wait; rediscover the action before retrying.",
            retryable=True,
        )
    if any(
        value in message
        for value in ("readonly", "disk i/o", "database or disk is full", "permission", "unable to open database file")
    ):
        return StorageError(StorageErrorCode.IO_ERROR, "SQLite could not durably access the work database.")
    if opening or isinstance(error, sqlite3.DatabaseError):
        return StorageError(StorageErrorCode.INVALID_STATE, "The current SQLite work state is malformed or corrupt.")
    return StorageError(StorageErrorCode.OPERATION_FAILED, "The SQLite storage operation failed.")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or enabled[0] != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "SQLite foreign-key enforcement is unavailable.")


def _configure_writes(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")


def _required_meta(connection: sqlite3.Connection) -> tuple[str, int]:
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_meta'"
        ).fetchone()
        if table is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has no current project metadata.")
        rows = connection.execute("SELECT application, schema_version FROM project_meta").fetchall()
    except sqlite3.Error as error:
        raise _translate_database_error(error, opening=True) from error
    if len(rows) != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain exactly one metadata row.")
    application, version = rows[0]
    if not isinstance(application, str) or not isinstance(version, int) or isinstance(version, bool):
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database metadata types are invalid.")
    return application, version


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    signature: list[tuple[str, str, str, str | None]] = []
    for row in rows:
        object_type, name, table_name, sql = row
        if (
            not isinstance(object_type, str)
            or not isinstance(name, str)
            or not isinstance(table_name, str)
            or (sql is not None and not isinstance(sql, str))
        ):
            raise StorageError(StorageErrorCode.INVALID_STATE, "The current SQLite schema metadata is malformed.")
        signature.append((object_type, name, table_name, sql))
    return tuple(signature)


@cache
def _expected_schema_signature() -> tuple[tuple[str, str, str, str | None], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema_bytes().decode("utf-8"))
        return _schema_signature(connection)
    except StorageError:
        raise
    except (sqlite3.Error, UnicodeError) as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The installed SQLite schema is invalid.") from error
    finally:
        connection.close()


def _verify_current_schema(connection: sqlite3.Connection) -> None:
    application, version = _required_meta(connection)
    if application != APPLICATION:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"The database belongs to application {application!r}, not {APPLICATION!r}.",
        )
    if version != SCHEMA_VERSION:
        raise StorageError(
            StorageErrorCode.SCHEMA_UNSUPPORTED,
            f"Schema sqlite-v{version} is not the supported {SCHEMA_ID} schema.",
        )
    try:
        if _schema_signature(connection) != _expected_schema_signature():
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database does not have the exact current schema.")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
    except sqlite3.Error as error:
        raise _translate_database_error(error, opening=True) from error
    if quick_check is None or quick_check[0] != "ok" or foreign_key_errors is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "SQLite reported invalid current work state.")


def schema_bytes() -> bytes:
    try:
        return Path(__file__).with_name("schema.sql").read_bytes()
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The installed SQLite schema could not be read.") from error


def _sync_database(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The initialized database could not be synchronized.") from error


def _staging_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.charlie-pinboard-stage")


def _journal_path(path: Path) -> Path:
    return path.with_name(f"{path.name}-journal")


def _cleanup_database_files(path: Path) -> bool:
    removed = False
    succeeded = True
    for candidate in (_journal_path(path), path):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            succeeded = False
            continue
        try:
            candidate.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            succeeded = False
    if removed:
        try:
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            succeeded = False
    return succeeded


def _prepare_database_publication(destination: Path) -> Path:
    staging = _staging_path(destination)
    if destination.exists():
        try:
            resumable = staging.exists() and destination.samefile(staging)
        except OSError as error:
            raise StorageError(
                StorageErrorCode.IO_ERROR, "SQLite publication residue could not be inspected."
            ) from error
        if not resumable:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                f"Database already exists: {destination}",
            )
        if not _cleanup_database_files(destination) or not _cleanup_database_files(staging):
            raise StorageError(StorageErrorCode.IO_ERROR, "SQLite publication residue could not be removed.")
    elif not _cleanup_database_files(staging):
        raise StorageError(StorageErrorCode.IO_ERROR, "SQLite staging residue could not be removed.")
    return staging


def _publish_database(staging: Path, destination: Path) -> None:
    published = False
    try:
        try:
            os.link(staging, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                f"Database already exists: {destination}",
            ) from error
        except OSError as error:
            raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite database could not be published.") from error
        published = True
        _sync_database(destination)
        if not _cleanup_database_files(staging):
            raise StorageError(StorageErrorCode.IO_ERROR, "SQLite staging cleanup could not be synchronized.")
    except StorageError:
        if not published or _cleanup_database_files(destination):
            _cleanup_database_files(staging)
        raise


def initialize_database(roots: DurableRoots, now: datetime) -> None:
    try:
        ensure_directory_chain(roots)
    except FileIOError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, str(error)) from error
    path = roots.database_path
    staging = _prepare_database_publication(path)

    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(staging, timeout=BUSY_TIMEOUT_MS / 1_000, isolation_level=None)
            _configure_connection(connection)
            _configure_writes(connection)
            connection.executescript(schema_bytes().decode("utf-8"))
            with write_transaction(connection):
                timestamp = now.isoformat()
                connection.execute(
                    """
                    INSERT INTO project_meta (
                        singleton, application, schema_version, revision, host_epoch, created_at, updated_at
                    ) VALUES (1, ?, ?, 0, 1, ?, ?)
                    """,
                    (APPLICATION, SCHEMA_VERSION, timestamp, timestamp),
                )
            _verify_current_schema(connection)
        finally:
            if connection is not None:
                connection.close()
        _sync_database(staging)
    except StorageError:
        _cleanup_database_files(staging)
        raise
    except (OSError, UnicodeError) as error:
        _cleanup_database_files(staging)
        raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite database could not be initialized.") from error
    except sqlite3.Error as error:
        _cleanup_database_files(staging)
        raise _translate_database_error(error) from error
    _publish_database(staging, path)


def _open_verified_database(
    path: Path,
    mode: OpenMode,
    *,
    configure_writes: bool,
    immutable: bool = False,
) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _database_uri(path, mode, immutable=immutable),
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        _configure_connection(connection)
        _verify_current_schema(connection)
        if configure_writes:
            _configure_writes(connection)
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise _translate_database_error(error, opening=True) from error


def open_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
    preflight = _open_verified_database(path, OpenMode.READ_ONLY, configure_writes=False, immutable=True)
    preflight.close()
    return _open_verified_database(path, mode, configure_writes=mode == OpenMode.READ_WRITE)


@contextmanager
def read_operation(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    try:
        yield connection
    except StorageError:
        raise
    except sqlite3.Error as error:
        raise _translate_database_error(error, opening=True) from error


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except StorageError:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        except Exception as error:
            connection.rollback()
            raise StorageError(
                StorageErrorCode.OPERATION_FAILED,
                "The application operation failed inside the SQLite transaction.",
            ) from error
        try:
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise _translate_database_error(error) from error
    except StorageError:
        raise
    except sqlite3.Error as error:
        raise _translate_database_error(error) from error


def backup_database(source: Path, destination: Path) -> None:
    staging = _prepare_database_publication(destination)
    source_connection = open_database(source, OpenMode.READ_ONLY)
    destination_connection: sqlite3.Connection | None = None
    try:
        try:
            destination_connection = sqlite3.connect(staging, isolation_level=None)
            source_connection.backup(destination_connection)
            destination_connection.close()
            destination_connection = None
            verified = open_database(staging, OpenMode.READ_ONLY)
            verified.close()
        finally:
            source_connection.close()
            if destination_connection is not None:
                destination_connection.close()
        _sync_database(staging)
    except StorageError:
        _cleanup_database_files(staging)
        raise
    except (OSError, sqlite3.Error) as error:
        _cleanup_database_files(staging)
        if isinstance(error, sqlite3.Error):
            raise _translate_database_error(error) from error
        raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite backup could not be published.") from error
    _publish_database(staging, destination)
