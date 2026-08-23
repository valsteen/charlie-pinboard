from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import assert_never
from uuid import uuid4

from charlie_pinboard.legacy.atomic import atomic_write_text
from charlie_pinboard.legacy.authority import Authority, AuthorityVersion, authority_transaction
from charlie_pinboard.legacy.leases import LeaseError, require_attempt, require_coordination
from charlie_pinboard.legacy.markdown import ITEM_PATTERN, parse_header, render_v2_header
from charlie_pinboard.legacy.storage_layout import PathIdentityError, identity_child


class ResourceError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ResourceScope(Enum):
    HOST_LOCAL = "host-local"


class ResourceClaimStatus(Enum):
    ACTIVE = "active"
    RESERVED = "reserved"
    RELEASED = "released"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    resource_id: str
    label: str
    scope: ResourceScope


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    resource_id: str
    attempt_id: str
    task_id: str
    host_id: str
    lease_id: str
    generation: int
    acquired_at: datetime
    expires_at: datetime
    status: ResourceClaimStatus
    attempt_lease_id: str
    attempt_lease_generation: int


def _v2_root(authority: Authority) -> Path:
    if authority.version != AuthorityVersion.V2:
        raise ResourceError(
            "MIGRATION_REQUIRED",
            "Resource operations require schema v2; run 'pinboard migrate --to v2' first.",
        )
    return authority.work_root


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ResourceError("RESOURCE_TIME_INVALID", "Resource timestamps must be timezone-aware.")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, path: Path) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' contains an invalid timestamp.") from error
    if parsed.tzinfo is None:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' contains a naive timestamp.")
    return parsed.astimezone(UTC)


def _required(header: dict[str, str | bool | None], path: Path, field: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' requires {field}.")
    return value


def _claim_path(work_root: Path, resource_id: str, host_id: str) -> Path:
    if ITEM_PATTERN.fullmatch(resource_id) is None:
        raise ResourceError("RESOURCE_ID_INVALID", f"Invalid resource identity '{resource_id}'.")
    try:
        identity_child(work_root, work_root / "resources", resource_id)
        identity_child(work_root, work_root / "leases" / "resources", host_id)
        return identity_child(
            work_root,
            work_root / "leases" / "resources",
            f"{resource_id}--{host_id}.md",
        )
    except PathIdentityError as error:
        raise ResourceError(
            "RESOURCE_IDENTITY_INVALID",
            f"Resource '{resource_id}' and host '{host_id}' must be safe path identities.",
        ) from error


def _resource_path(work_root: Path, resource_id: str) -> Path:
    if ITEM_PATTERN.fullmatch(resource_id) is None:
        raise ResourceError("RESOURCE_ID_INVALID", f"Invalid resource identity '{resource_id}'.")
    try:
        return identity_child(work_root, work_root / "resources", f"{resource_id}.md")
    except PathIdentityError as error:
        raise ResourceError("RESOURCE_ID_INVALID", f"Invalid resource identity '{resource_id}'.") from error


def declare_resource(
    work_root: Path,
    resource_id: str,
    label: str,
    coordination_lease_id: str,
    coordination_generation: int,
    *,
    scope: str | ResourceScope,
    now: datetime | None = None,
) -> ResourceDeclaration:
    if ITEM_PATTERN.fullmatch(resource_id) is None or not label:
        raise ResourceError("RESOURCE_DECLARATION_INVALID", "Resource identity and label are required.")
    try:
        match scope:
            case ResourceScope():
                selected_scope = scope
            case str():
                selected_scope = ResourceScope(scope)
            case _ as unreachable:
                assert_never(unreachable)
    except ValueError as error:
        raise ResourceError("RESOURCE_SCOPE_INVALID", "Only host-local resources are currently supported.") from error
    declaration = ResourceDeclaration(resource_id, label, selected_scope)
    text = (
        render_v2_header(
            {
                "kind": "work-resource",
                "schema": "repo-work/v2",
                "resource": resource_id,
                "label": label,
                "scope": selected_scope.value,
                "mode": "exclusive",
            }
        )
        + f"\n# {label}\n"
    )
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        path = _resource_path(root, resource_id)
        try:
            require_coordination(
                root,
                coordination_lease_id,
                coordination_generation,
                now=_now(now),
            )
        except LeaseError as error:
            raise ResourceError(error.code, str(error).partition(": ")[2]) from error
        if path.exists():
            existing = read_resource(root, resource_id)
            if existing != declaration:
                raise ResourceError("RESOURCE_ALREADY_DECLARED", f"Resource '{resource_id}' already differs.")
            return existing
        atomic_write_text(path, text)
    return declaration


def read_resource(work_root: Path, resource_id: str) -> ResourceDeclaration:
    path = _resource_path(work_root, resource_id)
    if not path.is_file():
        raise ResourceError("RESOURCE_NOT_DECLARED", f"Resource '{resource_id}' is not declared.")
    header = parse_header(path)
    if header.get("kind") != "work-resource" or header.get("schema") != "repo-work/v2":
        raise ResourceError("RESOURCE_DECLARATION_INVALID", f"'{path}' is not a schema-v2 resource.")
    if header.get("mode") != "exclusive":
        raise ResourceError("RESOURCE_DECLARATION_INVALID", "Only exclusive resources are supported.")
    try:
        scope = ResourceScope(_required(header, path, "scope"))
    except ValueError as error:
        raise ResourceError("RESOURCE_SCOPE_INVALID", f"'{path}' has an unsupported scope.") from error
    declared_id = _required(header, path, "resource")
    if declared_id != resource_id or path.stem != resource_id:
        raise ResourceError("RESOURCE_IDENTITY_MISMATCH", f"'{path}' does not describe resource '{resource_id}'.")
    return ResourceDeclaration(declared_id, _required(header, path, "label"), scope)


def _claim_text(claim: ResourceClaim) -> str:
    return (
        render_v2_header(
            {
                "kind": "resource-claim",
                "schema": "repo-work/v2",
                "resource": claim.resource_id,
                "attempt": claim.attempt_id,
                "attempt_lease_id": claim.attempt_lease_id,
                "attempt_lease_generation": claim.attempt_lease_generation,
                "owner_task_id": claim.task_id,
                "owner_host_id": claim.host_id,
                "lease_id": claim.lease_id,
                "lease_generation": claim.generation,
                "lease_acquired_at": _timestamp(claim.acquired_at),
                "lease_expires_at": _timestamp(claim.expires_at),
                "lease_status": claim.status.value,
            }
        )
        + f"\n# Resource Claim: {claim.resource_id}\n"
    )


def read_resource_claim(work_root: Path, resource_id: str, host_id: str) -> ResourceClaim:
    path = _claim_path(work_root, resource_id, host_id)
    if not path.is_file():
        raise ResourceError("RESOURCE_CLAIM_REQUIRED", f"Resource '{resource_id}' has no claim on '{host_id}'.")
    header = parse_header(path)
    if header.get("kind") != "resource-claim" or header.get("schema") != "repo-work/v2":
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' is not a schema-v2 resource claim.")
    try:
        generation = int(_required(header, path, "lease_generation"))
        attempt_generation = int(_required(header, path, "attempt_lease_generation"))
        status = ResourceClaimStatus(_required(header, path, "lease_status"))
    except ValueError as error:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' has invalid fencing fields.") from error
    if generation < 1 or attempt_generation < 1:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' has negative or zero fencing fields.")
    claimed_resource = _required(header, path, "resource")
    claimed_host = _required(header, path, "owner_host_id")
    if claimed_resource != resource_id or claimed_host != host_id:
        raise ResourceError(
            "RESOURCE_IDENTITY_MISMATCH",
            f"'{path}' does not describe resource '{resource_id}' on host '{host_id}'.",
        )
    attempt_id = _required(header, path, "attempt")
    if ITEM_PATTERN.fullmatch(attempt_id) is None:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' has an invalid attempt identity.")
    acquired_at = _parse_timestamp(_required(header, path, "lease_acquired_at"), path)
    expires_at = _parse_timestamp(_required(header, path, "lease_expires_at"), path)
    if expires_at < acquired_at:
        raise ResourceError("RESOURCE_CLAIM_INVALID", f"'{path}' has an expiry before acquisition.")
    return ResourceClaim(
        resource_id=claimed_resource,
        attempt_id=attempt_id,
        task_id=_required(header, path, "owner_task_id"),
        host_id=claimed_host,
        lease_id=_required(header, path, "lease_id"),
        generation=generation,
        acquired_at=acquired_at,
        expires_at=expires_at,
        status=status,
        attempt_lease_id=_required(header, path, "attempt_lease_id"),
        attempt_lease_generation=attempt_generation,
    )


def _active(claim: ResourceClaim, current: datetime) -> bool:
    return claim.status == ResourceClaimStatus.ACTIVE and current < claim.expires_at


def claim_resource(
    work_root: Path,
    resource_id: str,
    attempt_id: str,
    task_id: str,
    host_id: str,
    ttl_seconds: int,
    attempt_lease_id: str,
    attempt_lease_generation: int,
    *,
    now: datetime | None = None,
    lease_id: str | None = None,
) -> ResourceClaim:
    current = _now(now)
    if ttl_seconds <= 0:
        raise ResourceError("RESOURCE_CLAIM_REQUIRED", "A supported scope and positive TTL are required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        declaration = read_resource(root, resource_id)
        if declaration.scope != ResourceScope.HOST_LOCAL:
            raise ResourceError("RESOURCE_CLAIM_REQUIRED", "A supported scope and positive TTL are required.")
        try:
            attempt = require_attempt(root, attempt_id, attempt_lease_id, attempt_lease_generation, now=current)
        except LeaseError as error:
            raise ResourceError(error.code, str(error).partition(": ")[2]) from error
        if attempt.task_id != task_id or attempt.host_id != host_id:
            raise ResourceError(
                "ATTEMPT_LEASE_REQUIRED", "Resource claimant must match the current attempt owner and host."
            )
        path = _claim_path(root, resource_id, host_id)
        previous = read_resource_claim(root, resource_id, host_id) if path.is_file() else None
        if (
            previous is not None
            and previous.status == ResourceClaimStatus.RESERVED
            and previous.attempt_id != attempt_id
        ):
            raise ResourceError(
                "RESOURCE_BUSY",
                f"Resource '{resource_id}' on '{host_id}' is reserved by paused attempt '{previous.attempt_id}'.",
            )
        if previous is not None and _active(previous, current):
            try:
                _require_claim(root, resource_id, host_id, previous.lease_id, previous.generation, current)
            except ResourceError as error:
                if error.code not in {"ATTEMPT_LEASE_EXPIRED", "LEASE_FENCED"}:
                    raise
            else:
                raise ResourceError(
                    "RESOURCE_BUSY",
                    f"Resource '{resource_id}' on '{host_id}' is held by attempt '{previous.attempt_id}' "
                    f"in task '{previous.task_id}' until {previous.expires_at.isoformat()}.",
                )
        claim = ResourceClaim(
            resource_id,
            attempt_id,
            task_id,
            host_id,
            lease_id or uuid4().hex,
            (previous.generation if previous is not None else 0) + 1,
            current,
            current + timedelta(seconds=ttl_seconds),
            ResourceClaimStatus.ACTIVE,
            attempt_lease_id,
            attempt_lease_generation,
        )
        atomic_write_text(path, _claim_text(claim))
        return claim


def _require_claim(
    work_root: Path,
    resource_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    current: datetime,
) -> ResourceClaim:
    claim = read_resource_claim(work_root, resource_id, host_id)
    if claim.lease_id != lease_id or claim.generation != generation or claim.status != ResourceClaimStatus.ACTIVE:
        raise ResourceError("LEASE_FENCED", "The resource claim was released, revoked, or superseded.")
    if current >= claim.expires_at:
        raise ResourceError("RESOURCE_CLAIM_REQUIRED", f"The resource claim expired at {claim.expires_at.isoformat()}.")
    try:
        attempt = require_attempt(
            work_root,
            claim.attempt_id,
            claim.attempt_lease_id,
            claim.attempt_lease_generation,
            now=current,
        )
    except LeaseError as error:
        raise ResourceError(error.code, str(error).partition(": ")[2]) from error
    if attempt.task_id != claim.task_id or attempt.host_id != claim.host_id:
        raise ResourceError("LEASE_FENCED", "The resource claim no longer belongs to the current attempt owner.")
    return claim


def require_resource(
    work_root: Path,
    resource_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    *,
    now: datetime | None = None,
) -> ResourceClaim:
    return _require_claim(work_root, resource_id, host_id, lease_id, generation, _now(now))


def renew_resource(
    work_root: Path,
    resource_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
) -> ResourceClaim:
    current = _now(now)
    if ttl_seconds <= 0:
        raise ResourceError("RESOURCE_CLAIM_REQUIRED", "A positive TTL is required.")
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        claim = _require_claim(root, resource_id, host_id, lease_id, generation, current)
        renewed = ResourceClaim(
            claim.resource_id,
            claim.attempt_id,
            claim.task_id,
            claim.host_id,
            claim.lease_id,
            claim.generation,
            claim.acquired_at,
            current + timedelta(seconds=ttl_seconds),
            ResourceClaimStatus.ACTIVE,
            claim.attempt_lease_id,
            claim.attempt_lease_generation,
        )
        atomic_write_text(_claim_path(root, resource_id, host_id), _claim_text(renewed))
        return renewed


def release_resource(
    work_root: Path,
    resource_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    *,
    now: datetime | None = None,
) -> ResourceClaim:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        claim = _require_claim(root, resource_id, host_id, lease_id, generation, current)
        released = ResourceClaim(
            claim.resource_id,
            claim.attempt_id,
            claim.task_id,
            claim.host_id,
            claim.lease_id,
            claim.generation,
            claim.acquired_at,
            current,
            ResourceClaimStatus.RELEASED,
            claim.attempt_lease_id,
            claim.attempt_lease_generation,
        )
        atomic_write_text(_claim_path(root, resource_id, host_id), _claim_text(released))
        return released


def revoke_resource(
    work_root: Path,
    resource_id: str,
    host_id: str,
    coordination_lease_id: str,
    coordination_generation: int,
    *,
    now: datetime | None = None,
) -> ResourceClaim:
    current = _now(now)
    with authority_transaction(work_root) as authority:
        root = _v2_root(authority)
        try:
            require_coordination(
                root,
                coordination_lease_id,
                coordination_generation,
                now=current,
            )
        except LeaseError as error:
            raise ResourceError(error.code, str(error).partition(": ")[2]) from error
        claim = read_resource_claim(root, resource_id, host_id)
        revoked = ResourceClaim(
            claim.resource_id,
            claim.attempt_id,
            claim.task_id,
            claim.host_id,
            claim.lease_id,
            claim.generation + 1,
            claim.acquired_at,
            current,
            ResourceClaimStatus.REVOKED,
            claim.attempt_lease_id,
            claim.attempt_lease_generation,
        )
        atomic_write_text(_claim_path(root, resource_id, host_id), _claim_text(revoked))
        return revoked
