from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId


@dataclass(frozen=True, slots=True)
class AcquireCoordinationAuthority:
    host_epoch: int
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewCoordinationAuthority:
    authority: work_models.CoordinationCommandAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseCoordinationAuthority:
    authority: work_models.CoordinationCommandAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeCoordinationAuthority:
    lease_id: LeaseId
    generation: int
    revoked_at: datetime


type CoordinationAuthorityOperation = (
    AcquireCoordinationAuthority
    | RenewCoordinationAuthority
    | ReleaseCoordinationAuthority
    | RevokeCoordinationAuthority
)


@dataclass(frozen=True, slots=True)
class CoordinationAuthorityDecision:
    before: work_models.CoordinationLeaseAuthority | None
    after: work_models.CoordinationLeaseAuthority


class AttemptLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PreparationLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PreparationLeaseAuthority:
    host_epoch: int
    item: ItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: PreparationLeaseStatus


@dataclass(frozen=True, slots=True)
class InactivePreparationAuthority:
    host_epoch: int
    item: ItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime
    state: PreparationLeaseStatus


@dataclass(frozen=True, slots=True)
class AcquireInitialPreparationAuthority:
    host_epoch: int
    item: ItemId
    expected_project_revision: str
    expected_item_subject_revision: str
    expected_definition_revision: int
    expected_definition_digest: str
    coordination: work_models.CoordinationCommandAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransferPreparationAuthority:
    current: InactivePreparationAuthority
    coordination: work_models.CoordinationCommandAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewPreparationAuthority:
    current: work_models.PreparationCommandAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleasePreparationAuthority:
    current: work_models.PreparationCommandAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokePreparationAuthority:
    item: ItemId
    lease_id: LeaseId
    generation: int
    coordination: work_models.CoordinationCommandAuthority
    revoked_at: datetime


type PreparationAuthorityOperation = (
    AcquireInitialPreparationAuthority
    | TransferPreparationAuthority
    | RenewPreparationAuthority
    | ReleasePreparationAuthority
    | RevokePreparationAuthority
)


@dataclass(frozen=True, slots=True)
class PreparationAuthorityDecision:
    item: ItemId
    counter_before: int
    counter_after: int
    current_before: PreparationLeaseAuthority | None
    current_after: PreparationLeaseAuthority


@dataclass(frozen=True, slots=True)
class AttemptLeaseAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class InactiveAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class AcquireInitialAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransferAttemptAuthority:
    current: InactiveAttemptAuthority
    coordination: work_models.CoordinationCommandAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewAttemptAuthority:
    current: work_models.CommandAttemptAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseAttemptAuthority:
    current: work_models.CommandAttemptAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeAttemptAuthority:
    attempt: AttemptId
    lease_id: LeaseId
    generation: int
    coordination: work_models.CoordinationCommandAuthority
    revoked_at: datetime


type AttemptAuthorityOperation = (
    AcquireInitialAttemptAuthority
    | TransferAttemptAuthority
    | RenewAttemptAuthority
    | ReleaseAttemptAuthority
    | RevokeAttemptAuthority
)


@dataclass(frozen=True, slots=True)
class AttemptAuthorityDecision:
    attempt: AttemptId
    counter_before: int
    counter_after: int
    current_before: AttemptLeaseAuthority | None
    current_after: AttemptLeaseAuthority
