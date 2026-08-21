from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal

SCHEMA_V1: Final = "repo-work/v1"
SCHEMA_V2: Final = "repo-work/v2"
type SchemaV1 = Literal["repo-work/v1"]


class WorkState(Enum):
    INTAKE = "intake"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"


class AttemptState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CLOSED = "closed"


TERMINAL_STATES: Final = frozenset({"done", "superseded", "dropped"})


type HeaderValue = str | bool | None
type Header = dict[str, HeaderValue]


@dataclass(frozen=True, slots=True)
class QueueItem:
    item: str
    state: WorkState
    timing: str | None
    depends_on: tuple[str, ...]
    attempt: str | None
    source: str
    next_action: str | None
    notes: str
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class Queue:
    path: Path
    header: Header
    items: tuple[QueueItem, ...]
    revision: str

    def by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    path: Path
    item: str
    user_label: str
    queue_item: QueueItem | None = None
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    path: Path
    focus_item: str | None
    focus_attempt: str | None
    next_action: str


@dataclass(frozen=True, slots=True)
class Attempt:
    path: Path
    attempt: str
    item: str
    state: AttemptState
    branch: str
    base_revision: str
    provenance: str


class ArtifactRole(Enum):
    REQUIREMENTS = "requirements"
    PLAN = "plan"
    DESIGN = "design"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class ScopeArtifact:
    role: ArtifactRole
    position: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ScopeDependency:
    position: int
    dependency_id: str


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    position: int
    resource_id: str


@dataclass(frozen=True, slots=True)
class ItemScope:
    item_id: str
    user_label: str
    trigger: str | None
    why_it_matters: str | None
    effect: str | None
    unlock: str | None
    dependencies: tuple[ScopeDependency, ...] = ()
    resource_requirements: tuple[ResourceRequirement, ...] = ()
    artifacts: tuple[ScopeArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeAnchor:
    item: str
    revision: int
    digest: str
    scope: ItemScope


class PlanningDisposition(Enum):
    UNCHANGED = "unchanged"
    REVISED = "revised"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    DROPPED = "dropped"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class PlanningObligation:
    target: str
    position: int
    observed_scope_revision: int
    observed_scope_digest: str = ""
    disposition: PlanningDisposition | None = None
    evaluated_scope_revision: int | None = None
    evaluated_scope_digest: str | None = None
    resulting_scope_revision: int | None = None
    resulting_scope_digest: str | None = None
    replacements: tuple[str, ...] = ()
    outcome_evidence: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningImpact:
    impact_id: str
    source_item: str
    source_attempt: str | None
    source_scope_revision: int
    source_scope_digest: str
    summary: str
    evidence: str
    obligations: tuple[PlanningObligation, ...]


class ReservationState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    REVOKED_PENDING_RECOVERY = "revoked-pending-recovery"


class UseLeaseState(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    resource_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class ResourceInstance:
    instance_id: str
    resource_id: str
    host_id: str
    subject_revision: int


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: str
    resource_id: str
    instance_id: str
    attempt: str
    generation: int
    state: ReservationState


@dataclass(frozen=True, slots=True)
class ResourceUseLease:
    lease_id: str
    reservation_id: str
    attempt_lease_id: str
    attempt_generation: int
    generation: int
    state: UseLeaseState


@dataclass(frozen=True, slots=True)
class ResourceAuthority:
    resource_id: str
    host_id: str
    lease_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    attempt: str
    item: str
    lease_id: str | None
    generation: int
    resources: tuple[ResourceAuthority, ...] = ()


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal: str
    revision: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: str
    item: str
    state: AttemptState
    accepted_scope_revision: int | None = None
    accepted_scope_digest: str | None = None
    protected_candidate_revision: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectRevision:
    subject: str
    revision: str


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    revision: str
    generation: int
    items: tuple[QueueItem, ...]
    attempts: tuple[AttemptRecord, ...] = ()
    proposals: tuple[ProposalRecord, ...] = ()
    subject_revisions: tuple[SubjectRevision, ...] = ()
    attempt_authorities: tuple[AttemptAuthority, ...] = ()
    history_items: tuple[str, ...] = ()
    scopes: tuple[ScopeAnchor, ...] = ()
    planning_impacts: tuple[PlanningImpact, ...] = ()
    resource_definitions: tuple[ResourceDefinition, ...] = ()
    resource_instances: tuple[ResourceInstance, ...] = ()
    resource_reservations: tuple[ResourceReservation, ...] = ()
    resource_use_leases: tuple[ResourceUseLease, ...] = ()
    host_epoch: int = 0
    focus_item: str | None = None
    focus_attempt: str | None = None
    can_transfer_coordinator: bool = False

    def items_by_id(self) -> dict[str, QueueItem]:
        return {item.item: item for item in self.items}

    def attempts_by_id(self) -> dict[str, AttemptRecord]:
        return {attempt.attempt: attempt for attempt in self.attempts}

    def proposal_revisions(self) -> dict[str, str]:
        return {proposal.proposal: proposal.revision for proposal in self.proposals}

    def subject_revision(self, subject: str) -> str | None:
        return next((value.revision for value in self.subject_revisions if value.subject == subject), None)

    def authority_for(self, attempt: str, lease_id: str | None, generation: int) -> AttemptAuthority | None:
        return next(
            (
                authority
                for authority in self.attempt_authorities
                if authority.attempt == attempt
                and authority.lease_id == lease_id
                and authority.generation == generation
            ),
            None,
        )
