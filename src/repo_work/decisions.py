import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Literal, assert_never, cast, overload

from repo_work.model import (
    ArtifactRole,
    AttemptAuthority,
    AttemptState,
    ItemScope,
    LedgerSnapshot,
    PlanningDisposition,
    PlanningImpact,
    PlanningObligation,
    QueueItem,
    ReservationState,
    ResourceAuthority,
    ResourceRequirement,
    ResourceReservation,
    ResourceUseLease,
    ScopeAnchor,
    ScopeArtifact,
    ScopeDependency,
    UseLeaseState,
    WorkState,
)
from repo_work.transition_input import (
    AcceptProposalInput,
    ActivateInput,
    BlockInput,
    CloseInput,
    CloseOutcome,
    DeferInput,
    EmptyInput,
    EvidenceInput,
    MergeProposalInput,
    ReasonInput,
    TransferCoordinatorInput,
    TransitionInput,
)

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class DecisionErrorCode(Enum):
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"
    ACTION_NOT_MUTATING = "ACTION_NOT_MUTATING"
    ATTEMPT_AUTHORITY_REQUIRED = "ATTEMPT_AUTHORITY_REQUIRED"
    ATTEMPT_LEASE_REQUIRED = "ATTEMPT_LEASE_REQUIRED"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    DEPENDENCY_NOT_SATISFIED = "DEPENDENCY_NOT_SATISFIED"
    HISTORY_OUTCOME_INVALID = "HISTORY_OUTCOME_INVALID"
    HISTORY_RECORD_EXISTS = "HISTORY_RECORD_EXISTS"
    ITEM_ALREADY_EXISTS = "ITEM_ALREADY_EXISTS"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    ITEM_SCOPE_INVALID = "ITEM_SCOPE_INVALID"
    ITEM_SCOPE_STALE = "ITEM_SCOPE_STALE"
    LIVE_DEPENDENTS = "LIVE_DEPENDENTS"
    PLANNING_ACTION_STALE = "PLANNING_ACTION_STALE"
    PLANNING_IMPACT_INVALID = "PLANNING_IMPACT_INVALID"
    PLANNING_IMPACT_UNRESOLVED = "PLANNING_IMPACT_UNRESOLVED"
    PLANNING_OBLIGATION_NOT_FOUND = "PLANNING_OBLIGATION_NOT_FOUND"
    PLANNING_RESOLUTION_INVALID = "PLANNING_RESOLUTION_INVALID"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    RESOURCE_INSTANCE_REQUIRED = "RESOURCE_INSTANCE_REQUIRED"
    RESOURCE_INSTANCE_RESERVED = "RESOURCE_INSTANCE_RESERVED"
    RESOURCE_REQUIREMENT_INVALID = "RESOURCE_REQUIREMENT_INVALID"
    RESOURCE_RESERVATION_STALE = "RESOURCE_RESERVATION_STALE"
    RESOURCE_USE_LEASE_STALE = "RESOURCE_USE_LEASE_STALE"
    TRANSITION_INPUT_INVALID = "TRANSITION_INPUT_INVALID"


class DecisionError(RuntimeError):
    code: DecisionErrorCode

    def __init__(self, code: DecisionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class ActionKind(Enum):
    ACCEPT_PROPOSAL = "accept-proposal"
    ACTIVATE = "activate"
    BLOCK = "block"
    BLOCK_ITEM = "block-item"
    COMPLETE = "complete"
    CLOSE = "close"
    CONTINUE = "continue"
    DEFER = "defer"
    DISPATCH = "dispatch"
    INSPECT = "inspect"
    MARK_READY = "mark-ready"
    MERGE_PROPOSAL = "merge-proposal"
    PAUSE = "pause"
    REJECT_PROPOSAL = "reject-proposal"
    REOPEN = "reopen"
    REPORT_BLOCKER = "report-blocker"
    RESUME = "resume"
    RETURN_FOR_CORRECTION = "return-for-correction"
    RETURN_PROPOSAL = "return-proposal"
    SUBMIT_REVIEW = "submit-review"
    TRANSFER_COORDINATOR = "transfer-coordinator"


class AuthorizationKind(Enum):
    COORDINATOR = "coordinator"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    OBSERVER = "observer"


class Role(Enum):
    COORDINATOR = "coordinator"
    WORKER = "worker"
    OBSERVER = "observer"


@dataclass(frozen=True, slots=True)
class ResourceToken:
    resource_id: str
    host_id: str
    lease_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class Action:
    action_id: str
    kind: ActionKind
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str | None = None
    authorization: AuthorizationKind = AuthorizationKind.COORDINATOR
    lease_id: str | None = None
    resource_claims: tuple[ResourceToken, ...] = ()


@dataclass(frozen=True, slots=True)
class ActorAuthority:
    role: Role
    authorization: AuthorizationKind
    generation: int
    lease_id: str | None = None
    attempts: tuple[str, ...] = ()
    revision_scoped: bool = True


@dataclass(frozen=True, slots=True)
class ActionFactory:
    revision: str
    actor: ActorAuthority

    def make(
        self,
        kind: ActionKind,
        subject: str,
        label: str,
        subject_revision: str | None = None,
        resource_claims: tuple[ResourceToken, ...] = (),
    ) -> Action:
        return Action(
            action_id=f"{kind.value}:{subject}",
            kind=kind,
            subject=subject,
            label=label,
            expected_revision=self.revision,
            coordinator_generation=self.actor.generation,
            subject_revision=subject_revision,
            authorization=self.actor.authorization,
            lease_id=self.actor.lease_id,
            resource_claims=resource_claims,
        )


@dataclass(frozen=True, slots=True)
class ItemChange:
    item: str
    before: WorkState | None
    after: WorkState | None
    attempt: str | None = None
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptChange:
    attempt: str
    before: AttemptState | None
    after: AttemptState | None


@dataclass(frozen=True, slots=True)
class AttemptAuthorityChange:
    before: AttemptAuthority
    after: AttemptAuthority


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    action_id: str
    item: str | None
    outcome: str
    evidence: str | None
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    outcome_schema: str
    payload: bytes


class ResourceDecisionKind(Enum):
    ASSIGN = "assign"
    RELEASE = "release"
    REALLOCATE = "reallocate"
    REVOKE = "revoke"


@dataclass(frozen=True, slots=True)
class ReservationChange:
    before: ResourceReservation | None
    after: ResourceReservation


@dataclass(frozen=True, slots=True)
class ResourceUseLeaseChange:
    before: ResourceUseLease
    after: ResourceUseLease


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    item_change: ItemChange | None
    attempt_change: AttemptChange | None
    receipt: TransitionReceipt
    attempt_authority_change: AttemptAuthorityChange | None = None
    reservation_changes: tuple[ReservationChange, ...] = ()
    resource_use_lease_changes: tuple[ResourceUseLeaseChange, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    kind: ResourceDecisionKind
    changes: tuple[ReservationChange, ...]


@dataclass(frozen=True, slots=True)
class PlanningResolutionDecision:
    impact: PlanningImpact
    item_change: ItemChange | None
    attempt_change: AttemptChange | None


def _resource_token(value: ResourceAuthority) -> ResourceToken:
    return ResourceToken(value.resource_id, value.host_id, value.lease_id, value.generation)


def _authority(snapshot: LedgerSnapshot, actor: ActorAuthority, attempt: str) -> AttemptAuthority | None:
    if attempt not in actor.attempts:
        return None
    return snapshot.authority_for(attempt, actor.lease_id, actor.generation)


def validate_mutation_resources(
    snapshot: LedgerSnapshot,
    attempt: str,
    required_resources: tuple[str, ...],
    tokens: tuple[ResourceToken, ...],
) -> None:
    authorities = tuple(value for value in snapshot.attempt_authorities if value.attempt == attempt)
    if len(authorities) != 1 or authorities[0].lease_id is None:
        raise DecisionError(DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED, "Mutation requires one current attempt authority.")
    authority = authorities[0]
    if len(required_resources) != len(set(required_resources)):
        raise DecisionError(DecisionErrorCode.RESOURCE_REQUIREMENT_INVALID, "Required resources must be unique.")
    token_by_resource = {token.resource_id: token for token in tokens}
    if set(token_by_resource) != set(required_resources) or len(token_by_resource) != len(tokens):
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, "Mutation requires one exact token per resource requirement.")
    instances = {value.instance_id: value for value in snapshot.resource_instances}
    for resource_id in required_resources:
        reservation = next(
            (
                value
                for value in snapshot.resource_reservations
                if value.resource_id == resource_id
                and value.attempt == attempt
                and value.state == ReservationState.ACTIVE
            ),
            None,
        )
        if reservation is None:
            raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, f"Resource '{resource_id}' is not reserved by this attempt.")
        instance = instances.get(reservation.instance_id)
        token = token_by_resource[resource_id]
        if instance is None or instance.host_id != token.host_id:
            raise DecisionError(DecisionErrorCode.RESOURCE_INSTANCE_REQUIRED, f"Resource '{resource_id}' has no matching host-local instance.")
        if ResourceAuthority(token.resource_id, token.host_id, token.lease_id, token.generation) not in authority.resources:
            raise DecisionError(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, f"Resource '{resource_id}' is not held by this attempt authority.")
        use_lease = next(
            (
                value
                for value in snapshot.resource_use_leases
                if value.reservation_id == reservation.reservation_id
                and value.lease_id == token.lease_id
                and value.generation == token.generation
                and value.attempt_lease_id == authority.lease_id
                and value.attempt_generation == authority.generation
                and value.state == UseLeaseState.ACTIVE
            ),
            None,
        )
        if use_lease is None:
            raise DecisionError(DecisionErrorCode.RESOURCE_USE_LEASE_STALE, f"Resource '{resource_id}' has no current mutation lease.")


def _reservation(snapshot: LedgerSnapshot, reservation_id: str) -> ResourceReservation:
    reservation = next(
        (value for value in snapshot.resource_reservations if value.reservation_id == reservation_id),
        None,
    )
    if reservation is None:
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, f"Reservation '{reservation_id}' does not exist.")
    return reservation


def assign_resource(
    snapshot: LedgerSnapshot,
    *,
    reservation_id: str,
    resource_id: str,
    instance_id: str,
    attempt: str,
    generation: int,
) -> ResourceDecision:
    definitions = {value.resource_id for value in snapshot.resource_definitions}
    instance = next((value for value in snapshot.resource_instances if value.instance_id == instance_id), None)
    if resource_id not in definitions or instance is None or instance.resource_id != resource_id:
        raise DecisionError(DecisionErrorCode.RESOURCE_INSTANCE_REQUIRED, "Assignment requires a matching definition and instance.")
    if generation < 1 or not reservation_id:
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, "Reservation identity and generation must be current.")
    active = tuple(
        value for value in snapshot.resource_reservations if value.state == ReservationState.ACTIVE
    )
    if any(value.instance_id == instance_id for value in active):
        raise DecisionError(DecisionErrorCode.RESOURCE_INSTANCE_RESERVED, f"Instance '{instance_id}' is already reserved.")
    if any(value.attempt == attempt and value.resource_id == resource_id for value in active):
        raise DecisionError(DecisionErrorCode.RESOURCE_INSTANCE_RESERVED, "The attempt already has this resource requirement assigned.")
    reservation = ResourceReservation(
        reservation_id,
        resource_id,
        instance_id,
        attempt,
        generation,
        ReservationState.ACTIVE,
    )
    return ResourceDecision(ResourceDecisionKind.ASSIGN, (ReservationChange(None, reservation),))


def release_resource(snapshot: LedgerSnapshot, reservation_id: str) -> ResourceDecision:
    reservation = _reservation(snapshot, reservation_id)
    if reservation.state != ReservationState.ACTIVE:
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, "Only an active reservation can be released.")
    released = replace(reservation, state=ReservationState.RELEASED)
    return ResourceDecision(ResourceDecisionKind.RELEASE, (ReservationChange(reservation, released),))


def revoke_resource(
    snapshot: LedgerSnapshot,
    reservation_id: str,
    *,
    unresolved_intent: bool,
) -> ResourceDecision:
    reservation = _reservation(snapshot, reservation_id)
    if reservation.state != ReservationState.ACTIVE:
        raise DecisionError(DecisionErrorCode.RESOURCE_RESERVATION_STALE, "Only an active reservation can be revoked.")
    state = ReservationState.REVOKED_PENDING_RECOVERY if unresolved_intent else ReservationState.REVOKED
    revoked = replace(reservation, generation=reservation.generation + 1, state=state)
    return ResourceDecision(ResourceDecisionKind.REVOKE, (ReservationChange(reservation, revoked),))


def reallocate_resource(
    snapshot: LedgerSnapshot,
    reservation_id: str,
    *,
    replacement_id: str,
    instance_id: str,
    generation: int,
) -> ResourceDecision:
    previous = _reservation(snapshot, reservation_id)
    released = release_resource(snapshot, reservation_id).changes[0]
    remaining = replace(
        snapshot,
        resource_reservations=tuple(
            value for value in snapshot.resource_reservations if value.reservation_id != reservation_id
        ),
    )
    assigned = assign_resource(
        remaining,
        reservation_id=replacement_id,
        resource_id=previous.resource_id,
        instance_id=instance_id,
        attempt=previous.attempt,
        generation=generation,
    ).changes[0]
    return ResourceDecision(ResourceDecisionKind.REALLOCATE, (released, assigned))


def _unresolved_target(snapshot: LedgerSnapshot, item: str) -> bool:
    return any(
        obligation.target == item and obligation.disposition is None
        for impact in snapshot.planning_impacts
        for obligation in impact.obligations
    )


def _unresolved_source(snapshot: LedgerSnapshot, item: str) -> bool:
    return any(impact.source_item == item and any(value.disposition is None for value in impact.obligations) for impact in snapshot.planning_impacts)


def _scope_stale(snapshot: LedgerSnapshot, item: QueueItem) -> bool:
    if item.attempt is None:
        return False
    attempt = snapshot.attempts_by_id().get(item.attempt)
    scope = next((value for value in snapshot.scopes if value.item == item.item), None)
    if attempt is None or scope is None or attempt.accepted_scope_revision is None:
        return False
    return (attempt.accepted_scope_revision, attempt.accepted_scope_digest) != (scope.revision, scope.digest)


def _item_for_attempt(snapshot: LedgerSnapshot, attempt: str) -> QueueItem | None:
    return next((item for item in snapshot.items if item.attempt == attempt), None)


def _worker_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> tuple[Action, ...]:
    result: list[Action] = []
    for attempt in factory.actor.attempts:
        item = _item_for_attempt(snapshot, attempt)
        authority = _authority(snapshot, factory.actor, attempt)
        if item is None or authority is None or item.state != WorkState.ACTIVE:
            continue
        claims = tuple(_resource_token(value) for value in authority.resources)
        revision = snapshot.subject_revision(item.item)
        result.extend(
            (
                factory.make(ActionKind.CONTINUE, attempt, f"Continue {item.item}", revision, claims),
                factory.make(ActionKind.REPORT_BLOCKER, attempt, f"Report a blocker for {item.item}", revision, claims),
            )
        )
        if not _unresolved_target(snapshot, item.item) and not _scope_stale(snapshot, item):
            result.append(factory.make(ActionKind.SUBMIT_REVIEW, attempt, f"Submit {item.item} for review", revision, claims))
    return tuple(result)


def _active_coordinator_actions(snapshot: LedgerSnapshot, factory: ActionFactory) -> list[Action]:
    result: list[Action] = []
    for item in snapshot.items:
        if item.state not in {WorkState.ACTIVE, WorkState.REVIEW} or item.attempt is None:
            continue
        if item.state == WorkState.ACTIVE:
            result.append(factory.make(ActionKind.CONTINUE, item.attempt, f"Continue {item.item}"))
            if not _unresolved_target(snapshot, item.item) and not _scope_stale(snapshot, item):
                result.append(factory.make(ActionKind.DISPATCH, item.attempt, f"Prepare a worker launch for {item.item}"))
            result.extend(
                (
                    factory.make(ActionKind.PAUSE, item.attempt, f"Pause and preserve {item.item}"),
                    factory.make(ActionKind.BLOCK, item.attempt, f"Block {item.item} on a named condition"),
                )
            )
        if (
            not _unresolved_target(snapshot, item.item)
            and not _unresolved_source(snapshot, item.item)
            and not _scope_stale(snapshot, item)
        ):
            result.append(factory.make(ActionKind.COMPLETE, item.attempt, f"Accept and complete {item.item}"))
        if item.state == WorkState.REVIEW and factory.actor.authorization == AuthorizationKind.COORDINATION:
            result.append(
                factory.make(
                    ActionKind.RETURN_FOR_CORRECTION,
                    item.attempt,
                    f"Return {item.item} for correction",
                )
            )
    return result


def _item_actions(snapshot: LedgerSnapshot, item: QueueItem, factory: ActionFactory) -> list[Action]:
    close = factory.make(ActionKind.CLOSE, item.item, f"Record a terminal decision for {item.item}")
    if item.state == WorkState.INTAKE:
        return [
            factory.make(ActionKind.MARK_READY, item.item, f"Mark {item.item} ready"),
            factory.make(ActionKind.BLOCK_ITEM, item.item, f"Block {item.item} on a named condition"),
            factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"),
            close,
        ]
    if item.state == WorkState.READY:
        actions = [factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"), close]
        if not _unresolved_target(snapshot, item.item):
            actions.insert(0, factory.make(ActionKind.ACTIVATE, item.item, f"Activate {item.item}"))
        return actions
    dependencies_live = any(dependency in snapshot.items_by_id() for dependency in item.depends_on)
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED} and not dependencies_live:
        result = [factory.make(ActionKind.RESUME, item.item, f"Return {item.item} to ready")]
        if item.attempt is None:
            result.append(factory.make(ActionKind.DEFER, item.item, f"Defer {item.item} with a reopen condition"))
        return [*result, close]
    if item.state in {WorkState.PAUSED, WorkState.BLOCKED}:
        return [close]
    if item.state == WorkState.DEFERRED:
        return [factory.make(ActionKind.REOPEN, item.item, f"Reopen {item.item} for intake"), close]
    return []


def available_actions(snapshot: LedgerSnapshot, actor: ActorAuthority) -> tuple[Action, ...]:
    revision = snapshot.revision if actor.revision_scoped else ""
    factory = ActionFactory(revision, actor)
    match actor.role:
        case Role.OBSERVER:
            return (factory.make(ActionKind.INSPECT, "ledger", "Inspect current work"),)
        case Role.WORKER:
            result = _worker_actions(snapshot, factory)
            if not result:
                raise DecisionError(DecisionErrorCode.ATTEMPT_LEASE_REQUIRED, "The supplied attempt lease is not current for an active item.")
            return result
        case Role.COORDINATOR:
            result = _active_coordinator_actions(snapshot, factory)
            for item in snapshot.items:
                result.extend(_item_actions(snapshot, item, factory))
            for proposal in snapshot.proposals:
                for kind, verb in (
                    (ActionKind.ACCEPT_PROPOSAL, "Accept"),
                    (ActionKind.MERGE_PROPOSAL, "Merge"),
                    (ActionKind.RETURN_PROPOSAL, "Return"),
                    (ActionKind.REJECT_PROPOSAL, "Reject"),
                ):
                    result.append(
                        factory.make(kind, proposal.proposal, f"{verb} proposal {proposal.proposal}", proposal.revision)
                    )
            if snapshot.can_transfer_coordinator:
                result.append(factory.make(ActionKind.TRANSFER_COORDINATOR, "ledger", "Transfer coordinator ownership"))
            return tuple(result)
        case _ as unreachable:
            assert_never(unreachable)


def _nonempty(value: str, field: str) -> None:
    if not value:
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty.")


def _positioned(positions: list[int], identities: list[str], field: str) -> None:
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} positions must be zero-based and gapless.")
    if len(identities) != len(set(identities)) or any(not value for value in identities):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} identities must be unique and nonempty.")


def _semantic_artifacts(scope: ItemScope) -> tuple[dict[str, int | str], ...]:
    semantic_roles = {ArtifactRole.REQUIREMENTS, ArtifactRole.PLAN, ArtifactRole.DESIGN}
    artifacts = tuple(value for value in scope.artifacts if value.role in semantic_roles)
    identities: set[tuple[str, str, int]] = set()
    role_positions: dict[ArtifactRole, list[int]] = {}
    for artifact in artifacts:
        if artifact.position < 0:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact positions must be non-negative.")
        if artifact.kind != artifact.role.value:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Semantic artifact kind must equal its role.")
        if artifact.revision < 1:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact revisions must be positive.")
        for field, value in (
            ("artifact key", artifact.key),
            ("artifact selector", artifact.selector),
            ("artifact content digest", artifact.content_sha256),
        ):
            _nonempty(value, field)
        if len(artifact.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in artifact.content_sha256
        ):
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact content digest must be lowercase SHA-256.")
        selector_parts = artifact.selector.split("/")
        if artifact.selector.startswith("/") or any(part in {"", ".", ".."} for part in selector_parts):
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Artifact selector must be a canonical relative POSIX path.")
        identity = (artifact.kind, artifact.key, artifact.revision)
        if identity in identities:
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Semantic artifact identities must be unique.")
        identities.add(identity)
        role_positions.setdefault(artifact.role, []).append(artifact.position)
    for role, positions in role_positions.items():
        ordered = sorted(positions)
        if ordered != list(range(len(ordered))) or len(ordered) != len(set(ordered)):
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"Artifact positions for role '{role.value}' must be gapless.")
    return tuple(
        {
            "content_sha256": artifact.content_sha256,
            "key": artifact.key,
            "kind": artifact.kind,
            "position": artifact.position,
            "revision": artifact.revision,
            "role": artifact.role.value,
            "selector": artifact.selector,
        }
        for artifact in sorted(artifacts, key=_artifact_sort_key)
    )


def _artifact_sort_key(artifact: ScopeArtifact) -> tuple[str, int, str, str, int]:
    return (artifact.role.value, artifact.position, artifact.kind, artifact.key, artifact.revision)


def _dependency_sort_key(value: ScopeDependency) -> tuple[int, str]:
    return (value.position, value.dependency_id)


def _requirement_sort_key(value: ResourceRequirement) -> tuple[int, str]:
    return (value.position, value.resource_id)


def _obligation_sort_key(value: PlanningObligation) -> tuple[int, str]:
    return (value.position, value.target)


def item_scope_bytes(scope: ItemScope) -> bytes:
    _nonempty(scope.item_id, "item ID")
    _nonempty(scope.user_label, "user label")
    for field, value in (
        ("trigger", scope.trigger),
        ("why it matters", scope.why_it_matters),
        ("effect", scope.effect),
        ("unlock", scope.unlock),
    ):
        if value == "":
            raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, f"{field} must be nonempty or null.")
    _positioned(
        [value.position for value in scope.dependencies],
        [value.dependency_id for value in scope.dependencies],
        "Dependency",
    )
    _positioned(
        [value.position for value in scope.resource_requirements],
        [value.resource_id for value in scope.resource_requirements],
        "Resource requirement",
    )
    value = {
        "artifacts": _semantic_artifacts(scope),
        "dependencies": tuple(
            {"dependency_id": dependency.dependency_id, "position": dependency.position}
            for dependency in sorted(scope.dependencies, key=_dependency_sort_key)
        ),
        "effect": scope.effect,
        "item_id": scope.item_id,
        "resource_requirements": tuple(
            {"position": requirement.position, "resource_id": requirement.resource_id}
            for requirement in sorted(
                scope.resource_requirements,
                key=_requirement_sort_key,
            )
        ),
        "schema": "item-scope/v1",
        "trigger": scope.trigger,
        "unlock": scope.unlock,
        "user_label": scope.user_label,
        "why_it_matters": scope.why_it_matters,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def item_scope_digest(scope: ItemScope) -> str:
    return hashlib.sha256(item_scope_bytes(scope)).hexdigest()


def _anchor_value(revision: int, digest: str) -> dict[str, JsonValue]:
    if revision < 1 or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope anchors require a positive revision and lowercase SHA-256.")
    return {"scope_digest": digest, "scope_revision": revision}


def _scope_snapshot_value(anchor: ScopeAnchor) -> dict[str, JsonValue]:
    semantic = cast(JsonValue, json.loads(item_scope_bytes(anchor.scope)))
    return {
        "scope_digest": anchor.digest,
        "scope_revision": anchor.revision,
        "semantic": semantic,
    }


def _history_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def item_scope_change_outcome(before: ScopeAnchor | None, after: ScopeAnchor) -> HistoryOutcome:
    if after.digest != item_scope_digest(after.scope):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "After scope digest does not match its semantic value.")
    if before is not None:
        if before.item != after.item or before.revision + 1 != after.revision or before.digest == after.digest:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope changes require consecutive unequal anchors for one item.")
        if before.digest != item_scope_digest(before.scope):
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Before scope digest does not match its semantic value.")
    elif after.revision != 1:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "An initial scope starts at revision one.")
    payload: dict[str, JsonValue] = {
        "after": _scope_snapshot_value(after),
        "before": None if before is None else _scope_snapshot_value(before),
        "item_id": after.item,
    }
    outcome = HistoryOutcome("item-scope-change/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def planning_impact_outcome(impact: PlanningImpact) -> HistoryOutcome:
    targets: list[JsonValue] = [
        {
            "item_id": obligation.target,
            "position": obligation.position,
            "scope": _anchor_value(obligation.observed_scope_revision, obligation.observed_scope_digest),
        }
        for obligation in sorted(impact.obligations, key=_obligation_sort_key)
    ]
    payload: dict[str, JsonValue] = {
        "evidence": impact.evidence,
        "impact_id": impact.impact_id,
        "source": {
            "attempt_id": impact.source_attempt,
            "item_id": impact.source_item,
            "scope": _anchor_value(impact.source_scope_revision, impact.source_scope_digest),
        },
        "summary": impact.summary,
        "targets": targets,
    }
    outcome = HistoryOutcome("planning-impact/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def planning_resolution_outcome(impact: PlanningImpact, target: str) -> HistoryOutcome:
    obligation = next((value for value in impact.obligations if value.target == target), None)
    if obligation is None or obligation.disposition is None or obligation.reason is None:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning resolution must name one resolved obligation.")
    if obligation.evaluated_scope_revision is None or obligation.evaluated_scope_digest is None:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning resolution requires its evaluated scope.")
    resulting_scope = None
    if obligation.resulting_scope_revision is not None and obligation.resulting_scope_digest is not None:
        resulting_scope = _anchor_value(obligation.resulting_scope_revision, obligation.resulting_scope_digest)
    replacements: list[JsonValue] = [
        {"item_id": item_id, "position": position}
        for position, item_id in enumerate(obligation.replacements)
    ]
    payload: dict[str, JsonValue] = {
        "disposition": obligation.disposition.value,
        "evaluated_scope": _anchor_value(
            obligation.evaluated_scope_revision,
            obligation.evaluated_scope_digest,
        ),
        "impact_id": impact.impact_id,
        "observed_scope": _anchor_value(obligation.observed_scope_revision, obligation.observed_scope_digest),
        "outcome_evidence": obligation.outcome_evidence,
        "reason": obligation.reason,
        "replacements": replacements,
        "resulting_scope": resulting_scope,
        "target_item_id": target,
    }
    outcome = HistoryOutcome("planning-impact-resolution/v1", _history_bytes(payload))
    validate_history_outcome(outcome.outcome_schema, outcome.payload)
    return outcome


def _outcome_mapping(value: JsonValue, keys: frozenset[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome records must be JSON objects.")
    result = value
    if set(result) != keys or any(not isinstance(key, str) for key in result):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome record members do not match the schema.")
    return result


def _outcome_array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome collection must be a JSON array.")
    return value


@overload
def _outcome_string(value: JsonValue, *, nullable: Literal[False] = False) -> str: ...


@overload
def _outcome_string(value: JsonValue, *, nullable: Literal[True]) -> str | None: ...


def _outcome_string(value: JsonValue, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome string must be nonempty.")
    return value


def _outcome_integer(value: JsonValue, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or (positive and value < 1) or (not positive and value < 0):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome integer has an invalid type or range.")
    return value


def _validate_anchor_record(value: JsonValue) -> tuple[int, str]:
    record = _outcome_mapping(value, frozenset({"scope_revision", "scope_digest"}))
    revision = _outcome_integer(record["scope_revision"], positive=True)
    digest = _outcome_string(record["scope_digest"])
    if digest is None or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope digest must be lowercase SHA-256.")
    return revision, digest


def _validate_positioned_records(
    records: list[dict[str, JsonValue]],
    *,
    identity: Callable[[dict[str, JsonValue]], str],
) -> None:
    positions = [_outcome_integer(record["position"]) for record in records]
    identities = [identity(record) for record in records]
    if positions != list(range(len(records))) or len(identities) != len(set(identities)):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome positions or identities are not canonical.")


def _validate_semantic_scope(value: JsonValue) -> tuple[str, str]:
    keys = frozenset(
        {
            "schema",
            "item_id",
            "user_label",
            "trigger",
            "why_it_matters",
            "effect",
            "unlock",
            "dependencies",
            "resource_requirements",
            "artifacts",
        }
    )
    record = _outcome_mapping(value, keys)
    if record["schema"] != "item-scope/v1":
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic scope schema is not item-scope/v1.")
    item_id = _outcome_string(record["item_id"])
    _outcome_string(record["user_label"])
    for field in ("trigger", "why_it_matters", "effect", "unlock"):
        _outcome_string(record[field], nullable=True)
    dependencies = [
        _outcome_mapping(value, frozenset({"position", "dependency_id"}))
        for value in _outcome_array(record["dependencies"])
    ]
    _validate_positioned_records(dependencies, identity=lambda value: _outcome_string(value["dependency_id"]) or "")
    requirements = [
        _outcome_mapping(value, frozenset({"position", "resource_id"}))
        for value in _outcome_array(record["resource_requirements"])
    ]
    _validate_positioned_records(requirements, identity=lambda value: _outcome_string(value["resource_id"]) or "")
    artifacts = [
        _outcome_mapping(
            value,
            frozenset({"role", "position", "kind", "key", "revision", "selector", "content_sha256"}),
        )
        for value in _outcome_array(record["artifacts"])
    ]
    artifact_positions: dict[str, list[int]] = {}
    artifact_identities: set[tuple[str, str, int]] = set()
    artifact_order: list[tuple[str, int, str, str, int]] = []
    for artifact in artifacts:
        role = _outcome_string(artifact["role"])
        kind = _outcome_string(artifact["kind"])
        key = _outcome_string(artifact["key"])
        revision = _outcome_integer(artifact["revision"], positive=True)
        position = _outcome_integer(artifact["position"])
        selector = _outcome_string(artifact["selector"])
        digest = _outcome_string(artifact["content_sha256"])
        if role not in {"requirements", "plan", "design"} or kind != role or selector is None or digest is None:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic artifact identity is invalid.")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact digest must be lowercase SHA-256.")
        artifact_positions.setdefault(role, []).append(position)
        identity = (kind, key, revision)
        if identity in artifact_identities:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Semantic artifact identity is duplicated.")
        artifact_identities.add(identity)
        artifact_order.append((role, position, kind, key, revision))
    if any(positions != list(range(len(positions))) for positions in artifact_positions.values()):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact role positions are not canonical.")
    if artifact_order != sorted(artifact_order):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Artifact order is not canonical.")
    if item_id is None:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope item ID is missing.")
    digest = hashlib.sha256(_history_bytes(record)).hexdigest()
    return item_id, digest


def _validate_scope_snapshot(value: JsonValue) -> tuple[str, int, str]:
    record = _outcome_mapping(value, frozenset({"scope_revision", "scope_digest", "semantic"}))
    revision, digest = _validate_anchor_record(
        {"scope_revision": record["scope_revision"], "scope_digest": record["scope_digest"]}
    )
    item_id, computed = _validate_semantic_scope(record["semantic"])
    if digest != computed:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope digest does not match its semantic value.")
    return item_id, revision, digest


def _validate_scope_change(value: dict[str, JsonValue]) -> None:
    record = _outcome_mapping(value, frozenset({"item_id", "before", "after"}))
    item_id = _outcome_string(record["item_id"])
    after_item, after_revision, after_digest = _validate_scope_snapshot(record["after"])
    if item_id != after_item:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope outcome item IDs do not match.")
    if record["before"] is None:
        if after_revision != 1:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Initial scope revision must be one.")
        return
    before_item, before_revision, before_digest = _validate_scope_snapshot(record["before"])
    if before_item != item_id or before_revision + 1 != after_revision or before_digest == after_digest:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Scope outcome anchors are not a semantic change.")


def _validate_planning_impact_outcome(value: dict[str, JsonValue]) -> None:
    record = _outcome_mapping(value, frozenset({"impact_id", "source", "summary", "evidence", "targets"}))
    _outcome_string(record["impact_id"])
    _outcome_string(record["summary"])
    _outcome_string(record["evidence"])
    source = _outcome_mapping(record["source"], frozenset({"item_id", "attempt_id", "scope"}))
    _outcome_string(source["item_id"])
    _outcome_string(source["attempt_id"], nullable=True)
    _validate_anchor_record(source["scope"])
    targets = [
        _outcome_mapping(value, frozenset({"item_id", "position", "scope"}))
        for value in _outcome_array(record["targets"])
    ]
    if not targets:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning impact targets cannot be empty.")
    for target in targets:
        _validate_anchor_record(target["scope"])
    _validate_positioned_records(targets, identity=lambda value: _outcome_string(value["item_id"]) or "")


def _validate_planning_resolution_outcome(value: dict[str, JsonValue]) -> None:
    keys = frozenset(
        {
            "impact_id",
            "target_item_id",
            "observed_scope",
            "evaluated_scope",
            "resulting_scope",
            "disposition",
            "reason",
            "outcome_evidence",
            "replacements",
        }
    )
    record = _outcome_mapping(value, keys)
    _outcome_string(record["impact_id"])
    _outcome_string(record["target_item_id"])
    _outcome_string(record["reason"])
    _validate_anchor_record(record["observed_scope"])
    evaluated_revision, evaluated_digest = _validate_anchor_record(record["evaluated_scope"])
    disposition_text = _outcome_string(record["disposition"])
    try:
        disposition = PlanningDisposition(disposition_text)
    except ValueError as error:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Planning disposition is invalid.") from error
    replacements = [
        _outcome_mapping(value, frozenset({"position", "item_id"}))
        for value in _outcome_array(record["replacements"])
    ]
    _validate_positioned_records(replacements, identity=lambda value: _outcome_string(value["item_id"]) or "")
    outcome_evidence = _outcome_string(record["outcome_evidence"], nullable=True)
    resulting = record["resulting_scope"]
    if disposition == PlanningDisposition.REVISED:
        resulting_revision, resulting_digest = _validate_anchor_record(resulting)
        if resulting_revision != evaluated_revision + 1 or resulting_digest == evaluated_digest:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Revised scope anchor is invalid.")
    elif resulting is not None:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Only revised resolution may carry a resulting scope.")
    terminal = disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}
    if terminal != (outcome_evidence is not None):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Terminal outcome evidence does not match disposition.")
    if (disposition == PlanningDisposition.SUPERSEDED) != bool(replacements):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Replacement records do not match disposition.")


def validate_history_outcome(outcome_schema: str, payload: bytes) -> None:
    if not payload.endswith(b"\n"):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome JSON requires one final LF.")
    try:
        decoded = cast(JsonValue, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome is not valid UTF-8 JSON.") from error
    if not isinstance(decoded, dict):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome root must be a JSON object.")
    record = decoded
    if any(not isinstance(key, str) for key in record):
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome member names must be strings.")
    if _history_bytes(record) != payload:
        raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, "Outcome JSON is not canonical.")
    match outcome_schema:
        case "item-scope-change/v1":
            _validate_scope_change(record)
        case "planning-impact/v1":
            _validate_planning_impact_outcome(record)
        case "planning-impact-resolution/v1":
            _validate_planning_resolution_outcome(record)
        case _:
            raise DecisionError(DecisionErrorCode.HISTORY_OUTCOME_INVALID, f"Unsupported outcome schema '{outcome_schema}'.")


def advance_scope(previous: ScopeAnchor | None, item: str, scope: ItemScope) -> ScopeAnchor:
    if scope.item_id != item:
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_INVALID, "Scope item ID must match its owning item.")
    digest = item_scope_digest(scope)
    if previous is not None and previous.digest == digest:
        return previous
    return ScopeAnchor(item, 1 if previous is None else previous.revision + 1, digest, scope)


def _validate_impact_scopes(snapshot: LedgerSnapshot, impact: PlanningImpact) -> None:
    live = snapshot.items_by_id()
    if impact.source_scope_revision < 1 or not impact.source_scope_digest:
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact source scope must be an exact anchor.")
    if not impact.summary or not impact.evidence:
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact summary and evidence must be nonempty.")
    if not impact.obligations:
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impacts require at least one explicit target.")
    positions = [value.position for value in impact.obligations]
    targets = [value.target for value in impact.obligations]
    if sorted(positions) != list(range(len(positions))) or len(positions) != len(set(positions)):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact target positions must be gapless.")
    if len(targets) != len(set(targets)) or any(target not in live for target in targets):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact targets must be unique live items.")
    if any(value.observed_scope_revision < 1 or not value.observed_scope_digest for value in impact.obligations):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Every target requires an exact observed scope anchor.")
    scopes = {value.item: value for value in snapshot.scopes}
    source_scope = scopes.get(impact.source_item)
    if source_scope is not None and (source_scope.revision, source_scope.digest) != (
        impact.source_scope_revision,
        impact.source_scope_digest,
    ):
        raise DecisionError(DecisionErrorCode.PLANNING_ACTION_STALE, "Planning impact source scope changed before recording.")
    for obligation in impact.obligations:
        target_scope = scopes.get(obligation.target)
        if target_scope is not None and (target_scope.revision, target_scope.digest) != (
            obligation.observed_scope_revision,
            obligation.observed_scope_digest,
        ):
            raise DecisionError(DecisionErrorCode.PLANNING_ACTION_STALE, f"Target '{obligation.target}' scope changed before recording.")


def validate_planning_impact(snapshot: LedgerSnapshot, impact: PlanningImpact) -> None:
    live = snapshot.items_by_id()
    if impact.source_item not in live:
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact source must be a live item.")
    if impact.source_attempt is not None:
        attempt = snapshot.attempts_by_id().get(impact.source_attempt)
        if attempt is None or attempt.item != impact.source_item or attempt.state in {
            AttemptState.DONE,
            AttemptState.CLOSED,
        }:
            raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_INVALID, "Planning impact source attempt must be live and owned by the source item.")
    _validate_impact_scopes(snapshot, impact)


def _validate_resolution(
    evaluated_scope_revision: int,
    evaluated_scope_digest: str,
    disposition: PlanningDisposition,
    resulting_scope_revision: int | None,
    resulting_scope_digest: str | None,
    replacements: tuple[str, ...],
    outcome_evidence: str | None,
) -> None:
    if disposition == PlanningDisposition.REVISED:
        if (
            resulting_scope_revision != evaluated_scope_revision + 1
            or not resulting_scope_digest
            or resulting_scope_digest == evaluated_scope_digest
        ):
            raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Revised disposition requires a newer target scope.")
    elif resulting_scope_revision is not None or resulting_scope_digest is not None:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Only revised disposition accepts a resulting scope anchor.")
    if disposition == PlanningDisposition.SUPERSEDED:
        if not replacements or len(replacements) != len(set(replacements)):
            raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Superseded disposition requires ordered unique replacements.")
    elif replacements:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Only superseded disposition accepts replacements.")
    if disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        if outcome_evidence is None or not outcome_evidence.strip():
            raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Terminal disposition requires outcome evidence.")
    elif outcome_evidence is not None:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Nonterminal disposition cannot carry outcome evidence.")


def resolve_planning_obligation(
    snapshot: LedgerSnapshot,
    impact: PlanningImpact,
    target: str,
    disposition: PlanningDisposition,
    *,
    reason: str,
    resulting_scope_revision: int | None = None,
    resulting_scope_digest: str | None = None,
    replacements: tuple[str, ...] = (),
    outcome_evidence: str | None = None,
) -> PlanningImpact:
    validate_planning_impact(snapshot, impact)
    if not reason:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Resolution reason must be nonempty.")
    index = next((position for position, value in enumerate(impact.obligations) if value.target == target), None)
    if index is None:
        raise DecisionError(DecisionErrorCode.PLANNING_OBLIGATION_NOT_FOUND, f"Target '{target}' is not part of the impact.")
    current = impact.obligations[index]
    if current.disposition is not None:
        raise DecisionError(DecisionErrorCode.PLANNING_ACTION_STALE, f"Target '{target}' is already reconciled.")
    evaluated_scope = next((value for value in snapshot.scopes if value.item == target), None)
    evaluated_scope_revision = (
        current.observed_scope_revision if evaluated_scope is None else evaluated_scope.revision
    )
    evaluated_scope_digest = current.observed_scope_digest if evaluated_scope is None else evaluated_scope.digest
    _validate_resolution(
        evaluated_scope_revision,
        evaluated_scope_digest,
        disposition,
        resulting_scope_revision,
        resulting_scope_digest,
        replacements,
        outcome_evidence,
    )
    obligations = list(impact.obligations)
    obligations[index] = replace(
        current,
        disposition=disposition,
        evaluated_scope_revision=evaluated_scope_revision,
        evaluated_scope_digest=evaluated_scope_digest,
        resulting_scope_revision=resulting_scope_revision,
        resulting_scope_digest=resulting_scope_digest,
        replacements=replacements,
        outcome_evidence=outcome_evidence,
        reason=reason,
    )
    return replace(impact, obligations=tuple(obligations))


def decide_planning_resolution(
    snapshot: LedgerSnapshot,
    impact: PlanningImpact,
    target: str,
    disposition: PlanningDisposition,
    *,
    reason: str,
    resulting_scope_revision: int | None = None,
    resulting_scope_digest: str | None = None,
    replacements: tuple[str, ...] = (),
    outcome_evidence: str | None = None,
) -> PlanningResolutionDecision:
    item = _item(snapshot, target)
    if disposition == PlanningDisposition.DEFERRED and item.attempt is not None:
        raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "A target with a retained attempt must be blocked, not deferred.")
    if disposition == PlanningDisposition.SUPERSEDED:
        live = snapshot.items_by_id()
        if any(replacement not in live or replacement == target for replacement in replacements):
            raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "Replacements must be distinct live items.")
    updated = resolve_planning_obligation(
        snapshot,
        impact,
        target,
        disposition,
        reason=reason,
        resulting_scope_revision=resulting_scope_revision,
        resulting_scope_digest=resulting_scope_digest,
        replacements=replacements,
        outcome_evidence=outcome_evidence,
    )
    item_change: ItemChange | None = None
    attempt_change: AttemptChange | None = None
    if disposition == PlanningDisposition.BLOCKED:
        item_change = ItemChange(item.item, item.state, WorkState.BLOCKED, item.attempt)
        if item.attempt is not None:
            attempt = snapshot.attempts_by_id().get(item.attempt)
            attempt_change = AttemptChange(
                item.attempt,
                None if attempt is None else attempt.state,
                AttemptState.BLOCKED,
            )
    elif disposition == PlanningDisposition.DEFERRED:
        if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED}:
            raise DecisionError(DecisionErrorCode.PLANNING_RESOLUTION_INVALID, "The target cannot be deferred from its current state.")
        item_change = ItemChange(item.item, item.state, WorkState.DEFERRED)
    elif disposition in {PlanningDisposition.DROPPED, PlanningDisposition.SUPERSEDED}:
        item_change = ItemChange(item.item, item.state, None, item.attempt, outcome_evidence)
        if item.attempt is not None:
            attempt = snapshot.attempts_by_id().get(item.attempt)
            attempt_change = AttemptChange(
                item.attempt,
                None if attempt is None else attempt.state,
                AttemptState.CLOSED,
            )
    return PlanningResolutionDecision(updated, item_change, attempt_change)


def _item(snapshot: LedgerSnapshot, item_id: str) -> QueueItem:
    item = snapshot.items_by_id().get(item_id)
    if item is None:
        raise DecisionError(DecisionErrorCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    return item


def _attempt_item(snapshot: LedgerSnapshot, attempt: str) -> QueueItem:
    item = _item_for_attempt(snapshot, attempt)
    if item is None:
        raise DecisionError(DecisionErrorCode.ATTEMPT_NOT_FOUND, f"Attempt '{attempt}' does not exist.")
    return item


def _receipt(action: Action, item: str | None, outcome: str, evidence: str | None, now: datetime) -> TransitionReceipt:
    return TransitionReceipt(action.action_id, item, outcome, evidence, now)


type DecisionHandler = Callable[[LedgerSnapshot, Action, TransitionInput, datetime], Decision]


def _result(
    action: Action,
    now: datetime,
    *,
    item: str | None = None,
    item_change: ItemChange | None = None,
    attempt_change: AttemptChange | None = None,
    attempt_authority_change: AttemptAuthorityChange | None = None,
    reservation_changes: tuple[ReservationChange, ...] = (),
    resource_use_lease_changes: tuple[ResourceUseLeaseChange, ...] = (),
    outcome: str | None = None,
    evidence: str | None = None,
) -> Decision:
    return Decision(
        action,
        item_change,
        attempt_change,
        _receipt(action, item, outcome or action.kind.value, evidence, now),
        attempt_authority_change,
        reservation_changes,
        resource_use_lease_changes,
    )


def _activate(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, action.subject)
    if item.state != WorkState.READY or _unresolved_target(snapshot, item.item):
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not ready for activation.")
    if not isinstance(value, ActivateInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Activate requires activation input.")
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.ACTIVE, value.attempt),
        attempt_change=AttemptChange(value.attempt, None, AttemptState.ACTIVE),
    )


def _pause_or_block(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _attempt_item(snapshot, action.subject)
    if item.state != WorkState.ACTIVE:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "The named attempt is not active.")
    if action.kind == ActionKind.PAUSE and not isinstance(value, ReasonInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Pause requires a reason.")
    if action.kind == ActionKind.BLOCK and not isinstance(value, BlockInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Block requires a reason and dependencies.")
    target = WorkState.PAUSED if action.kind == ActionKind.PAUSE else WorkState.BLOCKED
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=AttemptChange(action.subject, AttemptState.ACTIVE, AttemptState(target.value)),
    )


def _complete(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _attempt_item(snapshot, action.subject)
    if item.state not in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "The named attempt is not active or in review.")
    if _unresolved_target(snapshot, item.item) or _unresolved_source(snapshot, item.item):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_UNRESOLVED, "Resolve planning impacts before completion.")
    if _scope_stale(snapshot, item):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope.")
    if not isinstance(value, EvidenceInput) or not value.evidence.strip():
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Completion requires outcome evidence.")
    if item.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    before = AttemptState.REVIEW if item.state == WorkState.REVIEW else AttemptState.ACTIVE
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.evidence),
        attempt_change=AttemptChange(action.subject, before, AttemptState.DONE),
        evidence=value.evidence,
    )


def _close(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, action.subject)
    if item.state in {WorkState.ACTIVE, WorkState.REVIEW}:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "Active or review work requires the acceptance path.")
    if not isinstance(value, CloseInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Close requires terminal outcome input.")
    if value.outcome == CloseOutcome.DROPPED and any(item.item in candidate.depends_on for candidate in snapshot.items):
        raise DecisionError(DecisionErrorCode.LIVE_DEPENDENTS, f"Item '{item.item}' still has live dependents.")
    if item.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.HISTORY_RECORD_EXISTS, f"History already contains '{item.item}'.")
    attempt_change = None
    if item.attempt is not None:
        attempt = snapshot.attempts_by_id().get(item.attempt)
        attempt_change = AttemptChange(item.attempt, None if attempt is None else attempt.state, AttemptState.DONE)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, None, item.attempt, value.reason),
        attempt_change=attempt_change,
        outcome=value.outcome.value,
        evidence=value.reason,
    )


def _resume(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, action.subject)
    if item.state not in {WorkState.PAUSED, WorkState.BLOCKED}:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' is not paused or blocked.")
    if any(dependency in snapshot.items_by_id() for dependency in item.depends_on):
        raise DecisionError(DecisionErrorCode.DEPENDENCY_NOT_SATISFIED, f"Item '{item.item}' still has a live dependency.")
    if not isinstance(value, EmptyInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Resume does not accept transition data.")
    target = WorkState.ACTIVE if item.attempt is not None else WorkState.READY
    attempt_change = None
    if item.attempt is not None:
        before = AttemptState.PAUSED if item.state == WorkState.PAUSED else AttemptState.BLOCKED
        attempt_change = AttemptChange(item.attempt, before, AttemptState.ACTIVE)
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, target, item.attempt),
        attempt_change=attempt_change,
    )


def _submit_review(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _attempt_item(snapshot, action.subject)
    if item.state != WorkState.ACTIVE:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "Only an active attempt can be submitted for review.")
    if _unresolved_target(snapshot, item.item):
        raise DecisionError(DecisionErrorCode.PLANNING_IMPACT_UNRESOLVED, "Resolve target planning impacts before review.")
    if _scope_stale(snapshot, item):
        raise DecisionError(DecisionErrorCode.ITEM_SCOPE_STALE, "The attempt has not accepted the item's current semantic scope.")
    if not isinstance(value, EmptyInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Submit review does not accept transition data.")
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, item.state, WorkState.REVIEW, item.attempt),
        attempt_change=AttemptChange(action.subject, AttemptState.ACTIVE, AttemptState.REVIEW),
    )


def _return_for_correction(
    snapshot: LedgerSnapshot,
    action: Action,
    value: TransitionInput,
    now: datetime,
) -> Decision:
    item = _attempt_item(snapshot, action.subject)
    if item.state != WorkState.REVIEW:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "Only an attempt in review can be returned for correction.")
    if not isinstance(value, ReasonInput) or not value.reason.strip():
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Returning a review requires a correction reason.")
    authorities = tuple(candidate for candidate in snapshot.attempt_authorities if candidate.attempt == action.subject)
    if len(authorities) != 1:
        raise DecisionError(
            DecisionErrorCode.ATTEMPT_AUTHORITY_REQUIRED,
            "Returning a review requires exactly one current attempt-authority record to fence.",
        )
    authority = authorities[0]
    authority_change = AttemptAuthorityChange(
        authority,
        replace(authority, lease_id=None, generation=authority.generation + 1, resources=()),
    )
    reservations = tuple(candidate for candidate in snapshot.resource_reservations if candidate.attempt == action.subject)
    reservation_changes = tuple(
        ReservationChange(
            reservation,
            replace(reservation, generation=reservation.generation + 1, state=ReservationState.REVOKED),
        )
        for reservation in reservations
    )
    reservation_ids = {reservation.reservation_id for reservation in reservations}
    use_lease_changes = tuple(
        ResourceUseLeaseChange(
            use_lease,
            replace(use_lease, generation=use_lease.generation + 1, state=UseLeaseState.REVOKED),
        )
        for use_lease in snapshot.resource_use_leases
        if use_lease.reservation_id in reservation_ids
    )
    return _result(
        action,
        now,
        item=item.item,
        item_change=ItemChange(item.item, WorkState.REVIEW, WorkState.ACTIVE, item.attempt),
        attempt_change=AttemptChange(action.subject, AttemptState.REVIEW, AttemptState.ACTIVE),
        attempt_authority_change=authority_change,
        reservation_changes=reservation_changes,
        resource_use_lease_changes=use_lease_changes,
        evidence=value.reason,
    )


def _simple_item_transition(
    snapshot: LedgerSnapshot,
    action: Action,
    value: TransitionInput,
    now: datetime,
) -> Decision:
    item = _item(snapshot, action.subject)
    if action.kind == ActionKind.REOPEN:
        expected, target, valid = WorkState.DEFERRED, WorkState.INTAKE, isinstance(value, EvidenceInput)
    elif action.kind == ActionKind.MARK_READY:
        expected, target, valid = WorkState.INTAKE, WorkState.READY, isinstance(value, ReasonInput)
    else:
        expected, target, valid = item.state, WorkState.BLOCKED, isinstance(value, BlockInput)
        if item.state not in {WorkState.INTAKE, WorkState.READY}:
            raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be blocked now.")
    if item.state != expected:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot perform '{action.kind.value}' now.")
    if not valid:
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, f"Input for '{action.kind.value}' is invalid.")
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, target))


def _defer(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    item = _item(snapshot, action.subject)
    if item.state not in {WorkState.INTAKE, WorkState.READY, WorkState.BLOCKED} or item.attempt is not None:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, f"Item '{item.item}' cannot be deferred now.")
    if not isinstance(value, DeferInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Defer requires a reopen condition.")
    return _result(action, now, item=item.item, item_change=ItemChange(item.item, item.state, WorkState.DEFERRED))


def _accept_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, action.subject)
    if not isinstance(value, AcceptProposalInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Accept proposal requires item input.")
    if value.item in snapshot.items_by_id() or value.item in snapshot.history_items:
        raise DecisionError(DecisionErrorCode.ITEM_ALREADY_EXISTS, f"Item '{value.item}' already exists.")
    change = ItemChange(value.item, None, WorkState(value.state.value))
    return _result(action, now, item=value.item, item_change=change)


def _require_proposal(snapshot: LedgerSnapshot, proposal: str) -> None:
    if proposal not in snapshot.proposal_revisions():
        raise DecisionError(DecisionErrorCode.PROPOSAL_NOT_FOUND, f"Proposal '{proposal}' does not exist.")


def _merge_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, action.subject)
    if not isinstance(value, MergeProposalInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Merge proposal requires a target item.")
    return _result(action, now)


def _dispose_proposal(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    _require_proposal(snapshot, action.subject)
    if not isinstance(value, ReasonInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Proposal disposition requires a reason.")
    return _result(action, now, evidence=value.reason)


def _transfer(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    if not snapshot.can_transfer_coordinator:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_AVAILABLE, "This ledger does not use transferable coordinator ownership.")
    if not isinstance(value, TransferCoordinatorInput):
        raise DecisionError(DecisionErrorCode.TRANSITION_INPUT_INVALID, "Coordinator transfer requires a task and host.")
    return _result(action, now)


DECISION_HANDLERS: dict[ActionKind, DecisionHandler] = {
    ActionKind.ACTIVATE: _activate,
    ActionKind.PAUSE: _pause_or_block,
    ActionKind.BLOCK: _pause_or_block,
    ActionKind.COMPLETE: _complete,
    ActionKind.CLOSE: _close,
    ActionKind.RESUME: _resume,
    ActionKind.SUBMIT_REVIEW: _submit_review,
    ActionKind.RETURN_FOR_CORRECTION: _return_for_correction,
    ActionKind.REOPEN: _simple_item_transition,
    ActionKind.MARK_READY: _simple_item_transition,
    ActionKind.BLOCK_ITEM: _simple_item_transition,
    ActionKind.DEFER: _defer,
    ActionKind.ACCEPT_PROPOSAL: _accept_proposal,
    ActionKind.MERGE_PROPOSAL: _merge_proposal,
    ActionKind.RETURN_PROPOSAL: _dispose_proposal,
    ActionKind.REJECT_PROPOSAL: _dispose_proposal,
    ActionKind.TRANSFER_COORDINATOR: _transfer,
}


def decide(snapshot: LedgerSnapshot, action: Action, value: TransitionInput, now: datetime) -> Decision:
    handler = DECISION_HANDLERS.get(action.kind)
    if handler is None:
        raise DecisionError(DecisionErrorCode.ACTION_NOT_MUTATING, f"Action '{action.kind.value}' is not a canonical transition.")
    return handler(snapshot, action, value, now)
