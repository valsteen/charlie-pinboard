import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from functools import cache
from pathlib import Path
from urllib.parse import quote

from charlie_pinboard.adapters.files.file_io import DurableRoots, FileIOError, ensure_directory_chain
from charlie_pinboard.domain.errors import DecisionError

APPLICATION = "charlie-pinboard"
SCHEMA_VERSION = 1
SCHEMA_ID = "sqlite-v1"
BUSY_TIMEOUT_MS = 2_000


class OpenMode(Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


class StorageErrorCode(Enum):
    BUSY = "STORAGE_BUSY"
    INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    INVALID_STATE = "WORK_STATE_INVALID"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
    IO_ERROR = "STORAGE_IO_ERROR"
    OPERATION_FAILED = "STORAGE_OPERATION_FAILED"


class StorageError(RuntimeError):
    code: StorageErrorCode
    retryable: bool

    def __init__(self, code: StorageErrorCode, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code.value}: {message}")


def _database_uri(path: Path, mode: OpenMode) -> str:
    return f"file:{quote(str(path.absolute()), safe='/')}?mode={mode.value}"


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


def _configure(connection: sqlite3.Connection, mode: OpenMode) -> None:
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or enabled[0] != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "SQLite foreign-key enforcement is unavailable.")
    if mode == OpenMode.READ_WRITE:
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
    if version < SCHEMA_VERSION:
        raise StorageError(
            StorageErrorCode.MIGRATION_REQUIRED,
            f"Schema sqlite-v{version} requires an explicit migration to {SCHEMA_ID}.",
        )
    if version > SCHEMA_VERSION:
        raise StorageError(StorageErrorCode.SCHEMA_TOO_NEW, f"Schema sqlite-v{version} is newer than {SCHEMA_ID}.")
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


def initialize_database(roots: DurableRoots, now: datetime) -> None:
    try:
        ensure_directory_chain(roots)
    except FileIOError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, str(error)) from error
    path = roots.database_path
    if path.exists():
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, f"Database already exists: {path}")

    connection: sqlite3.Connection | None = None
    created = False
    initialized = False
    try:
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1_000, isolation_level=None)
        created = True
        _configure(connection, OpenMode.READ_WRITE)
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
        initialized = True
    except StorageError:
        raise
    except (OSError, UnicodeError) as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite database could not be initialized.") from error
    except sqlite3.Error as error:
        raise _translate_database_error(error) from error
    finally:
        if connection is not None:
            connection.close()
        if created and not initialized:
            for candidate in (path, path.with_name(f"{path.name}-journal")):
                candidate.unlink(missing_ok=True)
        if initialized:
            try:
                _sync_database(path)
            except StorageError:
                for candidate in (path, path.with_name(f"{path.name}-journal")):
                    candidate.unlink(missing_ok=True)
                raise


def open_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _database_uri(path, mode),
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        _configure(connection, mode)
        _verify_current_schema(connection)
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise _translate_database_error(error, opening=True) from error


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
        except DecisionError:
            connection.rollback()
            raise
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
    if destination.exists():
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, f"Backup destination already exists: {destination}")
    source_connection = open_database(source, OpenMode.READ_ONLY)
    destination_connection: sqlite3.Connection | None = None
    try:
        destination_connection = sqlite3.connect(destination, isolation_level=None)
        source_connection.backup(destination_connection)
        destination_connection.close()
        destination_connection = None
        verified = open_database(destination, OpenMode.READ_ONLY)
        verified.close()
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except StorageError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as error:
        destination.unlink(missing_ok=True)
        if isinstance(error, sqlite3.Error):
            raise _translate_database_error(error) from error
        raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite backup could not be published.") from error
    finally:
        source_connection.close()
        if destination_connection is not None:
            destination_connection.close()
