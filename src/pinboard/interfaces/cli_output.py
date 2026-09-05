"""Concrete stdout effects shared by installed command presenters."""

import sys
from datetime import datetime

import msgspec

from pinboard.application import stored_state

type RetainedAuthorityLease = (
    tuple[stored_state.StoredAttemptLease, stored_state.AttemptLeaseGeneration]
    | tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration]
)


def authority_lease_fields(
    *,
    task_id: str,
    host_id: str,
    lease_id: str,
    generation: int,
    acquired_at: datetime,
    expires_at: datetime,
    status: str,
) -> dict[str, str | int]:
    """Project the common identity and timing fields of an authority lease."""

    return {
        "task_id": task_id,
        "host_id": host_id,
        "lease_id": lease_id,
        "generation": generation,
        "acquired_at": acquired_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": status,
    }


def retained_authority_lease_fields(retained: RetainedAuthorityLease) -> dict[str, str | int]:
    """Project common status fields from one retained attempt or preparation lease."""

    lease, anchor = retained
    return authority_lease_fields(
        task_id=str(anchor.task_id),
        host_id=str(anchor.host_id),
        lease_id=str(anchor.lease_id),
        generation=lease.generation,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        status=lease.state.value,
    )


def write_json[T](value: T) -> None:
    """Write one canonical, human-readable JSON value and nothing else."""

    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")
