import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, assert_never, cast
from uuid import uuid4

import msgspec

from charlie_pinboard import __version__
from charlie_pinboard.adapters.files.artifacts import ArtifactRepository
from charlie_pinboard.adapters.files.errors import ArtifactError, FileIOError, FileIOErrorCode, RootError, RootErrorCode
from charlie_pinboard.adapters.files.file_io import resolve_durable_roots
from charlie_pinboard.adapters.files.models import AffectedViews
from charlie_pinboard.adapters.files.root import resolve_shared_repository_root, resolve_source_checkout_root
from charlie_pinboard.adapters.files.views import rebuild as rebuild_views
from charlie_pinboard.adapters.files.views import refresh as refresh_views
from charlie_pinboard.adapters.sqlite.errors import StorageError
from charlie_pinboard.adapters.sqlite.registration import initialize_work_state
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import discover_actions
from charlie_pinboard.application.artifact_publication import publish_accepted_artifact, validate_transition_work_brief
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.dispatch import prepare_dispatch
from charlie_pinboard.application.errors import (
    ActionQueryError,
    DispatchError,
    DispatchErrorCode,
    QueryError,
)
from charlie_pinboard.application.queries import (
    item_status,
    overview_from_state,
    preview_parallel,
)
from charlie_pinboard.application.query_models import (
    OverviewItem,
    ParallelItem,
    ParallelPreview,
    WorkOverview,
)
from charlie_pinboard.application.service import (
    change_attempt_authority,
    change_coordination_authority,
    create_proposal,
    execute,
)
from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    StoredAttempt,
    StoredCoordinationLease,
    StoredWorkState,
)
from charlie_pinboard.application.validation import Diagnostic, Severity, ValidationReport, validate_work_state
from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.authority_models import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    AttemptAuthorityOperation,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    TransferAttemptAuthority,
)
from charlie_pinboard.domain.decisions import bind_transition
from charlie_pinboard.domain.errors import DecisionFailure
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HostId,
    ItemId,
    LeaseId,
    LedgerId,
    ProposalId,
    SubjectId,
    TaskId,
)
from charlie_pinboard.domain.proposal_models import (
    CreateProposalOperation,
    ProposalIntake,
)
from charlie_pinboard.interfaces.brief_source_models import (
    BriefSourceBatch,
    BriefSourcePlan,
    BriefSourceSegment,
    PlannedBriefSource,
)
from charlie_pinboard.interfaces.brief_sources import (
    decode_brief_source_manifest,
    plan_brief_sources,
    render_brief_source_batch,
)
from charlie_pinboard.interfaces.cli_commands import (
    ActionsCommand,
    AttemptAcquireCommand,
    AttemptReleaseCommand,
    AttemptRenewCommand,
    AttemptRevokeCommand,
    AttemptStatusCommand,
    AttemptTransitionCommand,
    BriefPublishCommand,
    BriefSourcesEmitCommand,
    BriefSourcesPlanCommand,
    CliCommand,
    CliInvocation,
    CloseCommand,
    CoordinatedAttemptAcquireCommand,
    CoordinationAcquireCommand,
    CoordinationApplyCommand,
    CoordinationDispatchCommand,
    CoordinationReleaseCommand,
    CoordinationRenewCommand,
    CoordinationReviewedDispatchCommand,
    CoordinationRevokeCommand,
    CoordinationStatusCommand,
    CoordinationTransitionCommand,
    CoordinatorDispatchCommand,
    CoordinatorReviewedDispatchCommand,
    CoordinatorTransitionCommand,
    DispatchCommand,
    InitializeCommand,
    InputContractCommand,
    ItemStatusCommand,
    LeasedActionsCommand,
    OverviewCommand,
    ParallelPreviewCommand,
    ProposalCommand,
    RebuildViewsCommand,
    ResolvedRoots,
    RootCommand,
    RootSelection,
    StableActionId,
    StableAttemptId,
    StableHostId,
    StableLeaseId,
    StableTaskId,
    StatusCommand,
    TransitionCommand,
    ValidateCommand,
)
from charlie_pinboard.interfaces.cli_models import (
    ActionsView,
    ActionView,
    BlockerActionDescriptorView,
    BriefPublicationView,
    BriefSourceBatchView,
    BriefSourcePlanView,
    BriefSourceSegmentView,
    BriefSourceView,
    CloseView,
    CoordinatedTransitionView,
    CoordinatorView,
    DependencyReasonView,
    DiagnosticView,
    InputContractView,
    OverviewItemView,
    OverviewView,
    ParallelItemView,
    ParallelPreviewView,
    ParallelReasonView,
    ReviewFlagView,
    RootView,
    StatusView,
    ValidationView,
)
from charlie_pinboard.interfaces.dispatch_brief import prepare_dispatch_from_artifact, read_dispatch_environment
from charlie_pinboard.interfaces.errors import (
    BriefSourceError,
    BriefSourceErrorCode,
    CommandError,
    CommandErrorCode,
    ProposalError,
    ProposalErrorCode,
    TransitionInputError,
    TransitionInputErrorCode,
    WorkBriefError,
    WorkBriefErrorCode,
)
from charlie_pinboard.interfaces.proposals import parse_proposal
from charlie_pinboard.interfaces.transition_input import (
    INPUT_CONTRACT_ACTION_KINDS,
    encoded_transition_input_schema,
    parse_transition_input,
)
from charlie_pinboard.interfaces.work_briefs import (
    build_attempt_brief_views,
    canonical_work_brief_bytes,
    decode_work_brief,
    decode_work_brief_identity,
)


def _write_json[T](value: T) -> None:
    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")


def _overview_item_view(item: OverviewItem) -> OverviewItemView:
    return OverviewItemView(
        item.item_id,
        item.label,
        item.state.value,
        item.position,
        item.eligible,
        item.timing,
        item.depends_on,
        tuple(DependencyReasonView(value.item_id, value.reason) for value in item.dependency_reasons),
        tuple(ReviewFlagView(value.kind.value, value.related_item, value.reason) for value in item.review_flags),
        item.attempt_id,
        item.next_action,
        item.notes,
    )


def _overview_view(overview: WorkOverview) -> OverviewView:
    return OverviewView(
        overview.schema,
        overview.authority,
        overview.revision,
        overview.focus_item,
        overview.focus_attempt,
        overview.active_attempts,
        tuple(_overview_item_view(item) for item in overview.items),
        overview.immediate_options,
    )


def _blocker_descriptor_view(
    descriptor: decision_models.BlockerActionDescriptor | None,
) -> BlockerActionDescriptorView | None:
    if descriptor is None:
        return None
    return BlockerActionDescriptorView(
        descriptor.effect.value,
        descriptor.required_role.value,
        descriptor.subject_kind.value,
        descriptor.lifecycle_precondition.value,
    )


def _input_contract_view(kind: decision_models.ActionKind) -> InputContractView:
    descriptor = decision_models.blocker_action_descriptor(kind)
    try:
        payload_schema = msgspec.Raw(encoded_transition_input_schema(kind.value))
    except TransitionInputError as error:
        if descriptor is None or error.code != TransitionInputErrorCode.ACTION_NOT_MUTATING:
            raise
        payload_schema = None
    return InputContractView(kind.value, _blocker_descriptor_view(descriptor), payload_schema)


def _brief_source_segment_view(segment: BriefSourceSegment) -> BriefSourceSegmentView:
    return BriefSourceSegmentView(
        segment.authority_id,
        segment.selector,
        segment.index,
        segment.start_line,
        segment.end_line,
        segment.content_byte_count,
        segment.content_sha256,
    )


def _brief_source_view(source: PlannedBriefSource) -> BriefSourceView:
    return BriefSourceView(
        source.authority_id,
        source.selector,
        source.families,
        source.selected_sha256,
        source.selected_byte_count,
        source.start_line,
        source.end_line,
        source.whole_file,
        tuple(_brief_source_segment_view(segment) for segment in source.segments),
    )


def _brief_source_batch_view(batch: BriefSourceBatch) -> BriefSourceBatchView:
    return BriefSourceBatchView(
        batch.index,
        batch.content_byte_count,
        batch.estimated_rendered_byte_count,
        tuple(_brief_source_segment_view(segment) for segment in batch.segments),
    )


def _brief_source_plan_view(plan: BriefSourcePlan) -> BriefSourcePlanView:
    return BriefSourcePlanView(
        plan.schema,
        plan.manifest_sha256,
        plan.max_batch_bytes,
        tuple(_brief_source_view(source) for source in plan.sources),
        tuple(_brief_source_batch_view(batch) for batch in plan.batches),
    )


def _action_view(action: decision_models.Action, *, include_input_contract: bool = False) -> ActionView:
    input_contract: InputContractView | None = None
    if include_input_contract:
        try:
            input_contract = _input_contract_view(action.kind)
        except TransitionInputError as error:
            if error.code != TransitionInputErrorCode.ACTION_NOT_MUTATING:
                raise
    return ActionView(
        action_id=action.action_id,
        kind=action.kind.value,
        subject=action.subject,
        label=action.label,
        expected_revision=action.expected_revision,
        coordinator_generation=action.coordinator_generation,
        subject_revision=action.subject_revision or "",
        authorization=action.authorization.value,
        lease_id=action.lease_id or "",
        semantics=_blocker_descriptor_view(decision_models.blocker_action_descriptor(action.kind)),
        input_contract=input_contract,
    )


def _parallel_item_view(item: ParallelItem) -> ParallelItemView:
    return ParallelItemView(
        item.item_id,
        item.label,
        item.state.value,
        item.attempt_id,
        item.outcome.value,
        tuple(ParallelReasonView(reason.code.value, reason.message) for reason in item.reasons),
    )


def _parallel_preview_view(preview: ParallelPreview) -> ParallelPreviewView:
    return ParallelPreviewView(
        preview.schema,
        preview.revision,
        preview.selection.value,
        preview.safe,
        tuple(_parallel_item_view(item) for item in preview.launchable),
        tuple(_parallel_item_view(item) for item in preview.excluded),
    )


type RawCliValue = str | int | bool | Path | list[str] | None
type RawCliValues = dict[str, RawCliValue]
type CliCommandType = type[CliCommand]
type CliDecoder = Callable[[RawCliValues], CliCommand]


class _RawCliArguments(argparse.Namespace):
    decoder: CliDecoder | None
    selected_parser: argparse.ArgumentParser | None

    def __init__(self) -> None:
        super().__init__()
        self.decoder = None
        self.selected_parser = None


class _BriefSourcesArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    max_batch_bytes: int
    json: bool
    emit_batch: int | None

    def __post_init__(self) -> None:
        if self.json == (self.emit_batch is not None):
            raise ValueError("exactly one of --json or --emit-batch is required")


class _ActionsArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    lease_id: StableLeaseId | None
    generation: int | None
    action_id: StableActionId | None
    json: bool

    def __post_init__(self) -> None:
        if (self.lease_id is None) != (self.generation is None):
            raise ValueError("--lease-id and --generation must be supplied together")


class _TransitionArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    payload: Path
    subject_revision: str | None
    lease_id: StableLeaseId | None
    authorization: Literal["coordinator", "coordination", "attempt"]

    def __post_init__(self) -> None:
        requires_lease = self.authorization != "coordinator"
        if requires_lease != (self.lease_id is not None):
            raise ValueError(f"--lease-id must be supplied exactly for {self.authorization} authorization")


class _DispatchArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    checkpoint: str
    environment: Path
    lease_id: StableLeaseId | None
    prompt: Path | None
    brief_review: Path | None
    review_id: str | None

    def __post_init__(self) -> None:
        if self.brief_review is None and self.review_id is not None:
            raise ValueError("--review-id is only valid with --brief-review")


class _AttemptAcquireArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    coordination_lease_id: StableLeaseId | None
    coordination_generation: int | None
    json: bool

    def __post_init__(self) -> None:
        if (self.coordination_lease_id is None) != (self.coordination_generation is None):
            raise ValueError("--coordination-lease-id and --coordination-generation must be supplied together")


def _decode_brief_sources(values: RawCliValues) -> BriefSourcesPlanCommand | BriefSourcesEmitCommand:
    arguments = msgspec.convert(values, type=_BriefSourcesArguments, strict=True)
    if arguments.emit_batch is None:
        return BriefSourcesPlanCommand(file=arguments.file, max_batch_bytes=arguments.max_batch_bytes)
    return BriefSourcesEmitCommand(
        file=arguments.file,
        emit_batch=arguments.emit_batch,
        max_batch_bytes=arguments.max_batch_bytes,
    )


def _decode_actions(values: RawCliValues) -> ActionsCommand | LeasedActionsCommand:
    arguments = msgspec.convert(values, type=_ActionsArguments, strict=True)
    if arguments.lease_id is None or arguments.generation is None:
        return ActionsCommand(role=arguments.role, action_id=arguments.action_id, json=arguments.json)
    return LeasedActionsCommand(
        role=arguments.role,
        lease_id=arguments.lease_id,
        generation=arguments.generation,
        action_id=arguments.action_id,
        json=arguments.json,
    )


def _decode_transition(values: RawCliValues) -> TransitionCommand:
    arguments = msgspec.convert(values, type=_TransitionArguments, strict=True)
    match arguments.authorization:
        case "coordinator":
            return CoordinatorTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                subject_revision=arguments.subject_revision,
            )
        case "coordination":
            lease_id = cast("StableLeaseId", arguments.lease_id)
            return CoordinationTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=lease_id,
                subject_revision=arguments.subject_revision,
            )
        case "attempt":
            lease_id = cast("StableLeaseId", arguments.lease_id)
            return AttemptTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=lease_id,
                subject_revision=arguments.subject_revision,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _decode_dispatch(values: RawCliValues) -> DispatchCommand:
    arguments = msgspec.convert(values, type=_DispatchArguments, strict=True)
    if arguments.lease_id is None:
        if arguments.brief_review is None:
            return CoordinatorDispatchCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                checkpoint=arguments.checkpoint,
                environment=arguments.environment,
                prompt=arguments.prompt,
            )
        return CoordinatorReviewedDispatchCommand(
            action_id=arguments.action_id,
            expected_revision=arguments.expected_revision,
            generation=arguments.generation,
            checkpoint=arguments.checkpoint,
            environment=arguments.environment,
            brief_review=arguments.brief_review,
            prompt=arguments.prompt,
            review_id=arguments.review_id,
        )
    if arguments.brief_review is None:
        return CoordinationDispatchCommand(
            action_id=arguments.action_id,
            expected_revision=arguments.expected_revision,
            generation=arguments.generation,
            lease_id=arguments.lease_id,
            checkpoint=arguments.checkpoint,
            environment=arguments.environment,
            prompt=arguments.prompt,
        )
    return CoordinationReviewedDispatchCommand(
        action_id=arguments.action_id,
        expected_revision=arguments.expected_revision,
        generation=arguments.generation,
        lease_id=arguments.lease_id,
        checkpoint=arguments.checkpoint,
        environment=arguments.environment,
        brief_review=arguments.brief_review,
        prompt=arguments.prompt,
        review_id=arguments.review_id,
    )


def _decode_attempt_acquire(values: RawCliValues) -> AttemptAcquireCommand | CoordinatedAttemptAcquireCommand:
    arguments = msgspec.convert(values, type=_AttemptAcquireArguments, strict=True)
    if arguments.coordination_lease_id is None or arguments.coordination_generation is None:
        return AttemptAcquireCommand(
            attempt_id=arguments.attempt_id,
            task_id=arguments.task_id,
            host_id=arguments.host_id,
            ttl_seconds=arguments.ttl_seconds,
            json=arguments.json,
        )
    return CoordinatedAttemptAcquireCommand(
        attempt_id=arguments.attempt_id,
        task_id=arguments.task_id,
        host_id=arguments.host_id,
        ttl_seconds=arguments.ttl_seconds,
        coordination_lease_id=arguments.coordination_lease_id,
        coordination_generation=arguments.coordination_generation,
        json=arguments.json,
    )


def _select(parser: argparse.ArgumentParser, command_type: CliCommandType) -> None:
    def decode(values: RawCliValues) -> CliCommand:
        return msgspec.convert(values, type=command_type, strict=True)

    parser.set_defaults(decoder=decode, selected_parser=parser)


def _select_decoder(parser: argparse.ArgumentParser, decoder: CliDecoder) -> None:
    parser.set_defaults(decoder=decoder, selected_parser=parser)


def _add_coordination_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    coordination = commands.add_parser("coordination", help="Borrow or manage temporary graph-wide authority.")
    operations = coordination.add_subparsers(required=True)
    apply = operations.add_parser("apply", help="Borrow coordination for one exact transition and release it.")
    apply.add_argument("--task-id", required=True)
    apply.add_argument("--host-id", required=True)
    apply.add_argument("--action-id", required=True)
    apply.add_argument("--payload", required=True, type=Path)
    apply.add_argument("--ttl-seconds", type=int, default=60)
    apply.add_argument("--json", action="store_true")
    _select(apply, CoordinationApplyCommand)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    _select(acquire, CoordinationAcquireCommand)
    renew = operations.add_parser("renew")
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select(renew, CoordinationRenewCommand)
    release = operations.add_parser("release")
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select(release, CoordinationReleaseCommand)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--json", action="store_true")
    _select(revoke, CoordinationRevokeCommand)
    status = operations.add_parser("status")
    status.add_argument("--json", action="store_true")
    _select(status, CoordinationStatusCommand)


def _add_attempt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    attempt = commands.add_parser("attempt", help="Manage a renewable attempt ownership claim.")
    operations = attempt.add_subparsers(required=True)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--attempt-id", required=True)
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--coordination-lease-id")
    acquire.add_argument("--coordination-generation", type=int)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    _select_decoder(acquire, _decode_attempt_acquire)
    renew = operations.add_parser("renew")
    renew.add_argument("--attempt-id", required=True)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select(renew, AttemptRenewCommand)
    release = operations.add_parser("release")
    release.add_argument("--attempt-id", required=True)
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select(release, AttemptReleaseCommand)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--attempt-id", required=True)
    revoke.add_argument("--lease-id", required=True)
    revoke.add_argument("--generation", required=True, type=int)
    revoke.add_argument("--coordination-lease-id", required=True)
    revoke.add_argument("--coordination-generation", required=True, type=int)
    revoke.add_argument("--json", action="store_true")
    _select(revoke, AttemptRevokeCommand)
    status = operations.add_parser("status")
    status.add_argument("--attempt-id", required=True)
    status.add_argument("--json", action="store_true")
    _select(status, AttemptStatusCommand)


def _add_item_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    item = commands.add_parser("item", help="Inspect one exact live or terminal item.")
    operations = item.add_subparsers(required=True)
    status = operations.add_parser("status", help="Show authoritative status for one exact item.")
    status.add_argument("--item-id", required=True)
    status.add_argument("--json", action="store_true")
    _select(status, ItemStatusCommand)


def _add_parallel_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parallel = commands.add_parser("parallel", help="Preview structurally independent work without launching it.")
    operations = parallel.add_subparsers(required=True)
    preview = operations.add_parser("preview")
    preview.add_argument("--item", action="append", default=[])
    preview.add_argument("--json", action="store_true")
    _select(preview, ParallelPreviewCommand)


def _add_chat_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    overview = commands.add_parser("overview", help="Show one coherent live-work snapshot.")
    overview.add_argument("--json", action="store_true")
    _select(overview, OverviewCommand)
    close = commands.add_parser("close", help="Record a terminal decision for non-active work.")
    close.add_argument("item_id")
    close.add_argument("--outcome", choices=tuple(outcome.value for outcome in work_models.CloseOutcome), required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--task-id", required=True)
    close.add_argument("--host-id", required=True)
    close.add_argument("--ttl-seconds", type=int, default=60)
    close.add_argument("--json", action="store_true")
    _select(close, CloseCommand)


def _add_inspection_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    root = commands.add_parser("root", help="Resolve the source checkout, shared repository, and work roots.")
    _select(root, RootCommand)
    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")
    _select(validate, ValidateCommand)
    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")
    _select(status, StatusCommand)
    _add_chat_parser(commands)
    _add_item_parser(commands)
    actions = commands.add_parser("actions", help="List the legal contextual actions.")
    actions.add_argument("--role", choices=tuple(role.value for role in decision_models.Role), required=True)
    actions.add_argument("--lease-id")
    actions.add_argument("--generation", type=int)
    actions.add_argument("--action-id", help="Return only this exact currently legal action.")
    actions.add_argument("--json", action="store_true")
    _select_decoder(actions, _decode_actions)
    input_contract = commands.add_parser(
        "input-contract", help="Show the canonical payload and semantics for one action kind."
    )
    input_contract.add_argument("action_kind", choices=INPUT_CONTRACT_ACTION_KINDS)
    input_contract.add_argument("--json", action="store_true")
    _select(input_contract, InputContractCommand)
    brief_sources = commands.add_parser(
        "brief-sources",
        help="Plan or emit deterministic context-bounded authority source batches.",
    )
    brief_sources.add_argument("--file", type=Path, required=True, help="pinboard-brief-sources/v1 manifest.")
    brief_sources.add_argument("--max-batch-bytes", type=int, default=24_000)
    brief_source_output = brief_sources.add_mutually_exclusive_group(required=True)
    brief_source_output.add_argument("--json", action="store_true", help="Print the complete batch plan.")
    brief_source_output.add_argument("--emit-batch", type=int, help="Print exactly one zero-based planned batch.")
    _select_decoder(brief_sources, _decode_brief_sources)


def _add_brief_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    brief = commands.add_parser("brief", help="Publish canonical typed work briefs without scheduling them.")
    operations = brief.add_subparsers(required=True)
    publish = operations.add_parser("publish", help="Validate and immutably publish one pinboard-work-brief/v2 file.")
    publish.add_argument("--file", type=Path, required=True)
    publish.add_argument("--json", action="store_true")
    _select(publish, BriefPublishCommand)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pinboard", description="Inspect and transition one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path, help="Select the exact source checkout for authority reads.")
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(required=True)
    _add_inspection_parsers(commands)
    initialize = commands.add_parser("init", help="Create an empty current SQLite work state.")
    _select(initialize, InitializeCommand)
    _add_brief_parser(commands)
    proposal = commands.add_parser("proposal", help="Create one visible intake candidate without activating it.")
    proposal.add_argument("--file", type=Path, required=True)
    _select(proposal, ProposalCommand)
    transition = commands.add_parser("transition", help="Apply one action returned by the actions command.")
    transition.add_argument("--action-id", required=True)
    transition.add_argument("--expected-revision", required=True)
    transition.add_argument("--generation", required=True, type=int)
    transition.add_argument("--subject-revision")
    transition.add_argument("--lease-id")
    transition.add_argument(
        "--authorization", choices=("coordinator", "coordination", "attempt"), default="coordinator"
    )
    transition.add_argument("--payload", required=True, type=Path)
    _select_decoder(transition, _decode_transition)
    dispatch = commands.add_parser("dispatch", help="Prepare or verify a canonical worker launch.")
    dispatch.add_argument("--action-id", required=True, help="Exact dispatch action returned by coordinator actions.")
    dispatch.add_argument("--expected-revision", required=True, help="Ledger revision from the dispatch action.")
    dispatch.add_argument("--generation", required=True, type=int, help="Coordinator generation from the action.")
    dispatch.add_argument("--lease-id", help="Current coordination lease identity.")
    dispatch.add_argument("--checkpoint", required=True, help="Stable checkpoint ID in the canonical work brief.")
    dispatch.add_argument(
        "--environment",
        required=True,
        type=Path,
        help="pinboard-dispatch/v1 JSON describing the checkout, branch, revision, and permissions.",
    )
    dispatch.add_argument(
        "--prompt",
        type=Path,
        help="Verify this transported prompt instead of rendering the canonical prompt.",
    )
    dispatch.add_argument(
        "--brief-review",
        type=Path,
        help="Validate and publish one complete ready review for this exact cross-boundary checkpoint.",
    )
    dispatch.add_argument(
        "--review-id",
        help="Kebab-case identity used only when preserving a differing later review.",
    )
    _select_decoder(dispatch, _decode_dispatch)
    _add_coordination_parser(commands)
    _add_attempt_parser(commands)
    _add_parallel_parser(commands)
    views = commands.add_parser("views", help="Repair generated human-readable views.")
    rebuild = views.add_subparsers(required=True).add_parser("rebuild")
    _select(rebuild, RebuildViewsCommand)
    return parser


def _resolve_roots(selection: RootSelection) -> ResolvedRoots:
    project_argument = selection.project_root
    selected_checkout = Path.cwd() if project_argument is None else project_argument
    try:
        source_checkout = resolve_source_checkout_root(selected_checkout)
        shared_repository = resolve_shared_repository_root(source_checkout)
    except RootError as error:
        if project_argument is None or error.code != RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE:
            raise
        source_checkout = project_argument.resolve()
        shared_repository = source_checkout
    work_argument = selection.work_root
    work = work_argument.resolve() if work_argument is not None else shared_repository / ".codex" / "pinboard"
    return ResolvedRoots(source_checkout, shared_repository, work, work_argument is not None)


def _diagnostic_view(report: ValidationReport) -> ValidationView:
    return ValidationView(
        valid=report.valid,
        diagnostics=tuple(
            DiagnosticView(
                code=diagnostic.code,
                severity=diagnostic.severity.value,
                path=str(diagnostic.path),
                message=diagnostic.message,
                hint=diagnostic.hint,
            )
            for diagnostic in report.diagnostics
        ),
    )


def _brief_views(roots: ResolvedRoots, store: SQLiteWorkStore) -> dict[AttemptId, bytes]:
    return build_attempt_brief_views(
        store.snapshot(),
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
    )


def _status_value(work: Path, source_checkout: Path, shared_repository: Path) -> StatusView:
    state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
    overview = overview_from_state(state)
    coordinator = state.authority.coordination
    return StatusView(
        valid=True,
        source_checkout_root=str(source_checkout),
        shared_repository_root=str(shared_repository),
        work_root=str(work),
        revision=str(state.lifecycle.project.revision),
        focus_item=overview.focus_item,
        focus_attempt=overview.focus_attempt,
        active_attempts=overview.active_attempts,
        next_action=state.focus.next_action,
        counts=dict(Counter(item.state.value for item in state.lifecycle.work_items)),
        visible_candidate_count=sum(1 for item in overview.items if item.state == work_models.WorkState.INTAKE),
        coordinator=(
            CoordinatorView(
                str(coordinator.task_id),
                str(coordinator.host_id),
                coordinator.generation,
                str(coordinator.lease_id),
                coordinator.expires_at.isoformat(),
                coordinator.state.value,
            )
            if coordinator is not None
            else None
        ),
        authority="sqlite-v1",
    )


def _action_from_command(command: TransitionCommand | DispatchCommand) -> decision_models.Action:
    action_id = command.action_id
    if ":" not in action_id:
        raise CommandError(CommandErrorCode.ACTION_ID_INVALID, "Action identity must be 'kind:subject'.")
    kind_value, subject = action_id.split(":", 1)
    try:
        kind = decision_models.ActionKind(kind_value)
    except ValueError as error:
        raise CommandError(CommandErrorCode.ACTION_ID_INVALID, f"Unknown action kind: {error}.") from error
    match command:
        case CoordinatorTransitionCommand(subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.COORDINATOR
            lease_id = None
        case CoordinationTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.COORDINATION
        case AttemptTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.ATTEMPT
        case CoordinatorDispatchCommand() | CoordinatorReviewedDispatchCommand():
            authorization = decision_models.AuthorizationKind.COORDINATOR
            lease_id = None
            subject_revision = None
        case CoordinationDispatchCommand(lease_id=lease_id) | CoordinationReviewedDispatchCommand(lease_id=lease_id):
            authorization = decision_models.AuthorizationKind.COORDINATION
            subject_revision = None
        case _ as unreachable:
            assert_never(unreachable)
    attempt_kinds = {
        decision_models.ActionKind.ACCEPT_CHECKPOINT,
        decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
        decision_models.ActionKind.BLOCK,
        decision_models.ActionKind.COMPLETE,
        decision_models.ActionKind.CONTINUE,
        decision_models.ActionKind.DISPATCH,
        decision_models.ActionKind.PAUSE,
        decision_models.ActionKind.REPORT_BLOCKER,
        decision_models.ActionKind.RETURN_FOR_CORRECTION,
        decision_models.ActionKind.SUBMIT_REVIEW,
    }
    proposal_kinds = {
        decision_models.ActionKind.ACCEPT_PROPOSAL,
        decision_models.ActionKind.MERGE_PROPOSAL,
        decision_models.ActionKind.REJECT_PROPOSAL,
        decision_models.ActionKind.RETURN_PROPOSAL,
    }
    subject_id: SubjectId
    if kind in attempt_kinds:
        subject_id = AttemptId(subject)
    elif kind in proposal_kinds:
        subject_id = ProposalId(subject)
    elif kind in {decision_models.ActionKind.INSPECT, decision_models.ActionKind.TRANSFER_COORDINATOR}:
        subject_id = LedgerId(subject)
    else:
        subject_id = ItemId(subject)
    return decision_models.Action(
        action_id=action_id,
        kind=kind,
        subject=subject_id,
        label=str(action_id),
        expected_revision=command.expected_revision,
        coordinator_generation=command.generation,
        subject_revision=subject_revision,
        authorization=authorization,
        lease_id=lease_id,
    )


def _reselect_action(
    roots: ResolvedRoots, supplied: decision_models.Action, role: decision_models.Role
) -> decision_models.Action:
    try:
        available = discover_actions(
            SQLiteWorkStore(roots.work / "state.sqlite3"),
            role,
            lease_id=supplied.lease_id,
            generation=supplied.coordinator_generation,
        )
    except ActionQueryError as error:
        raise CommandError(CommandErrorCode(error.code.value), str(error).partition(": ")[2]) from error
    current = next((value for value in available if value.action_id == supplied.action_id), None)
    if current is None:
        raise CommandError(
            CommandErrorCode.ACTION_NOT_AVAILABLE, f"Action '{supplied.action_id}' is not currently legal."
        )
    if current.expected_revision != supplied.expected_revision:
        raise CommandError(CommandErrorCode.STALE_ACTION, "The work ledger changed after this action was selected.")
    supplied_capability = (
        supplied.kind,
        supplied.subject,
        supplied.coordinator_generation,
        supplied.subject_revision,
        supplied.authorization,
        supplied.lease_id,
    )
    current_capability = (
        current.kind,
        current.subject,
        current.coordinator_generation,
        current.subject_revision,
        current.authorization,
        current.lease_id,
    )
    if current_capability != supplied_capability:
        raise CommandError(
            CommandErrorCode.ACTION_NOT_AVAILABLE,
            f"Action '{supplied.action_id}' no longer has exact current authority.",
        )
    return current


def _root(roots: ResolvedRoots, _command: RootCommand) -> int:
    _write_json(RootView(str(roots.source_checkout), str(roots.shared_repository), str(roots.work)))
    return 0


def _validate(roots: ResolvedRoots, command: ValidateCommand) -> int:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    try:
        attempt_briefs = _brief_views(roots, store)
    except WorkBriefError as error:
        report = validate_work_state(roots.work)
        report = ValidationReport(
            (
                *report.diagnostics,
                Diagnostic(error.code.value, Severity.ERROR, roots.work, error.message),
            )
        )
    except ArtifactError, StorageError:
        report = validate_work_state(roots.work)
    else:
        report = validate_work_state(roots.work, attempt_briefs)
    if command.json:
        _write_json(_diagnostic_view(report))
    else:
        print(report.render())
    return 0 if report.valid else 10


def _status(roots: ResolvedRoots, command: StatusCommand) -> int:
    value = _status_value(roots.work, roots.source_checkout, roots.shared_repository)
    if command.json:
        _write_json(value)
    else:
        print(f"OK WORK_STATE_VALID revision={value.revision}")
        print(f"focus_item={value.focus_item or 'none'} focus_attempt={value.focus_attempt or 'none'}")
        print(f"next_action={value.next_action} visible_candidates={value.visible_candidate_count}")
    return 0


def _overview(roots: ResolvedRoots, command: OverviewCommand) -> int:
    overview = overview_from_state(SQLiteWorkStore(roots.work / "state.sqlite3").snapshot())
    if command.json:
        _write_json(_overview_view(overview))
        return 0
    print(f"OK WORK_OVERVIEW revision={overview.revision} authority={overview.authority}")
    if not overview.items:
        print("live_work=none")
    for item in overview.items:
        attempt = f" attempt={item.attempt_id}" if item.attempt_id is not None else ""
        next_action = item.next_action or "none"
        print(
            f"{item.position}\t{item.item_id}\t{item.state.value}\teligible={str(item.eligible).lower()}"
            f"\tnext={next_action}{attempt}\t{item.label}"
        )
    print(
        f"visible_candidates={sum(1 for item in overview.items if item.state == work_models.WorkState.INTAKE)} "
        f"immediate_options={len(overview.immediate_options)}"
    )
    return 0


def _item_status(roots: ResolvedRoots, command: ItemStatusCommand) -> int:
    status = item_status(SQLiteWorkStore(roots.work / "state.sqlite3"), command.item_id)
    if command.json:
        _write_json(status)
        return 0
    print(
        f"OK ITEM_STATUS item={status.item_id} state={status.state.value} "
        f"revision={status.revision} authority={status.authority}"
    )
    print(
        f"label={status.label} timing={status.timing.value if status.timing is not None else 'none'} "
        f"next_action={status.next_action or 'none'}"
    )
    print(f"outcome_evidence={status.outcome_evidence or 'none'} notes={status.notes or 'none'}")
    if not status.attempts:
        print("attempts=none")
    for attempt in status.attempts:
        print(
            f"attempt={attempt.attempt_id} state={attempt.state.value} candidate={attempt.candidate_revision or 'none'}"
        )
    return 0


def _close(roots: ResolvedRoots, command: CloseCommand) -> int:
    payload = msgspec.json.encode({"outcome": command.outcome.value, "reason": command.reason}, order="sorted")
    revision = _execute_borrowed_coordination(
        roots,
        command.task_id,
        command.host_id,
        command.ttl_seconds,
        ActionId(f"close:{command.item_id}"),
        payload,
    )
    value = CloseView(
        command.item_id,
        command.outcome.value,
        command.reason,
        revision,
    )
    if command.json:
        _write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0


def _actions(roots: ResolvedRoots, command: ActionsCommand | LeasedActionsCommand) -> int:
    match command:
        case ActionsCommand():
            lease_id = None
            generation = None
        case LeasedActionsCommand(lease_id=lease_id, generation=generation):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    available = discover_actions(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        command.role,
        lease_id=lease_id,
        generation=generation,
    )
    exact_action_id = command.action_id
    if exact_action_id is not None:
        available = tuple(action for action in available if action.action_id == exact_action_id)
        if not available:
            raise CommandError(
                CommandErrorCode.ACTION_NOT_AVAILABLE,
                f"Action '{exact_action_id}' is not currently legal for this role and lease.",
            )
    if command.json:
        _write_json(
            ActionsView(
                tuple(
                    _action_view(
                        action,
                        include_input_contract=exact_action_id is not None,
                    )
                    for action in available
                )
            )
        )
    elif not available:
        print("OK NO_ACTIONS_AVAILABLE")
    else:
        for action in available:
            print(f"{action.action_id}\t{action.label}")
    return 0


def _input_contract(_roots: ResolvedRoots, command: InputContractCommand) -> int:
    value = _input_contract_view(command.action_kind)
    if command.json:
        _write_json(value)
    else:
        print(f"OK INPUT_CONTRACT action_kind={value.action_kind}")
        if value.semantics is not None:
            print(
                f"effect={value.semantics.effect} required_role={value.semantics.required_role} "
                f"subject_kind={value.semantics.subject_kind} "
                f"lifecycle_precondition={value.semantics.lifecycle_precondition}"
            )
        if value.payload_schema is None:
            print("payload_schema=none")
        else:
            sys.stdout.write(msgspec.json.format(bytes(value.payload_schema), indent=2).decode() + "\n")
    return 0


def _brief_sources(
    roots: ResolvedRoots,
    command: BriefSourcesPlanCommand | BriefSourcesEmitCommand,
) -> int:
    try:
        raw_manifest = command.file.read_bytes()
    except OSError as error:
        raise BriefSourceError(
            BriefSourceErrorCode.MANIFEST_INVALID,
            f"Cannot read brief source manifest '{command.file}': {error}",
        ) from error
    plan = plan_brief_sources(
        roots.source_checkout,
        decode_brief_source_manifest(raw_manifest),
        command.max_batch_bytes,
    )
    match command:
        case BriefSourcesPlanCommand():
            _write_json(_brief_source_plan_view(plan))
        case BriefSourcesEmitCommand(emit_batch=batch_index):
            sys.stdout.write(render_brief_source_batch(plan, batch_index).decode("utf-8"))
        case _ as unreachable:
            assert_never(unreachable)
    return 0


def _brief_publish(roots: ResolvedRoots, command: BriefPublishCommand) -> int:
    try:
        candidate = command.file.read_bytes()
    except OSError as error:
        raise WorkBriefError(
            WorkBriefErrorCode.BRIEF_INVALID,
            f"Cannot read work brief candidate '{command.file}': {error}",
        ) from error
    brief = decode_work_brief(candidate)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    accepted = publish_accepted_artifact(
        store,
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
        NewArtifact(
            ArtifactKind.BRIEF,
            brief.attempt_id,
            brief.artifact_revision,
            ".json",
            canonical_work_brief_bytes(brief),
        ),
        datetime.now(UTC),
    )
    view_result = rebuild_views(store, roots.work, _brief_views(roots, store))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    view = BriefPublicationView(
        int(accepted.artifact_ref_id),
        accepted.kind.value,
        accepted.key,
        accepted.revision,
        accepted.selector,
        accepted.content_sha256,
        accepted.size_bytes,
        accepted.accepted_revision,
    )
    if command.json:
        _write_json(view)
    else:
        print(
            f"OK BRIEF_PUBLISHED artifact_ref_id={view.artifact_ref_id} selector={view.selector} "
            f"accepted_revision={view.accepted_revision}"
        )
    return 0


def _initialize(roots: ResolvedRoots, _command: InitializeCommand) -> int:
    selected_work = roots.work if roots.explicit_work_root else None
    receipt = initialize_work_state(roots.shared_repository, selected_work)
    initialized = receipt.work_root
    store = SQLiteWorkStore(receipt.database_path)
    initialized_roots = ResolvedRoots(
        roots.source_checkout,
        roots.shared_repository,
        initialized,
        roots.explicit_work_root,
    )
    rebuilt = rebuild_views(store, initialized, _brief_views(initialized_roots, store))
    if rebuilt.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, rebuilt.warning.message)
    print(f"OK WORK_STATE_INITIALIZED {initialized}")
    return 0


def _proposal(roots: ResolvedRoots, command: ProposalCommand) -> int:
    path = command.file
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ProposalError(ProposalErrorCode.PROPOSAL_INVALID, f"Cannot read proposal at '{path}': {error}") from error
    proposal = parse_proposal(data)
    try:
        created_at = datetime.fromisoformat(proposal.created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProposalError(
            ProposalErrorCode.PROPOSAL_INVALID, "Proposal created_at must be an ISO timestamp."
        ) from error
    if created_at.tzinfo is None:
        if len(proposal.created_at) == 10:
            created_at = created_at.replace(tzinfo=UTC)
        else:
            raise ProposalError(ProposalErrorCode.PROPOSAL_INVALID, "Proposal created_at must include a timezone.")
    intake = ProposalIntake(
        ProposalId(proposal.proposal_id),
        created_at.astimezone(UTC),
        TaskId(proposal.source_task_id),
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        proposal.effect,
        proposal.unlock,
        proposal.relation.kind,
        None if proposal.relation.item is None else ItemId(proposal.relation.item),
        proposal.urgency_evidence,
        proposal.evidence,
        proposal.freshness_assumptions,
        proposal.position,
    )
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    result = create_proposal(store, CreateProposalOperation(intake), datetime.now(UTC))
    if isinstance(result, DecisionFailure):
        raise ProposalError(ProposalErrorCode(result.code.value), result.message)
    view_result = refresh_views(
        store,
        roots.work,
        AffectedViews(queue=True, history=True),
        _brief_views(roots, store),
    )
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    visible = next(
        value for value in store.snapshot().lifecycle.work_items if str(value.item_id) == proposal.proposal_id
    )
    print(f"OK PROPOSAL_CREATED {proposal.proposal_id} position={visible.queue_position} state={visible.state.value}")
    return 0


def _transition(roots: ResolvedRoots, cli_command: TransitionCommand) -> int:
    payload_path = cli_command.payload
    action = _action_from_command(cli_command)
    try:
        payload = payload_path.read_bytes()
    except OSError as error:
        raise CommandError(
            CommandErrorCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}"
        ) from error
    role = (
        decision_models.Role.WORKER
        if action.authorization == decision_models.AuthorizationKind.ATTEMPT
        else decision_models.Role.COORDINATOR
    )
    action = _reselect_action(roots, action, role)
    parsed = parse_transition_input(action.kind.value, payload)
    command = bind_transition(action, parsed)
    if isinstance(command, DecisionFailure):
        raise CommandError(CommandErrorCode(command.code.value), command.message)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    result = execute(
        store,
        command,
        datetime.now(UTC),
        lambda state, current: validate_transition_work_brief(
            state,
            current,
            artifacts,
            decode_work_brief_identity,
        ),
    )
    if isinstance(result, DecisionFailure):
        raise CommandError(CommandErrorCode(result.code.value), result.message)
    state = store.snapshot()
    affected = AffectedViews(
        queue=True,
        current_focus=True,
        history=True,
        items=(result.item,) if result.item is not None else (),
        attempts=(AttemptId(action.subject),)
        if any(attempt.attempt_id == action.subject for attempt in state.lifecycle.attempts)
        else (),
    )
    view_result = refresh_views(store, roots.work, affected, _brief_views(roots, store))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    revision = str(state.lifecycle.project.revision)
    print(f"OK TRANSITION_APPLIED {action.action_id} revision={revision}")
    return 0


def _prepare_dispatch(roots: ResolvedRoots, command: DispatchCommand) -> int:
    environment = read_dispatch_environment(command.environment)
    supplied_prompt: bytes | None = None
    if command.prompt is not None:
        try:
            supplied_prompt = command.prompt.read_bytes()
        except OSError as error:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_PROMPT_UNREADABLE,
                f"Cannot read '{command.prompt}': {error}",
            ) from error
    match command:
        case CoordinatorReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id) | (
            CoordinationReviewedDispatchCommand(brief_review=brief_review_path, review_id=review_id)
        ):
            pass
        case CoordinatorDispatchCommand() | CoordinationDispatchCommand():
            brief_review_path = None
            review_id = None
        case _ as unreachable:
            assert_never(unreachable)
    brief_review: bytes | None = None
    if brief_review_path is not None:
        try:
            brief_review = brief_review_path.read_bytes()
        except OSError as error:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
                f"Cannot read '{brief_review_path}': {error}",
            ) from error
    action = _action_from_command(command)
    action = _reselect_action(roots, action, decision_models.Role.COORDINATOR)
    prompt = prepare_dispatch(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
        prepare_dispatch_from_artifact,
        roots.source_checkout,
        action,
        command.checkpoint,
        environment,
        supplied_prompt,
        brief_review,
        review_id,
    )
    if supplied_prompt is None:
        sys.stdout.write(prompt)
    else:
        print("OK DISPATCH_READY")
    return 0


def _coordination_values(roots: ResolvedRoots) -> dict[str, str | int] | None:
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    value = state.authority.coordination
    if value is None:
        return None
    return {
        "task_id": str(value.task_id),
        "host_id": str(value.host_id),
        "lease_id": str(value.lease_id),
        "generation": value.generation,
        "acquired_at": value.acquired_at.isoformat(),
        "expires_at": value.expires_at.isoformat(),
        "status": value.state.value,
    }


def _emit_coordination(roots: ResolvedRoots, *, json: bool) -> int:
    values = _coordination_values(roots)
    if json:
        _write_json({"lease": None} if values is None else values)
    elif values is None:
        print("OK COORDINATION_AVAILABLE")
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _retained_coordination(state: StoredWorkState) -> StoredCoordinationLease:
    current = state.authority.coordination
    if current is None:
        raise CommandError(CommandErrorCode.COORDINATION_LEASE_REQUIRED, "Coordination authority does not exist.")
    return current


def _supplied_coordination_authority(
    state: StoredWorkState,
    current: StoredCoordinationLease,
    lease_id: LeaseId,
    generation: int,
) -> work_models.CoordinationCommandAuthority:
    return work_models.CoordinationCommandAuthority(
        state.lifecycle.project.host_epoch,
        current.task_id,
        current.host_id,
        lease_id,
        generation,
        current.expires_at,
    )


def _coordination(
    roots: ResolvedRoots,
    command: (
        CoordinationApplyCommand
        | CoordinationAcquireCommand
        | CoordinationRenewCommand
        | CoordinationReleaseCommand
        | CoordinationRevokeCommand
        | CoordinationStatusCommand
    ),
) -> int:
    if isinstance(command, CoordinationStatusCommand):
        return _emit_coordination(roots, json=command.json)
    if isinstance(command, CoordinationApplyCommand):
        return _coordinated_transition(roots, command)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    state = store.snapshot()
    now = datetime.now(UTC)
    match command:
        case CoordinationAcquireCommand(task_id=task_id, host_id=host_id, ttl_seconds=ttl_seconds):
            authority_operation = AcquireCoordinationAuthority(
                state.lifecycle.project.host_epoch,
                task_id,
                host_id,
                LeaseId(uuid4().hex),
                now,
                now + timedelta(seconds=ttl_seconds),
            )
        case CoordinationRenewCommand(lease_id=lease_id, generation=generation, ttl_seconds=ttl_seconds):
            authority_operation = RenewCoordinationAuthority(
                _supplied_coordination_authority(state, _retained_coordination(state), lease_id, generation),
                now,
                now + timedelta(seconds=ttl_seconds),
            )
        case CoordinationReleaseCommand(lease_id=lease_id, generation=generation):
            authority_operation = ReleaseCoordinationAuthority(
                _supplied_coordination_authority(state, _retained_coordination(state), lease_id, generation), now
            )
        case CoordinationRevokeCommand():
            current = _retained_coordination(state)
            authority_operation = RevokeCoordinationAuthority(current.lease_id, current.generation, now)
        case _ as unreachable:
            assert_never(unreachable)
    result = change_coordination_authority(store, authority_operation)
    if isinstance(result, DecisionFailure):
        raise CommandError(CommandErrorCode(result.code.value), result.message)
    view_result = refresh_views(
        store,
        roots.work,
        AffectedViews(queue=True, current_focus=True, history=True),
        _brief_views(roots, store),
    )
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    return _emit_coordination(roots, json=command.json)


def _coordinated_transition(roots: ResolvedRoots, command: CoordinationApplyCommand) -> int:
    payload_path = command.payload
    try:
        payload = payload_path.read_bytes()
    except OSError as error:
        raise CommandError(
            CommandErrorCode.TRANSITION_INPUT_INVALID, f"Cannot read transition payload: {error}"
        ) from error
    transition_revision = _execute_borrowed_coordination(
        roots,
        command.task_id,
        command.host_id,
        command.ttl_seconds,
        command.action_id,
        payload,
    )
    value = CoordinatedTransitionView(command.action_id, transition_revision)
    if command.json:
        _write_json(value)
    else:
        print(f"OK COORDINATED_TRANSITION action={value.action_id} revision={value.revision}")
    return 0


def _execute_borrowed_coordination(
    roots: ResolvedRoots,
    task_id: TaskId,
    host_id: HostId,
    ttl_seconds: int,
    action_id: ActionId,
    payload: bytes,
) -> str:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    now = datetime.now(UTC)
    state = store.snapshot()
    acquire = AcquireCoordinationAuthority(
        state.lifecycle.project.host_epoch,
        task_id,
        host_id,
        LeaseId(uuid4().hex),
        now,
        now + timedelta(seconds=ttl_seconds),
    )
    acquired = change_coordination_authority(store, acquire)
    if isinstance(acquired, DecisionFailure):
        raise CommandError(CommandErrorCode(acquired.code.value), acquired.message)
    transition_revision: str | None = None
    try:
        current_state = store.snapshot()
        coordination = _retained_coordination(current_state)
        available = discover_actions(
            store,
            decision_models.Role.COORDINATOR,
            lease_id=coordination.lease_id,
            generation=coordination.generation,
        )
        action = next(
            (candidate for candidate in available if candidate.action_id == action_id),
            None,
        )
        if action is None:
            raise CommandError(CommandErrorCode.ACTION_NOT_AVAILABLE, f"Action '{action_id}' is not currently legal.")
        parsed = parse_transition_input(action.kind.value, payload)
        command = bind_transition(action, parsed)
        if isinstance(command, DecisionFailure):
            raise CommandError(CommandErrorCode(command.code.value), command.message)
        result = execute(
            store,
            command,
            datetime.now(UTC),
            lambda state, current: validate_transition_work_brief(
                state,
                current,
                artifacts,
                decode_work_brief_identity,
            ),
        )
        if isinstance(result, DecisionFailure):
            raise CommandError(CommandErrorCode(result.code.value), result.message)
        transition_revision = str(store.snapshot().lifecycle.project.revision)
    finally:
        current = store.snapshot().authority.coordination
        if current is not None and current.lease_id == acquire.lease_id:
            released = change_coordination_authority(
                store,
                ReleaseCoordinationAuthority(
                    work_models.CoordinationCommandAuthority(
                        store.snapshot().lifecycle.project.host_epoch,
                        current.task_id,
                        current.host_id,
                        current.lease_id,
                        current.generation,
                        current.expires_at,
                    ),
                    datetime.now(UTC),
                ),
            )
            if isinstance(released, DecisionFailure) and transition_revision is None:
                raise CommandError(CommandErrorCode(released.code.value), released.message)
    view_result = rebuild_views(store, roots.work, _brief_views(roots, store))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    if transition_revision is None:
        raise CommandError(CommandErrorCode.WORK_STATE_INVALID, "The coordinated transition produced no revision.")
    return transition_revision


def _emit_attempt_authority(state: StoredWorkState, attempt_id: AttemptId, *, json: bool) -> int:
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    if lease is None:
        raise CommandError(
            CommandErrorCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' has no retained authority."
        )
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        raise CommandError(CommandErrorCode.WORK_STATE_INVALID, "Attempt authority has no exact identity anchor.")
    values: dict[str, str | int] = {
        "attempt_id": str(attempt_id),
        "task_id": str(anchor.task_id),
        "host_id": str(anchor.host_id),
        "lease_id": str(anchor.lease_id),
        "generation": lease.generation,
        "acquired_at": lease.acquired_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "status": lease.state.value,
    }
    if json:
        _write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _attempt_status(roots: ResolvedRoots, command: AttemptStatusCommand) -> int:
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    return _emit_attempt_authority(state, command.attempt_id, json=command.json)


def _current_attempt(state: StoredWorkState, attempt_id: AttemptId) -> StoredAttempt:
    attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
    if attempt is None:
        raise CommandError(CommandErrorCode.ATTEMPT_LEASE_REQUIRED, f"Attempt '{attempt_id}' is not current.")
    return attempt


def _attempt_acquire_operation(
    state: StoredWorkState,
    attempt: StoredAttempt,
    command: AttemptAcquireCommand | CoordinatedAttemptAcquireCommand,
    now: datetime,
) -> AttemptAuthorityOperation:
    attempt_id = command.attempt_id
    retained_record = next(
        (value for value in state.authority.attempt_leases if value.attempt_id == attempt_id),
        None,
    )
    lease_id = LeaseId(uuid4().hex)
    if retained_record is None:
        return AcquireInitialAttemptAuthority(
            state.lifecycle.project.host_epoch,
            attempt_id,
            attempt.item_id,
            command.task_id,
            command.host_id,
            lease_id,
            now,
            now + timedelta(seconds=command.ttl_seconds),
        )
    inactive = project_inactive_attempt_authority(state, attempt_id, now)
    if isinstance(inactive, DecisionFailure):
        raise CommandError(CommandErrorCode(inactive.code.value), inactive.message)
    coordination = state.authority.coordination
    if coordination is None:
        raise CommandError(CommandErrorCode.COORDINATION_LEASE_REQUIRED, "Attempt reacquisition requires coordination.")
    if isinstance(command, AttemptAcquireCommand):
        raise CommandError(
            CommandErrorCode.COORDINATION_LEASE_REQUIRED,
            "Attempt reacquisition requires the exact coordination lease and generation.",
        )
    return TransferAttemptAuthority(
        inactive,
        work_models.CoordinationCommandAuthority(
            state.lifecycle.project.host_epoch,
            coordination.task_id,
            coordination.host_id,
            command.coordination_lease_id,
            command.coordination_generation,
            coordination.expires_at,
        ),
        command.task_id,
        command.host_id,
        lease_id,
        now,
        now + timedelta(seconds=command.ttl_seconds),
    )


def _attempt_renew_operation(
    state: StoredWorkState,
    command: AttemptRenewCommand,
    now: datetime,
) -> RenewAttemptAuthority:
    retained = next(
        (
            value
            for value in project_decision_snapshot(state).command_attempt_authorities
            if value.attempt == command.attempt_id
        ),
        None,
    )
    if retained is None:
        raise CommandError(CommandErrorCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.")
    return RenewAttemptAuthority(
        replace(retained, lease_id=command.lease_id, generation=command.generation),
        now,
        now + timedelta(seconds=command.ttl_seconds),
    )


def _attempt_release_operation(
    state: StoredWorkState,
    command: AttemptReleaseCommand,
    now: datetime,
) -> ReleaseAttemptAuthority:
    retained = next(
        (
            value
            for value in project_decision_snapshot(state).command_attempt_authorities
            if value.attempt == command.attempt_id
        ),
        None,
    )
    if retained is None:
        raise CommandError(CommandErrorCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.")
    return ReleaseAttemptAuthority(
        replace(retained, lease_id=command.lease_id, generation=command.generation),
        now,
    )


def _attempt_revoke_operation(
    state: StoredWorkState,
    command: AttemptRevokeCommand,
    now: datetime,
) -> RevokeAttemptAuthority:
    coordination = state.authority.coordination
    if coordination is None:
        raise CommandError(CommandErrorCode.COORDINATION_LEASE_REQUIRED, "Coordination authority is absent.")
    return RevokeAttemptAuthority(
        command.attempt_id,
        command.lease_id,
        command.generation,
        work_models.CoordinationCommandAuthority(
            state.lifecycle.project.host_epoch,
            coordination.task_id,
            coordination.host_id,
            command.coordination_lease_id,
            command.coordination_generation,
            coordination.expires_at,
        ),
        now,
    )


def _change_attempt_authority(
    roots: ResolvedRoots,
    command: (
        AttemptAcquireCommand
        | CoordinatedAttemptAcquireCommand
        | AttemptRenewCommand
        | AttemptReleaseCommand
        | AttemptRevokeCommand
    ),
) -> int:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    state = store.snapshot()
    attempt = _current_attempt(state, command.attempt_id)
    now = datetime.now(UTC)
    match command:
        case AttemptAcquireCommand() | CoordinatedAttemptAcquireCommand():
            authority_operation = _attempt_acquire_operation(state, attempt, command, now)
        case AttemptRenewCommand():
            authority_operation = _attempt_renew_operation(state, command, now)
        case AttemptReleaseCommand():
            authority_operation = _attempt_release_operation(state, command, now)
        case AttemptRevokeCommand():
            authority_operation = _attempt_revoke_operation(state, command, now)
        case _ as unreachable:
            assert_never(unreachable)
    result = change_attempt_authority(store, authority_operation)
    if isinstance(result, DecisionFailure):
        raise CommandError(CommandErrorCode(result.code.value), result.message)
    refresh_result = refresh_views(
        store,
        roots.work,
        AffectedViews(queue=True, current_focus=True, history=True),
        _brief_views(roots, store),
    )
    if refresh_result.warning is not None:
        print(refresh_result.warning.message, file=sys.stderr)
    return _emit_attempt_authority(store.snapshot(), command.attempt_id, json=command.json)


def _print_parallel_group(title: str, items: tuple[ParallelItem, ...]) -> None:
    print(f"{title}:")
    if not items:
        print("- none")
        return
    for item in items:
        detail = "; ".join(reason.message for reason in item.reasons)
        attempt = f", attempt {item.attempt_id}" if item.attempt_id is not None else ""
        suffix = f" — {detail}" if detail else ""
        print(f"- {item.item_id} ({item.state.value}{attempt}){suffix}")


def _parallel(roots: ResolvedRoots, command: ParallelPreviewCommand) -> int:
    preview = preview_parallel(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        selected=tuple(command.item),
    )
    if command.json:
        _write_json(_parallel_preview_view(preview))
    else:
        print(
            f"OK PARALLEL_PREVIEW revision={preview.revision} selection={preview.selection.value} "
            f"safe={'yes' if preview.safe else 'no'}"
        )
        _print_parallel_group("Ready to launch together", preview.launchable)
        _print_parallel_group("Not launchable", preview.excluded)
    return 0


def _views(roots: ResolvedRoots, _command: RebuildViewsCommand) -> int:
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    result = rebuild_views(store, roots.work, _brief_views(roots, store))
    if result.warning is not None:
        raise FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, result.warning.message)
    print(f"OK VIEWS_REBUILT revision={result.database_revision}")
    return 0


def _decode_invocation(parser: argparse.ArgumentParser, raw: _RawCliArguments) -> CliInvocation:
    decoder = raw.decoder
    selected_parser = raw.selected_parser
    if selected_parser is None or decoder is None:
        parser.error("the selected command has no decoder")
    untyped_values = vars(raw).copy()
    for metadata_name in ("decoder", "selected_parser"):
        untyped_values.pop(metadata_name, None)
    values = cast("RawCliValues", untyped_values)
    root_values: RawCliValues = {
        "project_root": values.pop("project_root", None),
        "work_root": values.pop("work_root", None),
    }
    try:
        roots = msgspec.convert(root_values, type=RootSelection, strict=True)
        command = decoder(values)
    except msgspec.ValidationError as error:
        selected_parser.error(str(error))
    return CliInvocation(roots, command)


def _dispatch(invocation: CliInvocation) -> int:  # noqa: C901, PLR0912 - exhaustive closed command dispatch
    roots = _resolve_roots(invocation.roots)
    match invocation.command:
        case RootCommand() as command:
            return _root(roots, command)
        case ValidateCommand() as command:
            return _validate(roots, command)
        case StatusCommand() as command:
            return _status(roots, command)
        case OverviewCommand() as command:
            return _overview(roots, command)
        case ItemStatusCommand() as command:
            return _item_status(roots, command)
        case CloseCommand() as command:
            return _close(roots, command)
        case ActionsCommand() | LeasedActionsCommand() as command:
            return _actions(roots, command)
        case InputContractCommand() as command:
            return _input_contract(roots, command)
        case (BriefSourcesPlanCommand() | BriefSourcesEmitCommand()) as command:
            return _brief_sources(roots, command)
        case BriefPublishCommand() as command:
            return _brief_publish(roots, command)
        case InitializeCommand() as command:
            return _initialize(roots, command)
        case ProposalCommand() as command:
            return _proposal(roots, command)
        case (CoordinatorTransitionCommand() | CoordinationTransitionCommand() | AttemptTransitionCommand()) as command:
            return _transition(roots, command)
        case (
            CoordinatorDispatchCommand()
            | CoordinatorReviewedDispatchCommand()
            | CoordinationDispatchCommand()
            | CoordinationReviewedDispatchCommand()
        ) as command:
            return _prepare_dispatch(roots, command)
        case (
            CoordinationApplyCommand()
            | CoordinationAcquireCommand()
            | CoordinationRenewCommand()
            | CoordinationReleaseCommand()
            | CoordinationRevokeCommand()
            | CoordinationStatusCommand()
        ) as command:
            return _coordination(roots, command)
        case AttemptStatusCommand() as command:
            return _attempt_status(roots, command)
        case (
            AttemptAcquireCommand()
            | CoordinatedAttemptAcquireCommand()
            | AttemptRenewCommand()
            | AttemptReleaseCommand()
            | AttemptRevokeCommand()
        ) as command:
            return _change_attempt_authority(roots, command)
        case ParallelPreviewCommand() as command:
            return _parallel(roots, command)
        case RebuildViewsCommand() as command:
            return _views(roots, command)
        case _ as unreachable:
            assert_never(unreachable)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = parser.parse_args(argv, namespace=_RawCliArguments())
    try:
        return _dispatch(_decode_invocation(parser, raw))
    except (RootError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (
        CommandError,
        TransitionInputError,
        ActionQueryError,
        QueryError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 11
    except (StorageError, ArtifactError, FileIOError) as error:
        print(str(error), file=sys.stderr)
        return 12
    except ProposalError as error:
        print(str(error), file=sys.stderr)
        return 2 if error.code == ProposalErrorCode.PROPOSAL_INVALID else 13
    except DispatchError as error:
        print(str(error), file=sys.stderr)
        return 14
    except BriefSourceError as error:
        print(str(error), file=sys.stderr)
        return 15
    except WorkBriefError as error:
        print(str(error), file=sys.stderr)
        return 16
