from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from uuid import uuid4

from charlie_pinboard.domain.model import AttemptState
from charlie_pinboard.legacy.atomic import atomic_write_text
from charlie_pinboard.legacy.authority import Authority, AuthorityVersion, authority_transaction
from charlie_pinboard.legacy.markdown import (
    ITEM_PATTERN,
    ParseError,
    parse_attempt,
    parse_header,
    parse_queue,
    render_v2_header,
    replace_v2_header_fields,
)
from charlie_pinboard.legacy.storage_layout import PathIdentityError, identity_child


class LeaseError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class LeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    task_id: str
    host_id: str
    lease_id: str
    generation: int
    acquired_at: datetime
    expires_at: datetime
    status: LeaseStatus
    attempt_id: str | None = None


def _v2_root(authority: Authority) -> Path:
    if authority.version != AuthorityVersion.V2:
        raise LeaseError(
            "MIGRATION_REQUIRED",
            "Lease operations require schema v2; run 'pinboard migrate --to v2' first.",
        )
    return authority.work_root


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise LeaseError("LEASE_TIME_INVALID", "Lease timestamps must be timezone-aware.")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, path: Path, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LeaseError("LEASE_INVALID", f"'{path}' has invalid {field} '{value}'.") from error
    if parsed.tzinfo is None:
        raise LeaseError("LEASE_INVALID", f"'{path}' has naive {field} '{value}'.")
    return parsed.astimezone(UTC)


def _string(header: dict[str, str | bool | None], path: Path, field: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value:
        raise LeaseError("LEASE_INVALID", f"'{path}' requires a non-empty {field}.")
    return value


def _integer(header: dict[str, str | bool | None], path: Path, field: str) -> int:
    value = _string(header, path, field)
    try:
        result = int(value)
    except ValueError as error:
        raise LeaseError("LEASE_INVALID", f"'{path}' has invalid {field} '{value}'.") from error
    if result < 0:
        raise LeaseError("LEASE_INVALID", f"'{path}' has negative {field}.")
    return result


def _host_identity(value: str, work_root: Path, path: Path) -> str:
    try:
        identity_child(work_root, path.parent, value)
    except PathIdentityError as error:
        raise LeaseError("LEASE_IDENTITY_INVALID", f"'{path}' has an invalid host identity '{value}'.") from error
    return value


def _validate_interval(record: LeaseRecord, path: Path, *, minimum_generation: int) -> LeaseRecord:
    if record.generation < minimum_generation:
        raise LeaseError("LEASE_INVALID", f"'{path}' has an invalid lease generation {record.generation}.")
    if record.expires_at < record.acquired_at:
        raise LeaseError("LEASE_INVALID", f"'{path}' has an expiry before acquisition.")
    return record


def _coordination_path(work_root: Path) -> Path:
    try:
        return identity_child(work_root, work_root / "leases", "coordination.md")
    except PathIdentityError as error:
        raise LeaseError(
            "LEASE_IDENTITY_INVALID",
            "The coordination lease must stay inside the authoritative leases directory.",
        ) from error


def _read_coordination(work_root: Path, path: Path) -> LeaseRecord:
    header = parse_header(path)
    if header.get("kind") != "coordination-lease" or header.get("schema") != "repo-work/v2":
        raise LeaseError("LEASE_INVALID", f"'{path}' is not a schema-v2 coordination lease.")
    try:
        status = LeaseStatus(_string(header, path, "lease_status"))
    except ValueError as error:
        raise LeaseError("LEASE_INVALID", f"'{path}' has an invalid lease_status.") from error
    record = LeaseRecord(
        task_id=_string(header, path, "owner_task_id"),
        host_id=_host_identity(_string(header, path, "owner_host_id"), work_root, path),
        lease_id=_string(header, path, "lease_id"),
        generation=_integer(header, path, "lease_generation"),
        acquired_at=_parse_timestamp(_string(header, path, "lease_acquired_at"), path, "lease_acquired_at"),
        expires_at=_parse_timestamp(_string(header, path, "lease_expires_at"), path, "lease_expires_at"),
        status=status,
    )
    return _validate_interval(record, path, minimum_generation=1)


def _coordination_text(record: LeaseRecord) -> str:
    return (
        render_v2_header(
            {
                "kind": "coordination-lease",
                "schema": "repo-work/v2",
                "owner_task_id": record.task_id,
                "owner_host_id": record.host_id,
                "lease_id": record.lease_id,
                "lease_generation": record.generation,
                "lease_acquired_at": _timestamp(record.acquired_at),
                "lease_expires_at": _timestamp(record.expires_at),
                "lease_status": record.status.value,
            }
        )
        + "\n# Coordination Lease\n"
    )


def _active(record: LeaseRecord, current: datetime) -> bool:
    return record.status == LeaseStatus.ACTIVE and current < record.expires_at


def read_coordination_lease(work_root: Path) -> LeaseRecord | None:
    path = _coordination_path(work_root)
    return _read_coordination(work_root, path) if path.is_file() else None


def acquire_coordination(
    work_root: Path,
    task_id: str,
    host_id: str,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
    lease_id: str | None = None,
) -> LeaseRecord:
    current = _now(now)
    if not task_id or not host_id or ttl_seconds <= 0:
        raise LeaseError("COORDINATION_LEASE_REQUIRED", "Task, host, and positive TTL are required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        path = _coordination_path(root)
        _host_identity(host_id, root, path)
        previous = read_coordination_lease(root)
        if previous is not None and _active(previous, current):
            raise LeaseError(
                "COORDINATION_LEASE_BUSY",
                f"Held by task '{previous.task_id}' on '{previous.host_id}' until {previous.expires_at.isoformat()}.",
            )
        record = LeaseRecord(
            task_id,
            host_id,
            lease_id or uuid4().hex,
            (previous.generation if previous is not None else 0) + 1,
            current,
            current + timedelta(seconds=ttl_seconds),
            LeaseStatus.ACTIVE,
        )
        atomic_write_text(path, _coordination_text(record))
        return record


def _require_coordination_record(work_root: Path, lease_id: str, generation: int, current: datetime) -> LeaseRecord:
    record = read_coordination_lease(work_root)
    if record is None:
        raise LeaseError("COORDINATION_LEASE_REQUIRED", "No coordination lease exists.")
    if record.lease_id != lease_id or record.generation != generation or record.status != LeaseStatus.ACTIVE:
        raise LeaseError("LEASE_FENCED", "The coordination lease was released, revoked, or superseded.")
    if current >= record.expires_at:
        raise LeaseError(
            "COORDINATION_LEASE_REQUIRED", f"The coordination lease expired at {record.expires_at.isoformat()}."
        )
    return record


def require_coordination(
    work_root: Path, lease_id: str, generation: int, *, now: datetime | None = None
) -> LeaseRecord:
    return _require_coordination_record(work_root, lease_id, generation, _now(now))


def renew_coordination(
    work_root: Path,
    lease_id: str,
    generation: int,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
) -> LeaseRecord:
    current = _now(now)
    if ttl_seconds <= 0:
        raise LeaseError("COORDINATION_LEASE_REQUIRED", "A positive TTL is required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        record = _require_coordination_record(root, lease_id, generation, current)
        renewed = LeaseRecord(
            record.task_id,
            record.host_id,
            record.lease_id,
            record.generation,
            record.acquired_at,
            current + timedelta(seconds=ttl_seconds),
            LeaseStatus.ACTIVE,
        )
        atomic_write_text(_coordination_path(root), _coordination_text(renewed))
        return renewed


def release_coordination(
    work_root: Path, lease_id: str, generation: int, *, now: datetime | None = None
) -> LeaseRecord:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        record = _require_coordination_record(root, lease_id, generation, current)
        released = LeaseRecord(
            record.task_id,
            record.host_id,
            record.lease_id,
            record.generation,
            record.acquired_at,
            current,
            LeaseStatus.RELEASED,
        )
        atomic_write_text(_coordination_path(root), _coordination_text(released))
        return released


def revoke_coordination(work_root: Path, *, now: datetime | None = None) -> LeaseRecord:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        previous = read_coordination_lease(root)
        if previous is None:
            raise LeaseError("COORDINATION_LEASE_REQUIRED", "No coordination lease exists to revoke.")
        revoked = LeaseRecord(
            previous.task_id,
            previous.host_id,
            previous.lease_id,
            previous.generation + 1,
            previous.acquired_at,
            current,
            LeaseStatus.REVOKED,
        )
        atomic_write_text(_coordination_path(root), _coordination_text(revoked))
        return revoked


def _attempt_path(work_root: Path, attempt_id: str) -> Path:
    if ITEM_PATTERN.fullmatch(attempt_id) is None:
        raise LeaseError("ATTEMPT_ID_INVALID", f"Invalid attempt identity '{attempt_id}'.")
    try:
        directory = identity_child(work_root, work_root / "attempts", attempt_id)
    except PathIdentityError as error:
        raise LeaseError("ATTEMPT_ID_INVALID", f"Invalid attempt identity '{attempt_id}'.") from error
    return directory / "attempt.md"


def read_attempt_lease(work_root: Path, attempt_id: str) -> LeaseRecord:
    path = _attempt_path(work_root, attempt_id)
    header = parse_header(path)
    if header.get("kind") != "work-attempt" or header.get("schema") != "repo-work/v2":
        raise LeaseError("LEASE_INVALID", f"'{path}' is not a schema-v2 attempt lease.")
    if _string(header, path, "attempt") != attempt_id or path.parent.name != attempt_id:
        raise LeaseError("LEASE_IDENTITY_MISMATCH", f"'{path}' does not describe attempt '{attempt_id}'.")
    generation = _integer(header, path, "lease_generation")
    task_value = _string(header, path, "owner_task_id")
    host_value = _host_identity(_string(header, path, "owner_host_id"), work_root, path)
    lease_value = _string(header, path, "lease_id")
    acquired_value = _string(header, path, "lease_acquired_at")
    expires_value = _string(header, path, "lease_expires_at")
    status_text = _string(header, path, "lease_status")
    try:
        status = LeaseStatus(status_text)
    except ValueError as error:
        raise LeaseError("LEASE_INVALID", f"'{path}' has invalid attempt lease status.") from error
    record = LeaseRecord(
        task_value,
        host_value,
        lease_value,
        generation,
        _parse_timestamp(acquired_value, path, "lease_acquired_at"),
        _parse_timestamp(expires_value, path, "lease_expires_at"),
        status,
        attempt_id,
    )
    if generation == 0:
        if (
            record.task_id,
            record.host_id,
            record.lease_id,
            record.status,
            record.acquired_at,
        ) != ("unclaimed", "unclaimed", "unclaimed", LeaseStatus.RELEASED, record.expires_at):
            raise LeaseError("LEASE_INVALID", f"'{path}' has a partial unclaimed lease shape.")
        return record
    return _validate_interval(record, path, minimum_generation=1)


def _write_attempt_lease(work_root: Path, record: LeaseRecord) -> None:
    if record.attempt_id is None:
        raise LeaseError("LEASE_INVALID", "Attempt lease is missing an attempt identity.")
    path = _attempt_path(work_root, record.attempt_id)
    text = path.read_text(encoding="utf-8")
    replacements = {
        "owner_task_id": record.task_id,
        "owner_host_id": record.host_id,
        "lease_id": record.lease_id,
        "lease_generation": record.generation,
        "lease_acquired_at": _timestamp(record.acquired_at),
        "lease_expires_at": _timestamp(record.expires_at),
        "lease_status": record.status.value,
    }
    atomic_write_text(path, replace_v2_header_fields(text, replacements))


def _require_live_attempt(work_root: Path, attempt_id: str, code: str) -> None:
    try:
        queue = parse_queue(work_root / "queue.md")
        attempt = parse_attempt(_attempt_path(work_root, attempt_id))
    except (OSError, ParseError) as error:
        raise LeaseError("LEASE_INVALID", f"Cannot verify the live item for attempt '{attempt_id}': {error}") from error
    queue_item = next((item for item in queue.items if item.attempt == attempt_id), None)
    if (
        queue_item is None
        or attempt.state == AttemptState.DONE
        or queue_item.item != attempt.item
        or queue_item.state.value != attempt.state.value
    ):
        raise LeaseError(code, f"Attempt '{attempt_id}' does not belong to a live queue item.")


def acquire_attempt(
    work_root: Path,
    attempt_id: str,
    task_id: str,
    host_id: str,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
    lease_id: str | None = None,
) -> LeaseRecord:
    current = _now(now)
    if not task_id or ttl_seconds <= 0:
        raise LeaseError("ATTEMPT_LEASE_REQUIRED", "A positive TTL is required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        attempt_path = _attempt_path(root, attempt_id)
        _host_identity(host_id, root, attempt_path)
        _require_live_attempt(root, attempt_id, "ATTEMPT_LEASE_REQUIRED")
        previous = read_attempt_lease(root, attempt_id)
        if _active(previous, current):
            raise LeaseError(
                "ATTEMPT_LEASE_REQUIRED",
                f"Attempt '{attempt_id}' is held by task '{previous.task_id}' until {previous.expires_at.isoformat()}.",
            )
        record = LeaseRecord(
            task_id,
            host_id,
            lease_id or uuid4().hex,
            previous.generation + 1,
            current,
            current + timedelta(seconds=ttl_seconds),
            LeaseStatus.ACTIVE,
            attempt_id,
        )
        _write_attempt_lease(root, record)
        return record


def _require_attempt_record(
    work_root: Path, attempt_id: str, lease_id: str, generation: int, current: datetime
) -> LeaseRecord:
    _require_live_attempt(work_root, attempt_id, "LEASE_FENCED")
    record = read_attempt_lease(work_root, attempt_id)
    if record.lease_id != lease_id or record.generation != generation or record.status != LeaseStatus.ACTIVE:
        raise LeaseError("LEASE_FENCED", f"Attempt lease for '{attempt_id}' was released, revoked, or superseded.")
    if current >= record.expires_at:
        raise LeaseError("ATTEMPT_LEASE_EXPIRED", f"Attempt lease expired at {record.expires_at.isoformat()}.")
    return record


def require_attempt(
    work_root: Path,
    attempt_id: str,
    lease_id: str,
    generation: int,
    *,
    now: datetime | None = None,
) -> LeaseRecord:
    return _require_attempt_record(work_root, attempt_id, lease_id, generation, _now(now))


def renew_attempt(
    work_root: Path,
    attempt_id: str,
    lease_id: str,
    generation: int,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
) -> LeaseRecord:
    current = _now(now)
    if ttl_seconds <= 0:
        raise LeaseError("ATTEMPT_LEASE_REQUIRED", "A positive TTL is required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        record = _require_attempt_record(root, attempt_id, lease_id, generation, current)
        renewed = LeaseRecord(
            record.task_id,
            record.host_id,
            record.lease_id,
            record.generation,
            record.acquired_at,
            current + timedelta(seconds=ttl_seconds),
            LeaseStatus.ACTIVE,
            attempt_id,
        )
        _write_attempt_lease(root, renewed)
        return renewed


def release_attempt(
    work_root: Path,
    attempt_id: str,
    lease_id: str,
    generation: int,
    *,
    now: datetime | None = None,
) -> LeaseRecord:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        record = _require_attempt_record(root, attempt_id, lease_id, generation, current)
        released = LeaseRecord(
            record.task_id,
            record.host_id,
            record.lease_id,
            record.generation,
            record.acquired_at,
            current,
            LeaseStatus.RELEASED,
            attempt_id,
        )
        _write_attempt_lease(root, released)
        return released


def revoke_attempt(
    work_root: Path,
    attempt_id: str,
    coordination_lease_id: str,
    coordination_generation: int,
    *,
    now: datetime | None = None,
) -> LeaseRecord:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        _require_coordination_record(root, coordination_lease_id, coordination_generation, current)
        previous = read_attempt_lease(root, attempt_id)
        revoked = LeaseRecord(
            previous.task_id,
            previous.host_id,
            previous.lease_id,
            previous.generation + 1,
            previous.acquired_at,
            current,
            LeaseStatus.REVOKED,
            attempt_id,
        )
        _write_attempt_lease(root, revoked)
        return revoked
