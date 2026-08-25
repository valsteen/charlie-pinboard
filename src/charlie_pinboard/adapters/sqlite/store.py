import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import assert_never

import msgspec

from charlie_pinboard.adapters.files.artifacts import verify_reference
from charlie_pinboard.adapters.sqlite.database import (
    APPLICATION,
    SCHEMA_VERSION,
    OpenMode,
    StorageError,
    StorageErrorCode,
    open_database,
    read_operation,
    write_transaction,
)
from charlie_pinboard.application.artifacts import ArtifactRef
from charlie_pinboard.application.mutations import (
    AttemptAuthorityMutation,
    CoordinationAuthorityMutation,
    MutationContractError,
    ProposalCreationMutation,
    TransitionMutation,
    TransitionReceiptMutation,
    expected_stored_state,
)
from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    ArtifactRecords,
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    AuthorityRecords,
    CanonicalJson,
    CoordinationLeaseState,
    HistoryRecords,
    ItemArtifactLink,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    LifecycleRecords,
    MutationIntentState,
    OriginKind,
    PlanningObligationState,
    PlanningRecords,
    ProjectRecord,
    ProposalDisposition,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    ProposalRelation,
    ResourceInstanceLocator,
    ResourceInstanceState,
    ResourceMutationIntent,
    ResourceRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredReservationCounter,
    StoredResourceDefinition,
    StoredResourceInstance,
    StoredResourceReservation,
    StoredResourceUseLease,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.decisions import (
    TransitionReceipt,
)
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    PlanningDisposition,
    ReservationState,
    Timing,
    UseLeaseGenerationKind,
    UseLeaseState,
)


def _text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be text.")
    return value


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = row[key]
    if value is None or isinstance(value, str):
        return value
    raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be text or null.")


def _integer(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be an integer.")
    return value


def _optional_integer(row: sqlite3.Row, key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be an integer or null.")
    return value


def _time(row: sqlite3.Row, key: str) -> datetime:
    try:
        return datetime.fromisoformat(_text(row, key))
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an invalid timestamp.") from error


def _optional_time(row: sqlite3.Row, key: str) -> datetime | None:
    value = _optional_text(row, key)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an invalid timestamp.") from error


def _canonical_json(row: sqlite3.Row, key: str) -> CanonicalJson:
    value = _text(row, key)
    try:
        decoded = msgspec.json.decode(value, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has invalid JSON.") from error
    return CanonicalJson(bytes(decoded))


def _stored_json(row: sqlite3.Row, key: str) -> CanonicalJson:
    """Return JSON bytes whose canonical whitespace is owned by the stored history schema."""

    encoded = _text(row, key).encode("utf-8")
    try:
        msgspec.json.decode(encoded, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has invalid JSON.") from error
    return CanonicalJson(encoded)


def _optional_canonical_json(row: sqlite3.Row, key: str) -> CanonicalJson | None:
    value = _optional_text(row, key)
    if value is None:
        return None
    try:
        decoded = msgspec.json.decode(value, type=msgspec.Raw)
    except msgspec.DecodeError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has invalid JSON.") from error
    return CanonicalJson(bytes(decoded))


def _enum_value[EnumValue: Enum](constructor: type[EnumValue], row: sqlite3.Row, key: str) -> EnumValue:
    try:
        return constructor(_text(row, key))
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} has an unsupported value.") from error


def _expect(row: sqlite3.Row, key: str, expected: str) -> None:
    if _text(row, key) != expected:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Column {key!r} must be {expected!r}.")


def _validate_attempt_authority(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    attempt_counters = {value.attempt_id: value.generation_high_water for value in state.authority.attempt_counters}
    for anchor in state.authority.attempt_generations:
        high_water = attempt_counters.get(anchor.attempt_id)
        if high_water is None or anchor.generation > high_water:
            raise StorageError(error_code, "An attempt generation exceeds its retained counter.")
    for lease in state.authority.attempt_leases:
        high_water = attempt_counters.get(lease.attempt_id)
        if high_water is None or lease.generation != high_water:
            raise StorageError(error_code, "The current attempt lease does not match its retained counter.")


def _validate_reservation_authority(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    reservation_counters = {
        value.instance_id: value.generation_high_water for value in state.resources.reservation_counters
    }
    for reservation in state.resources.reservations:
        high_water = reservation_counters.get(reservation.instance_id)
        if high_water is None or reservation.acquisition_generation > high_water:
            raise StorageError(error_code, "A reservation generation exceeds its retained instance counter.")


def _validate_use_authority(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    instances = {value.instance_id: value for value in state.resources.instances}
    locators = {value.instance_id: value for value in state.resources.locators}
    reservations = {value.reservation_id: value for value in state.resources.reservations}
    attempt_leases = {value.attempt_id: value for value in state.authority.attempt_leases}
    attempt_anchors = {(value.attempt_id, value.generation): value for value in state.authority.attempt_generations}
    use_leases_by_reservation: dict[ReservationId, dict[int, StoredResourceUseLease]] = {}
    for lease in state.resources.use_leases:
        use_leases_by_reservation.setdefault(lease.reservation_id, {})[lease.generation] = lease
    for reservation_id, generations in use_leases_by_reservation.items():
        latest_generation = max(generations)
        for lease in generations.values():
            if lease.generation_kind == UseLeaseGenerationKind.GRANT and lease.state == UseLeaseState.REVOKED:
                fence = generations.get(lease.generation + 1)
                if (
                    fence is None
                    or fence.generation_kind != UseLeaseGenerationKind.FENCE
                    or fence.state != UseLeaseState.REVOKED
                ):
                    raise StorageError(error_code, "A revoked task-use grant has no immediately following fence.")
            if lease.state != UseLeaseState.ACTIVE:
                continue
            reservation = reservations.get(reservation_id)
            instance = instances.get(lease.instance_id)
            locator = locators.get(lease.instance_id)
            attempt_lease = attempt_leases.get(lease.attempt_id)
            attempt_anchor = (
                None
                if attempt_lease is None
                else attempt_anchors.get((attempt_lease.attempt_id, attempt_lease.generation))
            )
            if (
                lease.generation != latest_generation
                or lease.generation_kind != UseLeaseGenerationKind.GRANT
                or reservation is None
                or reservation.state != ReservationState.ACTIVE
                or lease.host_epoch != state.lifecycle.project.host_epoch
                or instance is None
                or lease.instance_subject_revision != instance.subject_revision
                or locator is None
                or lease.observation_generation != locator.observation_generation
                or lease.observation_digest != locator.observation_digest
                or attempt_lease is None
                or attempt_lease.state != AttemptLeaseState.ACTIVE
                or attempt_anchor is None
                or lease.attempt_lease_generation != attempt_lease.generation
                or lease.attempt_lease_id != attempt_anchor.lease_id
                or lease.task_id != attempt_anchor.task_id
                or lease.host_id != attempt_anchor.host_id
            ):
                raise StorageError(error_code, "An active task-use lease contradicts current resource authority.")


def _validate_current_state(state: StoredWorkState, error_code: StorageErrorCode) -> None:
    _validate_attempt_authority(state, error_code)
    _validate_reservation_authority(state, error_code)
    _validate_use_authority(state, error_code)


class _StoredStateReader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _rows(self, query: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self._connection.execute(query).fetchall())

    def read(self) -> StoredWorkState:
        state = StoredWorkState(
            self._lifecycle(),
            self._proposals(),
            self._planning(),
            self._artifacts(),
            self._authority(),
            self._resources(),
            self._history(),
            self._focus(),
        )
        _validate_current_state(state, StorageErrorCode.INVALID_STATE)
        return state

    def _project(self) -> ProjectRecord:
        rows = self._rows("SELECT * FROM project_meta ORDER BY singleton")
        if len(rows) != 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain one project record.")
        row = rows[0]
        _expect(row, "application", APPLICATION)
        if _integer(row, "schema_version") != SCHEMA_VERSION:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The opened database schema changed unexpectedly.")
        return ProjectRecord(
            APPLICATION,
            1,
            _integer(row, "revision"),
            _integer(row, "host_epoch"),
            _time(row, "created_at"),
            _time(row, "updated_at"),
        )

    def _lifecycle(self) -> LifecycleRecords:
        items = tuple(
            StoredWorkItem(
                ItemId(_text(row, "item_id")),
                _enum_value(OriginKind, row, "origin_kind"),
                _text(row, "user_label"),
                _enum_value(StoredWorkItemState, row, "state"),
                None if (timing := _optional_text(row, "timing")) is None else Timing(timing),
                _optional_text(row, "source"),
                _optional_text(row, "trigger"),
                _optional_text(row, "why_it_matters"),
                _optional_text(row, "effect"),
                _optional_text(row, "unlock"),
                _optional_text(row, "outcome_evidence"),
                _optional_text(row, "next_action"),
                _optional_text(row, "notes"),
                _integer(row, "scope_revision"),
                _text(row, "scope_digest"),
                _integer(row, "subject_revision"),
                _optional_time(row, "origin_created_at"),
                _optional_time(row, "origin_updated_at"),
                _time(row, "recorded_at"),
                _time(row, "updated_at"),
            )
            for row in self._rows("SELECT * FROM work_items ORDER BY item_id")
        )
        scopes = tuple(
            ItemScopeRevision(
                ItemId(_text(row, "item_id")),
                _integer(row, "scope_revision"),
                _text(row, "scope_digest"),
                _integer(row, "accepted_project_revision"),
                _time(row, "accepted_at"),
            )
            for row in self._rows("SELECT * FROM item_scope_revisions ORDER BY item_id, scope_revision")
        )
        dependencies = tuple(
            ItemDependency(
                ItemId(_text(row, "item_id")),
                ItemId(_text(row, "dependency_id")),
                _integer(row, "position"),
            )
            for row in self._rows("SELECT * FROM item_dependencies ORDER BY item_id, position")
        )
        item_artifacts = tuple(
            ItemArtifactLink(
                ItemId(_text(row, "item_id")),
                ArtifactRefId(_integer(row, "artifact_ref_id")),
                _enum_value(ArtifactRole, row, "role"),
                _integer(row, "position"),
            )
            for row in self._rows("SELECT * FROM item_artifacts ORDER BY item_id, role, position")
        )
        attempts: list[StoredAttempt] = []
        for row in self._rows("SELECT * FROM attempts ORDER BY attempt_id"):
            _expect(row, "brief_artifact_kind", "brief")
            if _optional_integer(row, "result_artifact_ref_id") is not None:
                _expect(row, "result_artifact_kind", "result")
            if _optional_integer(row, "blocker_artifact_ref_id") is not None:
                _expect(row, "blocker_artifact_kind", "blocker")
            attempts.append(
                StoredAttempt(
                    AttemptId(_text(row, "attempt_id")),
                    ItemId(_text(row, "item_id")),
                    _enum_value(OriginKind, row, "origin_kind"),
                    _enum_value(AttemptState, row, "state"),
                    _text(row, "branch"),
                    _text(row, "base_revision"),
                    _text(row, "provenance"),
                    ArtifactRefId(_integer(row, "brief_artifact_ref_id")),
                    None
                    if (result := _optional_integer(row, "result_artifact_ref_id")) is None
                    else ArtifactRefId(result),
                    None
                    if (blocker := _optional_integer(row, "blocker_artifact_ref_id")) is None
                    else ArtifactRefId(blocker),
                    _optional_text(row, "candidate_revision"),
                    _optional_time(row, "candidate_recorded_at"),
                    _integer(row, "accepted_scope_revision"),
                    _text(row, "accepted_scope_digest"),
                    _integer(row, "subject_revision"),
                    _optional_time(row, "origin_created_at"),
                    _optional_time(row, "origin_updated_at"),
                    _time(row, "recorded_at"),
                    _time(row, "updated_at"),
                )
            )
        return LifecycleRecords(self._project(), items, scopes, dependencies, item_artifacts, tuple(attempts))

    def _proposals(self) -> ProposalRecords:
        proposals = tuple(
            StoredProposal(
                ProposalId(_text(row, "proposal_id")),
                _enum_value(OriginKind, row, "origin_kind"),
                _time(row, "created_at"),
                _time(row, "recorded_at"),
                TaskId(_text(row, "source_task_id")),
                _text(row, "user_label"),
                _text(row, "trigger"),
                _text(row, "why_it_matters"),
                _enum_value(ProposalRelation, row, "relation_kind"),
                None if (relation := _optional_text(row, "relation_item_id")) is None else ItemId(relation),
                _text(row, "effect"),
                _text(row, "unlock"),
                _text(row, "urgency_evidence"),
                None
                if (disposition := _optional_text(row, "disposition")) is None
                else ProposalDisposition(disposition),
                None if (target := _optional_text(row, "disposition_target_item_id")) is None else ItemId(target),
                _optional_text(row, "disposition_reason"),
                _integer(row, "subject_revision"),
                _optional_time(row, "origin_disposed_at"),
                _optional_time(row, "disposition_recorded_at"),
            )
            for row in self._rows("SELECT * FROM proposals ORDER BY proposal_id")
        )
        evidence = tuple(
            ProposalEvidence(ProposalId(_text(row, "proposal_id")), _integer(row, "position"), _text(row, "selector"))
            for row in self._rows("SELECT * FROM proposal_evidence ORDER BY proposal_id, position")
        )
        freshness = tuple(
            ProposalFreshness(
                ProposalId(_text(row, "proposal_id")), _integer(row, "position"), _text(row, "assumption")
            )
            for row in self._rows("SELECT * FROM proposal_freshness ORDER BY proposal_id, position")
        )
        return ProposalRecords(proposals, evidence, freshness)

    def _planning(self) -> PlanningRecords:
        impacts: list[StoredPlanningImpact] = []
        for row in self._rows("SELECT * FROM planning_impacts ORDER BY impact_id"):
            if _integer(row, "primary_target_position") != 0:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE, "Planning impact primary target must be position zero."
                )
            impacts.append(
                StoredPlanningImpact(
                    PlanningImpactId(_text(row, "impact_id")),
                    ItemId(_text(row, "source_item_id")),
                    None if (attempt := _optional_text(row, "source_attempt_id")) is None else AttemptId(attempt),
                    _integer(row, "source_scope_revision"),
                    _text(row, "source_scope_digest"),
                    ItemId(_text(row, "primary_target_item_id")),
                    _text(row, "summary"),
                    _text(row, "evidence"),
                    _integer(row, "recorded_project_revision"),
                    _time(row, "recorded_at"),
                )
            )
        obligations = tuple(
            StoredPlanningObligation(
                PlanningImpactId(_text(row, "impact_id")),
                ItemId(_text(row, "target_item_id")),
                _integer(row, "target_position"),
                _integer(row, "observed_scope_revision"),
                _text(row, "observed_scope_digest"),
                _enum_value(PlanningObligationState, row, "status"),
                None
                if (disposition := _optional_text(row, "disposition")) is None
                else PlanningDisposition(disposition),
                _optional_integer(row, "evaluated_scope_revision"),
                _optional_text(row, "evaluated_scope_digest"),
                _optional_integer(row, "resulting_scope_revision"),
                _optional_text(row, "resulting_scope_digest"),
                None
                if (replacement := _optional_text(row, "primary_replacement_item_id")) is None
                else ItemId(replacement),
                _optional_text(row, "outcome_evidence"),
                _optional_text(row, "reason"),
                _optional_integer(row, "resolved_project_revision"),
                _time(row, "recorded_at"),
                _optional_time(row, "resolved_at"),
            )
            for row in self._rows("SELECT * FROM planning_impact_obligations ORDER BY impact_id, target_position")
        )
        replacements = tuple(
            StoredPlanningReplacement(
                PlanningImpactId(_text(row, "impact_id")),
                ItemId(_text(row, "target_item_id")),
                ItemId(_text(row, "replacement_item_id")),
                _integer(row, "position"),
            )
            for row in self._rows(
                "SELECT * FROM planning_impact_replacements ORDER BY impact_id, target_item_id, position"
            )
        )
        return PlanningRecords(tuple(impacts), obligations, replacements)

    def _artifacts(self) -> ArtifactRecords:
        return ArtifactRecords(
            tuple(
                ArtifactReference(
                    ArtifactRefId(_integer(row, "artifact_ref_id")),
                    _text(row, "artifact_key"),
                    _integer(row, "artifact_revision"),
                    _enum_value(ArtifactKind, row, "kind"),
                    _text(row, "relative_path"),
                    _text(row, "content_sha256"),
                    _integer(row, "size_bytes"),
                    _integer(row, "accepted_revision"),
                    _time(row, "created_at"),
                )
                for row in self._rows("SELECT * FROM artifact_refs ORDER BY artifact_ref_id")
            )
        )

    def _authority(self) -> AuthorityRecords:
        coordination_rows = self._rows("SELECT * FROM coordination_lease ORDER BY singleton")
        if len(coordination_rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple coordination leases.")
        coordination = None
        if coordination_rows:
            row = coordination_rows[0]
            coordination = StoredCoordinationLease(
                LeaseId(_text(row, "lease_id")),
                TaskId(_text(row, "task_id")),
                HostId(_text(row, "host_id")),
                _integer(row, "generation"),
                _time(row, "acquired_at"),
                _time(row, "expires_at"),
                _enum_value(CoordinationLeaseState, row, "status"),
            )
        counters = tuple(
            AttemptLeaseCounter(AttemptId(_text(row, "attempt_id")), _integer(row, "generation_high_water"))
            for row in self._rows("SELECT * FROM attempt_lease_counters ORDER BY attempt_id")
        )
        generations = tuple(
            AttemptLeaseGeneration(
                AttemptId(_text(row, "attempt_id")),
                _integer(row, "generation"),
                LeaseId(_text(row, "lease_id")),
                TaskId(_text(row, "task_id")),
                HostId(_text(row, "host_id")),
            )
            for row in self._rows("SELECT * FROM attempt_lease_generations ORDER BY attempt_id, generation")
        )
        leases = tuple(
            StoredAttemptLease(
                AttemptId(_text(row, "attempt_id")),
                _integer(row, "generation"),
                _time(row, "acquired_at"),
                _time(row, "expires_at"),
                _enum_value(AttemptLeaseState, row, "status"),
            )
            for row in self._rows("SELECT * FROM attempt_leases ORDER BY attempt_id")
        )
        return AuthorityRecords(coordination, counters, generations, leases)

    def _resources(self) -> ResourceRecords:
        definitions: list[StoredResourceDefinition] = []
        for row in self._rows("SELECT * FROM resources ORDER BY resource_id"):
            _expect(row, "scope", "portable-definition")
            _expect(row, "allocation_mode", "exclusive-instance")
            definitions.append(
                StoredResourceDefinition(
                    ResourceId(_text(row, "resource_id")),
                    _enum_value(OriginKind, row, "origin_kind"),
                    _text(row, "kind"),
                    _text(row, "description"),
                    _integer(row, "subject_revision"),
                    _optional_time(row, "origin_created_at"),
                    _optional_time(row, "origin_updated_at"),
                    _time(row, "recorded_at"),
                    _time(row, "updated_at"),
                )
            )
        requirements = tuple(
            ItemResourceRequirement(
                ItemId(_text(row, "item_id")),
                ResourceId(_text(row, "resource_id")),
                _integer(row, "position"),
            )
            for row in self._rows("SELECT * FROM item_resources ORDER BY item_id, position")
        )
        instances = tuple(
            StoredResourceInstance(
                ResourceInstanceId(_text(row, "instance_id")),
                ResourceId(_text(row, "resource_id")),
                HostId(_text(row, "host_id")),
                _text(row, "discovery_kind"),
                _text(row, "discovery_fingerprint"),
                _enum_value(ResourceInstanceState, row, "status"),
                _integer(row, "subject_revision"),
                _time(row, "recorded_at"),
                _time(row, "updated_at"),
            )
            for row in self._rows("SELECT * FROM resource_instances ORDER BY instance_id")
        )
        locators = tuple(
            ResourceInstanceLocator(
                ResourceInstanceId(_text(row, "instance_id")),
                HostId(_text(row, "host_id")),
                _text(row, "locator_schema"),
                _canonical_json(row, "locator_json"),
                _integer(row, "observation_generation"),
                _text(row, "observation_digest"),
                _time(row, "observed_at"),
            )
            for row in self._rows("SELECT * FROM resource_instance_locators ORDER BY instance_id")
        )
        counters = tuple(
            StoredReservationCounter(
                ResourceInstanceId(_text(row, "instance_id")), _integer(row, "generation_high_water")
            )
            for row in self._rows("SELECT * FROM resource_reservation_counters ORDER BY instance_id")
        )
        reservations = tuple(
            StoredResourceReservation(
                ReservationId(_text(row, "reservation_id")),
                ResourceInstanceId(_text(row, "instance_id")),
                ResourceId(_text(row, "resource_id")),
                HostId(_text(row, "host_id")),
                _integer(row, "generation"),
                AttemptId(_text(row, "attempt_id")),
                ItemId(_text(row, "item_id")),
                _enum_value(ReservationState, row, "status"),
                _integer(row, "subject_revision"),
                _time(row, "created_at"),
                _optional_time(row, "ended_at"),
            )
            for row in self._rows("SELECT * FROM resource_reservations ORDER BY reservation_id")
        )
        use_leases = [
            StoredResourceUseLease(
                ReservationId(_text(row, "reservation_id")),
                ResourceInstanceId(_text(row, "instance_id")),
                _integer(row, "reservation_generation"),
                AttemptId(_text(row, "attempt_id")),
                HostId(_text(row, "host_id")),
                _integer(row, "instance_subject_revision"),
                _integer(row, "observation_generation"),
                _text(row, "observation_digest"),
                TaskId(_text(row, "task_id")),
                LeaseId(_text(row, "attempt_lease_id")),
                _integer(row, "attempt_lease_generation"),
                LeaseId(_text(row, "lease_id")),
                _integer(row, "generation"),
                _enum_value(UseLeaseGenerationKind, row, "generation_kind"),
                _integer(row, "host_epoch"),
                _time(row, "acquired_at"),
                _time(row, "expires_at"),
                _enum_value(UseLeaseState, row, "status"),
            )
            for row in self._rows("SELECT * FROM resource_use_leases ORDER BY reservation_id, generation")
        ]
        mutation_intents: list[ResourceMutationIntent] = []
        for row in self._rows("SELECT * FROM resource_mutation_intents ORDER BY intent_id"):
            _expect(row, "resource_use_generation_kind", "grant")
            mutation_intents.append(
                ResourceMutationIntent(
                    MutationIntentId(_text(row, "intent_id")),
                    ReservationId(_text(row, "reservation_id")),
                    _integer(row, "reservation_generation"),
                    ResourceInstanceId(_text(row, "instance_id")),
                    AttemptId(_text(row, "attempt_id")),
                    HostId(_text(row, "host_id")),
                    _integer(row, "resource_use_generation"),
                    LeaseId(_text(row, "resource_use_lease_id")),
                    TaskId(_text(row, "task_id")),
                    LeaseId(_text(row, "attempt_lease_id")),
                    _integer(row, "attempt_lease_generation"),
                    _integer(row, "start_instance_subject_revision"),
                    _integer(row, "start_observation_generation"),
                    _text(row, "start_observation_digest"),
                    _text(row, "policy_schema"),
                    _canonical_json(row, "policy_json"),
                    _text(row, "policy_digest"),
                    _enum_value(MutationIntentState, row, "status"),
                    _time(row, "recorded_at"),
                    _optional_time(row, "resolved_at"),
                    _optional_integer(row, "result_observation_generation"),
                    _optional_text(row, "result_observation_digest"),
                    _optional_text(row, "evidence_schema"),
                    _optional_canonical_json(row, "evidence_json"),
                    _optional_text(row, "evidence_digest"),
                    None if (task := _optional_text(row, "disposition_task_id")) is None else TaskId(task),
                    _optional_text(row, "disposition_reason"),
                )
            )
        return ResourceRecords(
            tuple(definitions),
            requirements,
            instances,
            locators,
            counters,
            reservations,
            tuple(use_leases),
            tuple(mutation_intents),
        )

    def _history(self) -> HistoryRecords:
        receipts: list[StoredTransitionReceipt] = []
        for row in self._rows("SELECT * FROM transition_history ORDER BY history_id"):
            artifact_ref = _optional_integer(row, "artifact_ref_id")
            if artifact_ref is not None:
                _expect(row, "artifact_kind", "evidence")
            elif _optional_text(row, "artifact_kind") is not None:
                raise StorageError(StorageErrorCode.INVALID_STATE, "History artifact kind requires a reference.")
            receipts.append(
                StoredTransitionReceipt(
                    HistoryId(_integer(row, "history_id")),
                    _integer(row, "project_revision"),
                    ActionId(_text(row, "action_id")),
                    _enum_value(TransitionHistoryActionKind, row, "action_kind"),
                    HistorySubjectId(_text(row, "subject_id")),
                    None if artifact_ref is None else ArtifactRefId(artifact_ref),
                    _enum_value(TransitionHistoryAuthorizationKind, row, "authorization_kind"),
                    None if (task := _optional_text(row, "actor_task_id")) is None else TaskId(task),
                    None if (host := _optional_text(row, "actor_host_id")) is None else HostId(host),
                    _text(row, "input_schema"),
                    _stored_json(row, "input_json"),
                    _text(row, "outcome_schema"),
                    _stored_json(row, "outcome_json"),
                    _time(row, "committed_at"),
                )
            )
        return HistoryRecords(tuple(receipts))

    def _focus(self) -> StoredFocus:
        rows = self._rows("SELECT * FROM current_focus ORDER BY singleton")
        if len(rows) > 1:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has multiple focus records.")
        if not rows:
            return StoredFocus(None, None, "select", 0)
        row = rows[0]
        return StoredFocus(
            None if (item := _optional_text(row, "item_id")) is None else ItemId(item),
            None if (attempt := _optional_text(row, "attempt_id")) is None else AttemptId(attempt),
            _text(row, "next_action"),
            _integer(row, "subject_revision"),
        )


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json_text(value: CanonicalJson | None) -> str | None:
    return None if value is None else bytes(value).decode("utf-8")


class _StoredStateWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert_initial(self, state: StoredWorkState) -> None:
        project = state.lifecycle.project
        if project.application != APPLICATION or project.schema_version != SCHEMA_VERSION:
            raise StorageError(
                StorageErrorCode.INVALID_STATE, "Stored state does not match the current application schema."
            )
        _validate_current_state(state, StorageErrorCode.INVARIANT_VIOLATION)
        occupied = sum(
            _integer(row, "count")
            for table in (
                "artifact_refs",
                "work_items",
                "item_scope_revisions",
                "item_dependencies",
                "item_artifacts",
                "attempts",
                "planning_impacts",
                "planning_impact_obligations",
                "planning_impact_replacements",
                "proposals",
                "proposal_evidence",
                "proposal_freshness",
                "resources",
                "item_resources",
                "resource_instances",
                "resource_instance_locators",
                "resource_reservation_counters",
                "resource_reservations",
                "coordination_lease",
                "attempt_lease_counters",
                "attempt_lease_generations",
                "attempt_leases",
                "resource_use_leases",
                "resource_mutation_intents",
                "current_focus",
                "transition_history",
            )
            for row in self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
        )
        current_revision = self._connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
        if occupied != 0 or current_revision is None or current_revision[0] != 0:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Initial state requires a new empty database.")
        self._connection.execute("PRAGMA defer_foreign_keys = ON")
        self._artifacts(state.artifacts)
        self._lifecycle(state.lifecycle)
        self._planning(state.planning)
        self._proposals(state.proposals)
        self._resources(state.resources)
        self._authority(state.authority)
        self._focus(state.focus)
        self._history(state.history)
        self._connection.execute(
            """
            UPDATE project_meta
            SET revision = ?, host_epoch = ?, created_at = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (project.revision, project.host_epoch, project.created_at.isoformat(), project.updated_at.isoformat()),
        )

    def replace_current(self, state: StoredWorkState) -> None:
        self._connection.execute("PRAGMA defer_foreign_keys = ON")
        for table in (
            "transition_history",
            "current_focus",
            "resource_mutation_intents",
            "resource_use_leases",
            "attempt_leases",
            "attempt_lease_generations",
            "attempt_lease_counters",
            "coordination_lease",
            "resource_reservations",
            "resource_reservation_counters",
            "resource_instance_locators",
            "resource_instances",
            "item_resources",
            "resources",
            "proposal_evidence",
            "proposal_freshness",
            "proposals",
            "planning_impact_replacements",
            "planning_impact_obligations",
            "planning_impacts",
            "attempts",
            "item_artifacts",
            "item_dependencies",
            "item_scope_revisions",
            "work_items",
            "artifact_refs",
        ):
            self._connection.execute(f"DELETE FROM {table}")
        self._connection.execute("UPDATE project_meta SET revision = 0")
        self.insert_initial(state)

    def _artifacts(self, records: ArtifactRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO artifact_refs (
                artifact_ref_id, artifact_key, artifact_revision, kind, relative_path, content_sha256,
                size_bytes, accepted_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.artifact_ref_id,
                    value.key,
                    value.revision,
                    value.kind.value,
                    value.selector,
                    value.content_sha256,
                    value.size_bytes,
                    value.accepted_revision,
                    value.created_at.isoformat(),
                )
                for value in records.references
            ),
        )

    def _lifecycle(self, records: LifecycleRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO work_items (
                item_id, origin_kind, user_label, state, timing, source, trigger, why_it_matters,
                effect, unlock, outcome_evidence, next_action, notes, scope_revision, scope_digest,
                subject_revision, origin_created_at, origin_updated_at, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.item_id,
                    value.origin.value,
                    value.user_label,
                    value.state.value,
                    None if value.timing is None else value.timing.value,
                    value.source,
                    value.trigger,
                    value.why_it_matters,
                    value.effect,
                    value.unlock,
                    value.outcome_evidence,
                    value.next_action,
                    value.notes,
                    value.scope_revision,
                    value.scope_digest,
                    value.subject_revision,
                    _timestamp(value.origin_created_at),
                    _timestamp(value.origin_updated_at),
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.work_items
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO item_scope_revisions (
                item_id, scope_revision, scope_digest, accepted_project_revision, accepted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.item_id,
                    value.revision,
                    value.digest,
                    value.accepted_project_revision,
                    value.accepted_at.isoformat(),
                )
                for value in records.scope_revisions
            ),
        )
        self._connection.executemany(
            "INSERT INTO item_dependencies (item_id, dependency_id, position) VALUES (?, ?, ?)",
            tuple((value.item_id, value.dependency_id, value.position) for value in records.dependencies),
        )
        self._connection.executemany(
            "INSERT INTO item_artifacts (item_id, artifact_ref_id, role, position) VALUES (?, ?, ?, ?)",
            tuple(
                (value.item_id, value.artifact_ref_id, value.role.value, value.position)
                for value in records.item_artifacts
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO attempts (
                attempt_id, item_id, origin_kind, state, branch, base_revision, provenance,
                brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                blocker_artifact_ref_id, blocker_artifact_kind, candidate_revision, candidate_recorded_at,
                accepted_scope_revision, accepted_scope_digest, subject_revision, origin_created_at,
                origin_updated_at, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'brief', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.attempt_id,
                    value.item_id,
                    value.origin.value,
                    value.state.value,
                    value.branch,
                    value.base_revision,
                    value.provenance,
                    value.brief_artifact_ref_id,
                    value.result_artifact_ref_id,
                    None if value.result_artifact_ref_id is None else "result",
                    value.blocker_artifact_ref_id,
                    None if value.blocker_artifact_ref_id is None else "blocker",
                    value.candidate_revision,
                    _timestamp(value.candidate_recorded_at),
                    value.accepted_scope_revision,
                    value.accepted_scope_digest,
                    value.subject_revision,
                    _timestamp(value.origin_created_at),
                    _timestamp(value.origin_updated_at),
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.attempts
            ),
        )

    def _planning(self, records: PlanningRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO planning_impacts (
                impact_id, source_item_id, source_attempt_id, source_scope_revision, source_scope_digest,
                primary_target_item_id, primary_target_position, summary, evidence,
                recorded_project_revision, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.impact_id,
                    value.source_item_id,
                    value.source_attempt_id,
                    value.source_scope_revision,
                    value.source_scope_digest,
                    value.primary_target_item_id,
                    value.summary,
                    value.evidence,
                    value.recorded_project_revision,
                    value.recorded_at.isoformat(),
                )
                for value in records.impacts
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO planning_impact_obligations (
                impact_id, target_item_id, target_position, observed_scope_revision, observed_scope_digest,
                status, disposition, evaluated_scope_revision, evaluated_scope_digest,
                resulting_scope_revision, resulting_scope_digest, primary_replacement_item_id,
                primary_replacement_position, outcome_evidence, reason, resolved_project_revision,
                recorded_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.impact_id,
                    value.target_item_id,
                    value.position,
                    value.observed_scope_revision,
                    value.observed_scope_digest,
                    value.state.value,
                    None if value.disposition is None else value.disposition.value,
                    value.evaluated_scope_revision,
                    value.evaluated_scope_digest,
                    value.resulting_scope_revision,
                    value.resulting_scope_digest,
                    value.primary_replacement_item_id,
                    0 if value.primary_replacement_item_id is not None else None,
                    value.outcome_evidence,
                    value.reason,
                    value.resolved_project_revision,
                    value.recorded_at.isoformat(),
                    _timestamp(value.resolved_at),
                )
                for value in records.obligations
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO planning_impact_replacements (
                impact_id, target_item_id, disposition, replacement_item_id, position
            ) VALUES (?, ?, 'superseded', ?, ?)
            """,
            tuple(
                (value.impact_id, value.target_item_id, value.replacement_item_id, value.position)
                for value in records.replacements
            ),
        )

    def _proposals(self, records: ProposalRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO proposals (
                proposal_id, origin_kind, created_at, recorded_at, source_task_id, user_label,
                trigger, why_it_matters, relation_kind, relation_item_id, effect, unlock,
                urgency_evidence, disposition, disposition_target_item_id, disposition_reason,
                subject_revision, origin_disposed_at, disposition_recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.proposal_id,
                    value.origin.value,
                    value.created_at.isoformat(),
                    value.recorded_at.isoformat(),
                    value.source_task_id,
                    value.user_label,
                    value.trigger,
                    value.why_it_matters,
                    value.relation.value,
                    value.relation_item_id,
                    value.effect,
                    value.unlock,
                    value.urgency_evidence,
                    None if value.disposition is None else value.disposition.value,
                    value.disposition_target_item_id,
                    value.disposition_reason,
                    value.subject_revision,
                    _timestamp(value.origin_disposed_at),
                    _timestamp(value.disposition_recorded_at),
                )
                for value in records.proposals
            ),
        )
        self._connection.executemany(
            "INSERT INTO proposal_evidence (proposal_id, position, selector) VALUES (?, ?, ?)",
            tuple((value.proposal_id, value.position, value.selector) for value in records.evidence),
        )
        self._connection.executemany(
            "INSERT INTO proposal_freshness (proposal_id, position, assumption) VALUES (?, ?, ?)",
            tuple((value.proposal_id, value.position, value.assumption) for value in records.freshness),
        )

    def _resources(self, records: ResourceRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO resources (
                resource_id, origin_kind, kind, scope, allocation_mode, description, subject_revision,
                origin_created_at, origin_updated_at, recorded_at, updated_at
            ) VALUES (?, ?, ?, 'portable-definition', 'exclusive-instance', ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.resource_id,
                    value.origin.value,
                    value.kind,
                    value.description,
                    value.subject_revision,
                    _timestamp(value.origin_created_at),
                    _timestamp(value.origin_updated_at),
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.definitions
            ),
        )
        self._connection.executemany(
            "INSERT INTO item_resources (item_id, resource_id, position) VALUES (?, ?, ?)",
            tuple((value.item_id, value.resource_id, value.position) for value in records.requirements),
        )
        self._connection.executemany(
            """
            INSERT INTO resource_instances (
                instance_id, resource_id, host_id, discovery_kind, discovery_fingerprint,
                status, subject_revision, recorded_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.instance_id,
                    value.resource_id,
                    value.host_id,
                    value.discovery_kind,
                    value.discovery_fingerprint,
                    value.state.value,
                    value.subject_revision,
                    value.recorded_at.isoformat(),
                    value.updated_at.isoformat(),
                )
                for value in records.instances
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO resource_instance_locators (
                instance_id, host_id, locator_schema, locator_json, observation_generation,
                observation_digest, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.instance_id,
                    value.host_id,
                    value.locator_schema,
                    _json_text(value.locator),
                    value.observation_generation,
                    value.observation_digest,
                    value.observed_at.isoformat(),
                )
                for value in records.locators
            ),
        )
        self._connection.executemany(
            "INSERT INTO resource_reservation_counters (instance_id, generation_high_water) VALUES (?, ?)",
            tuple((value.instance_id, value.generation_high_water) for value in records.reservation_counters),
        )
        self._connection.executemany(
            """
            INSERT INTO resource_reservations (
                reservation_id, instance_id, resource_id, host_id, generation, attempt_id,
                item_id, status, subject_revision, created_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.reservation_id,
                    value.instance_id,
                    value.resource_id,
                    value.host_id,
                    value.acquisition_generation,
                    value.attempt_id,
                    value.item_id,
                    value.state.value,
                    value.subject_revision,
                    value.created_at.isoformat(),
                    _timestamp(value.ended_at),
                )
                for value in records.reservations
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO resource_use_leases (
                reservation_id, instance_id, reservation_generation, attempt_id, host_id,
                instance_subject_revision, observation_generation, observation_digest, task_id,
                attempt_lease_id, attempt_lease_generation, lease_id, generation, generation_kind,
                host_epoch, acquired_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.reservation_id,
                    value.instance_id,
                    value.reservation_generation,
                    value.attempt_id,
                    value.host_id,
                    value.instance_subject_revision,
                    value.observation_generation,
                    value.observation_digest,
                    value.task_id,
                    value.attempt_lease_id,
                    value.attempt_lease_generation,
                    value.lease_id,
                    value.generation,
                    value.generation_kind.value,
                    value.host_epoch,
                    value.acquired_at.isoformat(),
                    value.expires_at.isoformat(),
                    value.state.value,
                )
                for value in records.use_leases
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO resource_mutation_intents (
                intent_id, reservation_id, reservation_generation, instance_id, attempt_id, host_id,
                resource_use_generation, resource_use_lease_id, resource_use_generation_kind, task_id,
                attempt_lease_id, attempt_lease_generation, start_instance_subject_revision,
                start_observation_generation, start_observation_digest, policy_schema, policy_json,
                policy_digest, status, recorded_at, resolved_at, result_observation_generation,
                result_observation_digest, evidence_schema, evidence_json, evidence_digest,
                disposition_task_id, disposition_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'grant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.intent_id,
                    value.reservation_id,
                    value.reservation_generation,
                    value.instance_id,
                    value.attempt_id,
                    value.host_id,
                    value.resource_use_generation,
                    value.resource_use_lease_id,
                    value.task_id,
                    value.attempt_lease_id,
                    value.attempt_lease_generation,
                    value.start_instance_subject_revision,
                    value.start_observation_generation,
                    value.start_observation_digest,
                    value.policy_schema,
                    _json_text(value.policy),
                    value.policy_digest,
                    value.state.value,
                    value.recorded_at.isoformat(),
                    _timestamp(value.resolved_at),
                    value.result_observation_generation,
                    value.result_observation_digest,
                    value.evidence_schema,
                    _json_text(value.evidence),
                    value.evidence_digest,
                    value.disposition_task_id,
                    value.disposition_reason,
                )
                for value in records.mutation_intents
            ),
        )

    def _authority(self, records: AuthorityRecords) -> None:
        if records.coordination is not None:
            value = records.coordination
            self._connection.execute(
                """
                INSERT INTO coordination_lease (
                    singleton, lease_id, task_id, host_id, generation, acquired_at, expires_at, status
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.lease_id,
                    value.task_id,
                    value.host_id,
                    value.generation,
                    value.acquired_at.isoformat(),
                    value.expires_at.isoformat(),
                    value.state.value,
                ),
            )
        self._connection.executemany(
            "INSERT INTO attempt_lease_counters (attempt_id, generation_high_water) VALUES (?, ?)",
            tuple((value.attempt_id, value.generation_high_water) for value in records.attempt_counters),
        )
        self._connection.executemany(
            """
            INSERT INTO attempt_lease_generations (attempt_id, generation, lease_id, task_id, host_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (value.attempt_id, value.generation, value.lease_id, value.task_id, value.host_id)
                for value in records.attempt_generations
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO attempt_leases (attempt_id, generation, acquired_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.attempt_id,
                    value.generation,
                    value.acquired_at.isoformat(),
                    value.expires_at.isoformat(),
                    value.state.value,
                )
                for value in records.attempt_leases
            ),
        )

    def _focus(self, focus: StoredFocus) -> None:
        self._connection.execute(
            """
            INSERT INTO current_focus (singleton, item_id, attempt_id, next_action, subject_revision)
            VALUES (1, ?, ?, ?, ?)
            """,
            (focus.item_id, focus.attempt_id, focus.next_action, focus.subject_revision),
        )

    def _history(self, records: HistoryRecords) -> None:
        self._connection.executemany(
            """
            INSERT INTO transition_history (
                history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
                input_json, outcome_schema, outcome_json, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    value.history_id,
                    value.project_revision,
                    value.action_id,
                    value.action_kind.value,
                    value.subject_id,
                    value.artifact_ref_id,
                    None if value.artifact_ref_id is None else "evidence",
                    value.authorization.value,
                    value.actor_task_id,
                    value.actor_host_id,
                    value.input_schema,
                    _json_text(value.input_payload),
                    value.outcome_schema,
                    _json_text(value.outcome_payload),
                    value.committed_at.isoformat(),
                )
                for value in records.receipts
            ),
        )


class _SQLiteWorkTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def snapshot(self) -> StoredWorkState:
        return _StoredStateReader(self._connection).read()

    def commit(self, mutation: TransitionReceiptMutation) -> TransitionReceipt:
        current = _StoredStateReader(self._connection).read()
        if current != mutation.before:
            raise StorageError(
                StorageErrorCode.STALE_WRITE,
                "The stored work state changed; rediscover the mutation before retrying.",
            )
        try:
            expected = expected_stored_state(mutation)
        except MutationContractError as error:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, str(error)) from error
        if mutation.after != expected:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "The supplied stored state does not match the accepted mutation's exact relational delta.",
            )
        _StoredStateWriter(self._connection).replace_current(expected)
        if _StoredStateReader(self._connection).read() != expected:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "The stored mutation did not round-trip exactly.")
        match mutation:
            case (
                TransitionMutation()
                | ProposalCreationMutation()
                | CoordinationAuthorityMutation()
                | AttemptAuthorityMutation()
            ):
                return mutation.receipt.transition
            case _ as unreachable:
                assert_never(unreachable)


class SQLiteWorkStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def snapshot(self) -> StoredWorkState:
        connection = open_database(self._path, OpenMode.READ_ONLY)
        try:
            with read_operation(connection):
                return _StoredStateReader(connection).read()
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Generator[_SQLiteWorkTransaction]:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                yield _SQLiteWorkTransaction(connection)
        finally:
            connection.close()

    def initialize_state(self, state: StoredWorkState) -> None:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                _StoredStateWriter(connection).insert_initial(state)
        finally:
            connection.close()

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
        *,
        item_id: ItemId | None = None,
        role: ArtifactRole | None = None,
    ) -> ArtifactReference:
        connection = open_database(self._path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                before = _StoredStateReader(connection).read()
                verify_reference(work_root, published)
                if (item_id is None) != (role is None):
                    raise StorageError(
                        StorageErrorCode.INVARIANT_VIOLATION,
                        "An artifact relationship requires both an item and role.",
                    )
                existing = next(
                    (
                        value
                        for value in before.artifacts.references
                        if (value.kind, value.key, value.revision)
                        == (published.kind, published.key, published.revision)
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.selector,
                        existing.content_sha256,
                        existing.size_bytes,
                    ) != (published.selector, published.content_sha256, published.size_bytes):
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "An accepted artifact identity already names different bytes.",
                        )
                    reference = existing
                else:
                    reference = ArtifactReference(
                        ArtifactRefId(
                            1 + max((int(value.artifact_ref_id) for value in before.artifacts.references), default=0)
                        ),
                        published.key,
                        published.revision,
                        published.kind,
                        published.selector,
                        published.content_sha256,
                        published.size_bytes,
                        before.lifecycle.project.revision + 1,
                        accepted_at,
                    )
                relationship_exists = item_id is not None and any(
                    value.item_id == item_id
                    and value.artifact_ref_id == reference.artifact_ref_id
                    and value.role == role
                    for value in before.lifecycle.item_artifacts
                )
                if existing is not None and (item_id is None or relationship_exists):
                    return existing
                revision = before.lifecycle.project.revision + 1
                lifecycle = before.lifecycle
                if item_id is not None and role is not None:
                    item_index = next(
                        (index for index, value in enumerate(lifecycle.work_items) if value.item_id == item_id),
                        None,
                    )
                    if item_index is None or published.kind.value != role.value:
                        raise StorageError(
                            StorageErrorCode.INVARIANT_VIOLATION,
                            "Artifact relationship does not match a current item and compatible role.",
                        )
                    items = list(lifecycle.work_items)
                    items[item_index] = replace(
                        items[item_index],
                        subject_revision=revision,
                        updated_at=accepted_at,
                    )
                    position = sum(
                        1 for value in lifecycle.item_artifacts if value.item_id == item_id and value.role == role
                    )
                    item_artifact = ItemArtifactLink(item_id, reference.artifact_ref_id, role, position)
                    item_artifact_order = (str(item_artifact.item_id), item_artifact.role.value, item_artifact.position)
                    insertion_index = len(lifecycle.item_artifacts)
                    for index, existing_link in enumerate(lifecycle.item_artifacts):
                        existing_order = (str(existing_link.item_id), existing_link.role.value, existing_link.position)
                        if item_artifact_order < existing_order:
                            insertion_index = index
                            break
                    item_artifacts = (
                        *lifecycle.item_artifacts[:insertion_index],
                        item_artifact,
                        *lifecycle.item_artifacts[insertion_index:],
                    )
                    lifecycle = replace(
                        lifecycle,
                        work_items=tuple(items),
                        item_artifacts=item_artifacts,
                    )
                after = replace(
                    before,
                    lifecycle=replace(
                        lifecycle,
                        project=replace(
                            lifecycle.project,
                            revision=revision,
                            updated_at=accepted_at,
                        ),
                    ),
                    artifacts=replace(
                        before.artifacts,
                        references=(
                            before.artifacts.references
                            if existing is not None
                            else (*before.artifacts.references, reference)
                        ),
                    ),
                )
                _StoredStateWriter(connection).replace_current(after)
                if _StoredStateReader(connection).read() != after:
                    raise StorageError(
                        StorageErrorCode.INVARIANT_VIOLATION,
                        "The accepted artifact did not round-trip exactly.",
                    )
                return reference
        finally:
            connection.close()
