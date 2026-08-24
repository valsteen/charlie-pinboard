import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from charlie_pinboard import __version__
from charlie_pinboard.adapters.files.artifacts import ArtifactRepository
from charlie_pinboard.adapters.files.file_io import DurableRoots
from charlie_pinboard.adapters.files.views import rebuild as rebuild_views
from charlie_pinboard.adapters.sqlite.database import OpenMode, StorageError, open_database, write_transaction
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import ArtifactKind, TransitionHistoryActionKind
from charlie_pinboard.legacy.legacy_import import (
    CUTOVER_TOMBSTONE,
    INACTIVE_ROOT_SELECTORS,
    LegacyImportError,
    inactive_roots_from_manifest,
)
from charlie_pinboard.legacy.validate import validate_sqlite_work_state

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type Sha256String = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]


@dataclass(frozen=True, slots=True)
class CleanupCorrectionReceipt:
    cutover_id: str
    artifact_selector: str
    artifact_sha256: str
    original_artifact_selector: str
    original_artifact_sha256: str
    removed_selectors: tuple[str, ...]
    database_revision_before_receipt: int
    committed_revision: int
    verified_clean_at: datetime
    receipt_bytes: bytes


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    cutover_id: str
    artifact_selector: str
    artifact_sha256: str
    database_revision_before_receipt: int
    committed_revision: int
    verified_clean_at: datetime
    receipt_bytes: bytes
    correction: CleanupCorrectionReceipt | None = None


class _ValidationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    code: Literal["WORK_STATE_VALID"]
    revision: int


class _CleanupReceiptRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    absent_selectors: tuple[str, ...]
    cutover_id: Sha256String
    database_revision_before_receipt: int
    executable_sha256: Sha256String
    plugin_version: NonEmptyString
    schema: Literal["repo-work-cleanup-receipt/v1"]
    validation: _ValidationRecord
    verified_clean_at: NonEmptyString


class _CleanupCorrectionReceiptRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    absent_selectors: tuple[str, ...]
    cutover_id: Sha256String
    database_revision_before_receipt: int
    executable_sha256: Sha256String
    original_artifact_selector: NonEmptyString
    original_artifact_sha256: Sha256String
    original_committed_revision: int
    plugin_version: NonEmptyString
    removed_selectors: tuple[str, ...]
    schema: Literal["repo-work-cleanup-correction-receipt/v1"]
    validation: _ValidationRecord
    verified_clean_at: NonEmptyString


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_id(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The expected cutover id is not lowercase SHA-256.")


def _import_manifest(work_root: Path, cutover_id: str) -> tuple[str, bytes]:
    state = SQLiteWorkStore(work_root / "state.sqlite3").snapshot()
    expected_action = f"legacy-import:{cutover_id}"
    imports = [
        receipt
        for receipt in state.history.receipts
        if receipt.action_kind == TransitionHistoryActionKind.LEGACY_IMPORT and receipt.action_id == expected_action
    ]
    if len(imports) != 1 or imports[0].artifact_ref_id is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The expected legacy import receipt is absent.")
    reference = next(
        (value for value in state.artifacts.references if value.artifact_ref_id == imports[0].artifact_ref_id),
        None,
    )
    if reference is None or reference.kind != ArtifactKind.EVIDENCE:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The import receipt has no evidence manifest.")
    ArtifactRepository(DurableRoots(work_root, ())).verify(reference)
    return reference.selector, (work_root / reference.selector).read_bytes()


def _historical_absent_selectors(cutover_id: str, inactive: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                "authority.json",
                "v2",
                "legacy-v2",
                "legacy-v1",
                f".retired-legacy-v2-{cutover_id}",
                f".retired-legacy-v1-{cutover_id}",
                *inactive,
            }
        )
    )


def _absent_selectors(cutover_id: str) -> tuple[str, ...]:
    return _historical_absent_selectors(cutover_id, INACTIVE_ROOT_SELECTORS)


def _verify_absent(work_root: Path, selectors: tuple[str, ...]) -> None:
    present = [
        selector for selector in selectors if (work_root / selector).exists() or (work_root / selector).is_symlink()
    ]
    if present:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Legacy selectors remain: {', '.join(present)}.")


def _recognized_empty_residue(work_root: Path, selectors: tuple[str, ...]) -> tuple[str, ...]:
    residue: list[str] = []
    for selector in selectors:
        path = work_root / selector
        if path.is_symlink():
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Legacy selector {selector} is a symlink.")
        if not path.exists():
            continue
        if not path.is_dir():
            raise LegacyImportError(
                "STORAGE_INVARIANT_VIOLATION", f"Unmanifested legacy selector {selector} is not a directory."
            )
        try:
            populated = next(path.iterdir(), None) is not None
        except OSError as error:
            raise LegacyImportError(
                "STORAGE_INVARIANT_VIOLATION", f"Unmanifested legacy selector {selector} cannot be inspected."
            ) from error
        if populated:
            raise LegacyImportError(
                "STORAGE_INVARIANT_VIOLATION", f"Unmanifested legacy selector {selector} is not empty."
            )
        residue.append(selector)
    return tuple(residue)


def _remove_empty_residue(work_root: Path, selectors: tuple[str, ...]) -> None:
    for selector in selectors:
        try:
            (work_root / selector).rmdir()
        except OSError as error:
            raise LegacyImportError(
                "STORAGE_INVARIANT_VIOLATION", f"Empty legacy selector {selector} could not be removed."
            ) from error
        _sync_directory(work_root)


def _require_valid_sqlite(work_root: Path) -> None:
    report = validate_sqlite_work_state(work_root)
    if not report.valid:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", report.render())


def _runtime_sha256() -> str:
    try:
        return hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    except OSError as error:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The transitional executable cannot be hashed."
        ) from error


def _remove_legacy(work_root: Path, cutover_id: str) -> None:
    marker = work_root / "authority.json"
    if marker.exists() and marker.read_bytes() != CUTOVER_TOMBSTONE:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The exact SQLite cutover tombstone is not active.")
    for name in ("legacy-v2", "legacy-v1"):
        source = work_root / name
        retired = work_root / f".retired-{name}-{cutover_id}"
        if source.exists() and retired.exists():
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Both live and retired {name} selectors exist.")
        if source.exists():
            source.replace(retired)
            _sync_directory(work_root)
        if retired.exists():
            if retired.is_symlink() or not retired.is_dir():
                raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Retired selector {retired.name} is invalid.")
            shutil.rmtree(retired)
            _sync_directory(work_root)
    if marker.exists():
        marker.unlink()
        _sync_directory(work_root)


def _require_removal_preconditions(work_root: Path, cutover_id: str) -> None:
    marker = work_root / "authority.json"
    if marker.is_symlink() or (marker.exists() and (not marker.is_file() or marker.read_bytes() != CUTOVER_TOMBSTONE)):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The exact SQLite cutover tombstone is not active.")
    for name in ("legacy-v2", "legacy-v1"):
        source = work_root / name
        retired = work_root / f".retired-{name}-{cutover_id}"
        if (source.exists() or source.is_symlink()) and (retired.exists() or retired.is_symlink()):
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Both live and retired {name} selectors exist.")
        for selector in (source, retired):
            if selector.is_symlink() or (selector.exists() and not selector.is_dir()):
                raise LegacyImportError(
                    "STORAGE_INVARIANT_VIOLATION", f"Legacy archive selector {selector.name} is invalid."
                )


def _receipt_time(value: _CleanupReceiptRecord) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.verified_clean_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup receipt time is invalid.") from error
    if timestamp.tzinfo is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup receipt time must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _correction_time(value: _CleanupCorrectionReceiptRecord) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.verified_clean_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup correction time is invalid.") from error
    if timestamp.tzinfo is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup correction time must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _decode_receipt(data: bytes) -> tuple[int, datetime]:
    try:
        value = msgspec.json.decode(data, type=_CleanupReceiptRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup receipt is invalid JSON.") from error
    return value.database_revision_before_receipt, _receipt_time(value)


def _decode_correction_receipt(data: bytes) -> tuple[_CleanupCorrectionReceiptRecord, datetime]:
    try:
        value = msgspec.json.decode(data, type=_CleanupCorrectionReceiptRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The cleanup correction receipt is invalid JSON."
        ) from error
    return value, _correction_time(value)


def _validate_orphan_receipt(data: bytes, current: _CleanupReceiptRecord) -> None:
    try:
        value = msgspec.json.decode(data, type=_CleanupReceiptRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The unreferenced cleanup artifact is not recoverable."
        ) from error
    _receipt_time(value)
    expected = _CleanupReceiptRecord(
        current.absent_selectors,
        current.cutover_id,
        current.database_revision_before_receipt,
        current.executable_sha256,
        current.plugin_version,
        current.schema,
        current.validation,
        value.verified_clean_at,
    )
    canonical = msgspec.json.encode(value, order="sorted") + b"\n"
    if value != expected or data != canonical:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The unreferenced cleanup artifact is not recoverable.")


def _validate_orphan_correction_receipt(data: bytes, current: _CleanupCorrectionReceiptRecord) -> None:
    try:
        value = msgspec.json.decode(data, type=_CleanupCorrectionReceiptRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The unreferenced cleanup correction artifact is not recoverable."
        ) from error
    _correction_time(value)
    expected = _CleanupCorrectionReceiptRecord(
        current.absent_selectors,
        current.cutover_id,
        current.database_revision_before_receipt,
        current.executable_sha256,
        current.original_artifact_selector,
        current.original_artifact_sha256,
        current.original_committed_revision,
        current.plugin_version,
        current.removed_selectors,
        current.schema,
        current.validation,
        value.verified_clean_at,
    )
    canonical = msgspec.json.encode(value, order="sorted") + b"\n"
    if value != expected or data != canonical:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The unreferenced cleanup correction artifact is not recoverable."
        )


def _validate_receipt_value(
    receipt: CleanupReceipt,
    absent: tuple[str, ...],
) -> _CleanupReceiptRecord:
    try:
        value = msgspec.json.decode(receipt.receipt_bytes, type=_CleanupReceiptRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The cleanup receipt is invalid JSON.") from error
    expected = _CleanupReceiptRecord(
        absent,
        receipt.cutover_id,
        receipt.database_revision_before_receipt,
        value.executable_sha256,
        value.plugin_version,
        "repo-work-cleanup-receipt/v1",
        _ValidationRecord("WORK_STATE_VALID", receipt.database_revision_before_receipt),
        receipt.verified_clean_at.isoformat().replace("+00:00", "Z"),
    )
    if value != expected or hashlib.sha256(receipt.receipt_bytes).hexdigest() != receipt.artifact_sha256:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The committed cleanup receipt no longer matches.")
    return value


def _validate_correction_value(
    receipt: CleanupCorrectionReceipt,
    original: CleanupReceipt,
    absent: tuple[str, ...],
) -> None:
    value, verified = _decode_correction_receipt(receipt.receipt_bytes)
    expected = _CleanupCorrectionReceiptRecord(
        absent,
        receipt.cutover_id,
        receipt.database_revision_before_receipt,
        value.executable_sha256,
        original.artifact_selector,
        original.artifact_sha256,
        original.committed_revision,
        value.plugin_version,
        receipt.removed_selectors,
        "repo-work-cleanup-correction-receipt/v1",
        _ValidationRecord("WORK_STATE_VALID", receipt.database_revision_before_receipt),
        receipt.verified_clean_at.isoformat().replace("+00:00", "Z"),
    )
    if (
        value != expected
        or verified != receipt.verified_clean_at
        or not set(value.removed_selectors).issubset(INACTIVE_ROOT_SELECTORS)
        or hashlib.sha256(receipt.receipt_bytes).hexdigest() != receipt.artifact_sha256
    ):
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The committed cleanup correction receipt no longer matches."
        )


def _single_integer(row: sqlite3.Row | None, label: str) -> int:
    if row is None or not isinstance(row[0], int) or isinstance(row[0], bool):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"{label} could not be allocated.")
    return row[0]


def _existing_receipt(work_root: Path, cutover_id: str) -> CleanupReceipt | None:
    state = SQLiteWorkStore(work_root / "state.sqlite3").snapshot()
    action_id = f"legacy-cleanup:{cutover_id}"
    rows = [
        value
        for value in state.history.receipts
        if value.action_kind == TransitionHistoryActionKind.LEGACY_CLEANUP and value.action_id == action_id
    ]
    if not rows:
        return None
    if len(rows) != 1 or rows[0].artifact_ref_id is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup history is ambiguous.")
    reference = next(
        (value for value in state.artifacts.references if value.artifact_ref_id == rows[0].artifact_ref_id),
        None,
    )
    if reference is None or reference.kind != ArtifactKind.EVIDENCE:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup history has no evidence artifact.")
    ArtifactRepository(DurableRoots(work_root, ())).verify(reference)
    data = (work_root / reference.selector).read_bytes()
    before_revision, verified = _decode_receipt(data)
    if rows[0].project_revision != before_revision + 1 or reference.accepted_revision != rows[0].project_revision:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup receipt revisions contradict history.")
    return CleanupReceipt(
        cutover_id,
        reference.selector,
        reference.content_sha256,
        before_revision,
        rows[0].project_revision,
        verified,
        data,
    )


def _existing_correction_receipt(
    work_root: Path,
    cutover_id: str,
    original: CleanupReceipt,
    absent: tuple[str, ...],
) -> CleanupCorrectionReceipt | None:
    state = SQLiteWorkStore(work_root / "state.sqlite3").snapshot()
    action_id = f"legacy-cleanup-repair:{cutover_id}"
    rows = [
        value
        for value in state.history.receipts
        if value.action_kind == TransitionHistoryActionKind.LEGACY_CLEANUP and value.action_id == action_id
    ]
    if not rows:
        return None
    if len(rows) != 1 or rows[0].artifact_ref_id is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup correction history is ambiguous.")
    reference = next(
        (value for value in state.artifacts.references if value.artifact_ref_id == rows[0].artifact_ref_id),
        None,
    )
    if reference is None or reference.kind != ArtifactKind.EVIDENCE:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup correction history has no evidence artifact.")
    ArtifactRepository(DurableRoots(work_root, ())).verify(reference)
    data = (work_root / reference.selector).read_bytes()
    value, verified = _decode_correction_receipt(data)
    if rows[0].project_revision != value.database_revision_before_receipt + 1:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup correction revisions contradict history.")
    if reference.accepted_revision != rows[0].project_revision:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "Cleanup correction artifact revision contradicts history."
        )
    receipt = CleanupCorrectionReceipt(
        cutover_id,
        reference.selector,
        reference.content_sha256,
        value.original_artifact_selector,
        value.original_artifact_sha256,
        value.removed_selectors,
        value.database_revision_before_receipt,
        rows[0].project_revision,
        verified,
        data,
    )
    _validate_correction_value(receipt, original, absent)
    return receipt


def _repair_incomplete_cleanup(  # noqa: PLR0915 - one bounded repair transaction keeps adoption inspectable
    work_root: Path,
    cutover_id: str,
    now: datetime,
    original: CleanupReceipt,
    inactive: tuple[str, ...],
    absent: tuple[str, ...],
) -> CleanupReceipt:
    historical_absent = _historical_absent_selectors(cutover_id, inactive)
    _validate_receipt_value(original, historical_absent)
    _verify_absent(work_root, historical_absent)
    correction = _existing_correction_receipt(work_root, cutover_id, original, absent)
    if correction is not None:
        _verify_absent(work_root, absent)
        return replace(original, correction=correction)
    repairable_selectors = tuple(selector for selector in INACTIVE_ROOT_SELECTORS if selector not in inactive)
    _recognized_empty_residue(work_root, repairable_selectors)
    verified = now.astimezone(UTC)
    executable_sha256 = _runtime_sha256()
    database_path = work_root / "state.sqlite3"
    key = f"legacy-cleanup-repair-{cutover_id}"
    selector = f"artifacts/evidence/{key}/1.json"
    path = work_root / selector
    connection = open_database(database_path, OpenMode.READ_WRITE)
    try:
        with write_transaction(connection):
            action_id = f"legacy-cleanup-repair:{cutover_id}"
            committed = connection.execute(
                "SELECT 1 FROM transition_history WHERE action_kind = 'legacy-cleanup' AND action_id = ?",
                (action_id,),
            ).fetchone()
            if committed is None:
                removed = _recognized_empty_residue(work_root, repairable_selectors)
                _remove_empty_residue(work_root, removed)
                _verify_absent(work_root, absent)
                _require_valid_sqlite(work_root)
                row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
                if row is None or not isinstance(row[0], int):
                    raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Project revision is unavailable.")
                before_revision = row[0]
                committed_revision = before_revision + 1
                receipt_value = _CleanupCorrectionReceiptRecord(
                    absent,
                    cutover_id,
                    before_revision,
                    executable_sha256,
                    original.artifact_selector,
                    original.artifact_sha256,
                    original.committed_revision,
                    __version__,
                    removed,
                    "repo-work-cleanup-correction-receipt/v1",
                    _ValidationRecord("WORK_STATE_VALID", before_revision),
                    verified.isoformat().replace("+00:00", "Z"),
                )
                receipt_bytes = msgspec.json.encode(receipt_value, order="sorted") + b"\n"
                if path.exists() or path.is_symlink():
                    status = path.lstat()
                    referenced = connection.execute(
                        "SELECT 1 FROM artifact_refs WHERE relative_path = ?",
                        (selector,),
                    ).fetchone()
                    if referenced is not None or stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                        raise LegacyImportError(
                            "STORAGE_INVARIANT_VIOLATION",
                            "Cleanup correction artifact collision is not recoverable.",
                        )
                    orphan_bytes = path.read_bytes()
                    _validate_orphan_correction_receipt(orphan_bytes, receipt_value)
                    path.unlink()
                    _sync_directory(path.parent)
                published = ArtifactRepository(DurableRoots(work_root, ())).publish(
                    NewArtifact(ArtifactKind.EVIDENCE, key, 1, ".json", receipt_bytes)
                )
                artifact_row = connection.execute(
                    "SELECT COALESCE(MAX(artifact_ref_id), 0) + 1 FROM artifact_refs"
                ).fetchone()
                history_row = connection.execute(
                    "SELECT COALESCE(MAX(history_id), 0) + 1 FROM transition_history"
                ).fetchone()
                artifact_ref_id = _single_integer(artifact_row, "Cleanup correction artifact identity")
                history_id = _single_integer(history_row, "Cleanup correction history identity")
                connection.execute(
                    """
                    INSERT INTO artifact_refs (
                        artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
                        size_bytes, accepted_revision, created_at
                    ) VALUES (?, ?, 1, 'evidence', ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_ref_id,
                        key,
                        published.selector,
                        published.content_sha256,
                        published.size_bytes,
                        committed_revision,
                        verified.isoformat(),
                    ),
                )
                input_json = (
                    _canonical_json(
                        {
                            "cutover_id": cutover_id,
                            "original_artifact_selector": original.artifact_selector,
                            "original_artifact_sha256": original.artifact_sha256,
                        }
                    )
                    .removesuffix(b"\n")
                    .decode()
                )
                outcome_json = (
                    _canonical_json(
                        {
                            "artifact_selector": selector,
                            "artifact_sha256": published.content_sha256,
                            "removed_selectors": list(removed),
                            "verified_clean_at": verified.isoformat().replace("+00:00", "Z"),
                        }
                    )
                    .removesuffix(b"\n")
                    .decode()
                )
                connection.execute(
                    """
                    INSERT INTO transition_history (
                        history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                        artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
                        input_json, outcome_schema, outcome_json, committed_at
                    ) VALUES (?, ?, ?, 'legacy-cleanup', 'ledger', ?, 'evidence', 'migration', NULL, NULL,
                        'repo-work-cleanup-correction-input/v1', ?,
                        'repo-work-cleanup-correction-outcome/v1', ?, ?)
                    """,
                    (
                        history_id,
                        committed_revision,
                        action_id,
                        artifact_ref_id,
                        input_json,
                        outcome_json,
                        verified.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1",
                    (committed_revision, verified.isoformat()),
                )
    except StorageError as error:
        cause = error.__cause__
        if isinstance(cause, LegacyImportError):
            raise cause from error
        raise
    finally:
        connection.close()
    correction = _existing_correction_receipt(work_root, cutover_id, original, absent)
    if correction is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup correction receipt did not reload.")
    view_result = rebuild_views(SQLiteWorkStore(database_path), work_root)
    if view_result.warning is not None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", view_result.warning.message)
    return replace(original, correction=correction)


def cleanup_legacy(  # noqa: C901, PLR0912, PLR0915 - one bounded deletion transaction keeps adoption inspectable
    base_work_root: Path, expected_cutover_id: str, now: datetime
) -> CleanupReceipt:
    _validate_id(expected_cutover_id)
    if now.tzinfo is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup time must be timezone-aware.")
    work_root = base_work_root.resolve()
    _manifest_selector, manifest = _import_manifest(work_root, expected_cutover_id)
    inactive = inactive_roots_from_manifest(manifest)
    absent = _absent_selectors(expected_cutover_id)
    _require_valid_sqlite(work_root)
    existing = _existing_receipt(work_root, expected_cutover_id)
    if existing is not None:
        try:
            _validate_receipt_value(existing, absent)
        except LegacyImportError:
            return _repair_incomplete_cleanup(work_root, expected_cutover_id, now, existing, inactive, absent)
        _verify_absent(work_root, absent)
        correction = _existing_correction_receipt(work_root, expected_cutover_id, existing, absent)
        if correction is not None:
            raise LegacyImportError(
                "STORAGE_INVARIANT_VIOLATION", "A complete cleanup receipt has an unexpected correction receipt."
            )
        return existing
    _require_removal_preconditions(work_root, expected_cutover_id)
    _verify_absent(work_root, tuple(selector for selector in inactive))
    repairable_selectors = tuple(selector for selector in INACTIVE_ROOT_SELECTORS if selector not in inactive)
    _recognized_empty_residue(work_root, repairable_selectors)
    verified = now.astimezone(UTC)
    executable_sha256 = _runtime_sha256()
    database_path = work_root / "state.sqlite3"
    key = f"legacy-cleanup-{expected_cutover_id}"
    selector = f"artifacts/evidence/{key}/1.json"
    path = work_root / selector
    connection = open_database(database_path, OpenMode.READ_WRITE)
    try:
        with write_transaction(connection):
            action_id = f"legacy-cleanup:{expected_cutover_id}"
            committed = connection.execute(
                "SELECT 1 FROM transition_history WHERE action_kind = 'legacy-cleanup' AND action_id = ?",
                (action_id,),
            ).fetchone()
            if committed is None:
                empty_residue = _recognized_empty_residue(work_root, repairable_selectors)
                _remove_empty_residue(work_root, empty_residue)
                _remove_legacy(work_root, expected_cutover_id)
                _verify_absent(work_root, absent)
                _require_valid_sqlite(work_root)
                row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
                if row is None or not isinstance(row[0], int):
                    raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Project revision is unavailable.")
                before_revision = row[0]
                committed_revision = before_revision + 1
                receipt_value = _CleanupReceiptRecord(
                    absent,
                    expected_cutover_id,
                    before_revision,
                    executable_sha256,
                    __version__,
                    "repo-work-cleanup-receipt/v1",
                    _ValidationRecord("WORK_STATE_VALID", before_revision),
                    verified.isoformat().replace("+00:00", "Z"),
                )
                receipt_bytes = msgspec.json.encode(receipt_value, order="sorted") + b"\n"
                if path.exists():
                    status = path.lstat()
                    referenced = connection.execute(
                        "SELECT 1 FROM artifact_refs WHERE relative_path = ?",
                        (selector,),
                    ).fetchone()
                    if referenced is not None or stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                        raise LegacyImportError(
                            "STORAGE_INVARIANT_VIOLATION", "Cleanup artifact collision is not recoverable."
                        )
                    orphan_bytes = path.read_bytes()
                    _validate_orphan_receipt(orphan_bytes, receipt_value)
                    path.unlink()
                    _sync_directory(path.parent)
                published = ArtifactRepository(DurableRoots(work_root, ())).publish(
                    NewArtifact(ArtifactKind.EVIDENCE, key, 1, ".json", receipt_bytes)
                )
                artifact_row = connection.execute(
                    "SELECT COALESCE(MAX(artifact_ref_id), 0) + 1 FROM artifact_refs"
                ).fetchone()
                history_row = connection.execute(
                    "SELECT COALESCE(MAX(history_id), 0) + 1 FROM transition_history"
                ).fetchone()
                artifact_ref_id = _single_integer(artifact_row, "Cleanup artifact identity")
                history_id = _single_integer(history_row, "Cleanup history identity")
                connection.execute(
                    """
                    INSERT INTO artifact_refs (
                        artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
                        size_bytes, accepted_revision, created_at
                    ) VALUES (?, ?, 1, 'evidence', ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_ref_id,
                        key,
                        published.selector,
                        published.content_sha256,
                        published.size_bytes,
                        committed_revision,
                        verified.isoformat(),
                    ),
                )
                input_json = _canonical_json({"cutover_id": expected_cutover_id}).removesuffix(b"\n").decode()
                outcome_json = (
                    _canonical_json(
                        {
                            "artifact_selector": selector,
                            "artifact_sha256": published.content_sha256,
                            "verified_clean_at": verified.isoformat().replace("+00:00", "Z"),
                        }
                    )
                    .removesuffix(b"\n")
                    .decode()
                )
                connection.execute(
                    """
                    INSERT INTO transition_history (
                        history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                        artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
                        input_json, outcome_schema, outcome_json, committed_at
                    ) VALUES (?, ?, ?, 'legacy-cleanup', 'ledger', ?, 'evidence', 'migration', NULL, NULL,
                        'repo-work-cleanup-input/v1', ?, 'repo-work-cleanup-outcome/v1', ?, ?)
                    """,
                    (
                        history_id,
                        committed_revision,
                        action_id,
                        artifact_ref_id,
                        input_json,
                        outcome_json,
                        verified.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1",
                    (committed_revision, verified.isoformat()),
                )
    except StorageError as error:
        cause = error.__cause__
        if isinstance(cause, LegacyImportError):
            raise cause from error
        raise
    finally:
        connection.close()
    result = _existing_receipt(work_root, expected_cutover_id)
    if result is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Cleanup receipt did not reload.")
    _validate_receipt_value(result, absent)
    view_result = rebuild_views(SQLiteWorkStore(database_path), work_root)
    if view_result.warning is not None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", view_result.warning.message)
    return result
