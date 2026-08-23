import argparse
import contextlib
import sys
from collections import Counter
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import assert_never
from uuid import uuid4

import msgspec

from charlie_pinboard import __version__
from charlie_pinboard.adapters.files.root import RootError, resolve_project_root
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    HostId,
    ItemId,
    LeaseId,
    LedgerId,
    ProposalId,
    ResourceId,
    SubjectId,
)
from charlie_pinboard.domain.resource_decisions import ResourceToken
from charlie_pinboard.identity import PROGRAM_NAME
from charlie_pinboard.interfaces.transition_input import (
    TRANSITION_ACTION_KINDS,
    CloseOutcome,
    TransitionInputError,
    encoded_legacy_transition_input_schema,
    parse_legacy_transition_input,
)
from charlie_pinboard.interfaces.transitions import TransitionError, apply_action
from charlie_pinboard.legacy.actions import (
    Action,
    ActionError,
    ActionKind,
    AuthorizationKind,
    Role,
    actions_for,
    state_revision,
)
from charlie_pinboard.legacy.authority import AuthorityVersion, authority_transaction, resolve_authority
from charlie_pinboard.legacy.coordinator import read_coordinator
from charlie_pinboard.legacy.dispatch import DispatchError, prepare_dispatch, read_dispatch_environment
from charlie_pinboard.legacy.leases import (
    LeaseError,
    LeaseRecord,
    acquire_attempt,
    acquire_coordination,
    read_attempt_lease,
    read_coordination_lease,
    release_attempt,
    release_coordination,
    renew_attempt,
    renew_coordination,
    revoke_attempt,
    revoke_coordination,
)
from charlie_pinboard.legacy.markdown import parse_current, parse_queue
from charlie_pinboard.legacy.migration import MigrationError, migrate_to_v2
from charlie_pinboard.legacy.overview import OverviewError, OverviewItem, WorkOverview, read_overview
from charlie_pinboard.legacy.parallel import ParallelError, ParallelItem, ParallelPreview, preview_parallel
from charlie_pinboard.legacy.proposals import ProposalError, create_proposal
from charlie_pinboard.legacy.registration import RegistrationError, initialize_work_state, initialize_work_state_v2
from charlie_pinboard.legacy.resources import (
    ResourceClaim,
    ResourceDeclaration,
    ResourceError,
    ResourceScope,
    claim_resource,
    declare_resource,
    read_resource,
    read_resource_claim,
    release_resource,
    renew_resource,
    revoke_resource,
)
from charlie_pinboard.legacy.transaction_store import AtomicCommitError, recover_pending_commit
from charlie_pinboard.legacy.validate import ValidationReport, validate_work_state


class CommandName(Enum):
    ROOT = "root"
    VALIDATE = "validate"
    STATUS = "status"
    OVERVIEW = "overview"
    CLOSE = "close"
    ACTIONS = "actions"
    INPUT_CONTRACT = "input-contract"
    RECOVER = "recover"
    INIT = "init"
    PROPOSAL = "proposal"
    TRANSITION = "transition"
    DISPATCH = "dispatch"
    MIGRATE = "migrate"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    RESOURCE = "resource"
    PARALLEL = "parallel"


class CoordinationOperation(Enum):
    APPLY = "apply"
    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    STATUS = "status"


class AttemptOperation(Enum):
    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    STATUS = "status"


class ResourceOperation(Enum):
    DECLARE = "declare"
    CLAIM = "claim"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    STATUS = "status"


class ParallelOperation(Enum):
    PREVIEW = "preview"


class CliArguments(argparse.Namespace):
    command: str
    project_root: Path | None
    work_root: Path | None
    json: bool
    role: str
    coordinator_task_id: str
    host_id: str
    file: Path
    action_id: str
    action_kind: str
    expected_revision: str
    generation: int
    subject_revision: str | None
    payload: Path
    checkpoint: str
    environment: Path
    prompt: Path | None
    brief_review: Path | None
    review_id: str | None
    operation: str
    lease_id: str | None
    authorization: str
    task_id: str
    ttl_seconds: int
    attempt_id: str
    attempt_lease_id: str
    attempt_generation: int
    coordination_lease_id: str
    coordination_generation: int
    resource_id: str
    resource_claim: list[list[str]] | None
    label: str
    scope: str
    to: str
    item: list[str]
    item_id: str
    outcome: str
    reason: str


@dataclass(frozen=True, slots=True)
class CommandContext:
    arguments: CliArguments
    project: Path
    work: Path


class RootView(msgspec.Struct, frozen=True):
    project_root: str
    work_root: str


class DiagnosticView(msgspec.Struct, frozen=True):
    code: str
    severity: str
    path: str
    message: str
    hint: str | None


class ValidationView(msgspec.Struct, frozen=True):
    valid: bool
    diagnostics: tuple[DiagnosticView, ...]


class CoordinatorView(msgspec.Struct, frozen=True):
    task_id: str
    host_id: str
    generation: int
    lease_id: str = ""
    expires_at: str = ""
    status: str = "legacy"


class StatusView(msgspec.Struct, frozen=True):
    valid: bool
    project_root: str
    work_root: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    next_action: str
    counts: dict[str, int]
    inbox_count: int
    coordinator: CoordinatorView | None
    authority: str = "v1"


class OverviewItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    timing: str | None
    depends_on: tuple[str, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str

    @classmethod
    def from_item(cls, item: OverviewItem) -> OverviewItemView:
        return cls(
            item.item_id,
            item.label,
            item.state.value,
            item.timing,
            item.depends_on,
            item.attempt_id,
            item.next_action,
            item.notes,
        )


class OverviewView(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItemView, ...]
    inbox: tuple[str, ...]
    immediate_options: tuple[str, ...]

    @classmethod
    def from_overview(cls, overview: WorkOverview) -> OverviewView:
        return cls(
            overview.schema,
            overview.authority,
            overview.revision,
            overview.focus_item,
            overview.focus_attempt,
            overview.active_attempts,
            tuple(OverviewItemView.from_item(item) for item in overview.items),
            overview.inbox,
            overview.immediate_options,
        )


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    payload_schema: msgspec.Raw

    @classmethod
    def from_kind(cls, kind: str) -> InputContractView:
        return cls(kind, msgspec.Raw(encoded_legacy_transition_input_schema(kind)))


class ActionView(msgspec.Struct, frozen=True, omit_defaults=True):
    action_id: str
    kind: str
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str
    authorization: str
    lease_id: str
    resource_claims: tuple[ResourceToken, ...]
    input_contract: InputContractView | None = None

    @classmethod
    def from_action(cls, action: Action, *, include_input_contract: bool = False) -> ActionView:
        input_contract: InputContractView | None = None
        if include_input_contract:
            try:
                input_contract = InputContractView.from_kind(action.kind.value)
            except TransitionInputError as error:
                if error.code != "ACTION_NOT_MUTATING":
                    raise
        return cls(
            action_id=action.action_id,
            kind=action.kind.value,
            subject=action.subject,
            label=action.label,
            expected_revision=action.expected_revision,
            coordinator_generation=action.coordinator_generation,
            subject_revision=action.subject_revision or "",
            authorization=action.authorization.value,
            lease_id=action.lease_id or "",
            resource_claims=action.resource_claims,
            input_contract=input_contract,
        )


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView, ...]


class CoordinatedTransitionView(msgspec.Struct, frozen=True):
    action_id: str
    revision: str


class RecoveryView(msgspec.Struct, frozen=True):
    recovered: bool
    revision: str


class ParallelReasonView(msgspec.Struct, frozen=True):
    code: str
    message: str


class ParallelItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    resources: tuple[str, ...]
    outcome: str
    reasons: tuple[ParallelReasonView, ...]

    @classmethod
    def from_item(cls, item: ParallelItem) -> ParallelItemView:
        return cls(
            item.item_id,
            item.label,
            item.state.value,
            item.attempt_id,
            item.resources,
            item.outcome.value,
            tuple(ParallelReasonView(reason.code.value, reason.message) for reason in item.reasons),
        )


class ParallelPreviewView(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    host_id: str
    selection: str
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    requires_selection: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]

    @classmethod
    def from_preview(cls, preview: ParallelPreview) -> ParallelPreviewView:
        return cls(
            preview.schema,
            preview.revision,
            preview.host_id,
            preview.selection.value,
            preview.safe,
            tuple(ParallelItemView.from_item(item) for item in preview.launchable),
            tuple(ParallelItemView.from_item(item) for item in preview.requires_selection),
            tuple(ParallelItemView.from_item(item) for item in preview.excluded),
        )


def _write_json[T](value: T) -> None:
    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")


def _add_coordination_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    coordination = commands.add_parser("coordination", help="Borrow or manage temporary graph-wide authority.")
    operations = coordination.add_subparsers(dest="operation", required=True)
    apply = operations.add_parser("apply", help="Borrow coordination for one exact transition and release it.")
    apply.add_argument("--task-id", required=True)
    apply.add_argument("--host-id", required=True)
    apply.add_argument("--action-id", required=True)
    apply.add_argument("--payload", required=True, type=Path)
    apply.add_argument("--ttl-seconds", type=int, default=60)
    apply.add_argument("--json", action="store_true")
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    for operation in ("renew", "release"):
        command = operations.add_parser(operation)
        command.add_argument("--lease-id", required=True)
        command.add_argument("--generation", required=True, type=int)
        if operation == "renew":
            command.add_argument("--ttl-seconds", required=True, type=int)
        command.add_argument("--json", action="store_true")
    operations.add_parser("revoke").add_argument("--json", action="store_true")
    operations.add_parser("status").add_argument("--json", action="store_true")


def _add_attempt_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    attempt = commands.add_parser("attempt", help="Manage a renewable attempt ownership claim.")
    operations = attempt.add_subparsers(dest="operation", required=True)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--attempt-id", required=True)
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    for operation in ("renew", "release"):
        command = operations.add_parser(operation)
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--lease-id", required=True)
        command.add_argument("--generation", required=True, type=int)
        if operation == "renew":
            command.add_argument("--ttl-seconds", required=True, type=int)
        command.add_argument("--json", action="store_true")
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--attempt-id", required=True)
    revoke.add_argument("--coordination-lease-id", required=True)
    revoke.add_argument("--coordination-generation", required=True, type=int)
    revoke.add_argument("--json", action="store_true")
    status = operations.add_parser("status")
    status.add_argument("--attempt-id", required=True)
    status.add_argument("--json", action="store_true")


def _add_resource_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    resource = commands.add_parser("resource", help="Declare and manage host-local exclusive resources.")
    operations = resource.add_subparsers(dest="operation", required=True)
    declare = operations.add_parser("declare")
    declare.add_argument("--resource-id", required=True)
    declare.add_argument("--label", required=True)
    declare.add_argument("--scope", choices=("host-local",), required=True)
    declare.add_argument("--coordination-lease-id", required=True)
    declare.add_argument("--coordination-generation", required=True, type=int)
    declare.add_argument("--json", action="store_true")
    claim = operations.add_parser("claim")
    claim.add_argument("--resource-id", required=True)
    claim.add_argument("--attempt-id", required=True)
    claim.add_argument("--task-id", required=True)
    claim.add_argument("--host-id", required=True)
    claim.add_argument("--ttl-seconds", required=True, type=int)
    claim.add_argument("--attempt-lease-id", required=True)
    claim.add_argument("--attempt-generation", required=True, type=int)
    claim.add_argument("--json", action="store_true")
    for operation in ("renew", "release"):
        command = operations.add_parser(operation)
        command.add_argument("--resource-id", required=True)
        command.add_argument("--host-id", required=True)
        command.add_argument("--lease-id", required=True)
        command.add_argument("--generation", required=True, type=int)
        if operation == "renew":
            command.add_argument("--ttl-seconds", required=True, type=int)
        command.add_argument("--json", action="store_true")
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--resource-id", required=True)
    revoke.add_argument("--host-id", required=True)
    revoke.add_argument("--coordination-lease-id", required=True)
    revoke.add_argument("--coordination-generation", required=True, type=int)
    revoke.add_argument("--json", action="store_true")
    status = operations.add_parser("status")
    status.add_argument("--resource-id", required=True)
    status.add_argument("--host-id")
    status.add_argument("--json", action="store_true")


def _add_parallel_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parallel = commands.add_parser("parallel", help="Preview structurally independent work without launching it.")
    operations = parallel.add_subparsers(dest="operation", required=True)
    preview = operations.add_parser("preview")
    preview.add_argument("--host-id", required=True)
    preview.add_argument("--item", action="append", default=[])
    preview.add_argument("--json", action="store_true")


def _add_chat_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    overview = commands.add_parser("overview", help="Show one coherent live-work snapshot.")
    overview.add_argument("--json", action="store_true")
    close = commands.add_parser("close", help="Record a terminal decision for non-active work.")
    close.add_argument("item_id")
    close.add_argument("--outcome", choices=tuple(outcome.value for outcome in CloseOutcome), required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--task-id")
    close.add_argument("--host-id")
    close.add_argument("--ttl-seconds", type=int, default=60)
    close.add_argument("--json", action="store_true")


def _add_inspection_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands.add_parser("root", help="Resolve the shared project and work roots.")
    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")
    _add_chat_parser(commands)
    actions = commands.add_parser("actions", help="List the legal contextual actions.")
    actions.add_argument("--role", choices=tuple(role.value for role in Role), required=True)
    actions.add_argument("--lease-id")
    actions.add_argument("--generation", type=int)
    actions.add_argument("--action-id", help="Return only this exact currently legal action.")
    actions.add_argument("--json", action="store_true")
    input_contract = commands.add_parser(
        "input-contract", help="Show the canonical JSON payload schema for one transition action kind."
    )
    input_contract.add_argument("action_kind", choices=TRANSITION_ACTION_KINDS)
    input_contract.add_argument("--json", action="store_true")
    recover = commands.add_parser("recover", help="Roll back a durable interrupted transition journal.")
    recover.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description="Inspect and transition one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    _add_inspection_parsers(commands)
    initialize = commands.add_parser("init", help="Create an empty schema-v2 ledger.")
    initialize.add_argument("--coordinator-task-id")
    initialize.add_argument("--host-id")
    proposal = commands.add_parser("proposal", help="Create one immutable inbox proposal.")
    proposal.add_argument("--file", type=Path, required=True)
    transition = commands.add_parser("transition", help="Apply one action returned by the actions command.")
    transition.add_argument("--action-id", required=True)
    transition.add_argument("--expected-revision", required=True)
    transition.add_argument("--generation", required=True, type=int)
    transition.add_argument("--subject-revision")
    transition.add_argument("--lease-id")
    transition.add_argument(
        "--resource-claim",
        action="append",
        nargs=4,
        metavar=("RESOURCE_ID", "HOST_ID", "LEASE_ID", "GENERATION"),
        help="Exact resource claim token returned by actions; repeat for each required resource.",
    )
    transition.add_argument(
        "--authorization", choices=("coordinator", "coordination", "attempt"), default="coordinator"
    )
    transition.add_argument("--payload", required=True, type=Path)
    dispatch = commands.add_parser("dispatch", help="Prepare or verify a canonical worker launch.")
    dispatch.add_argument("--action-id", required=True, help="Exact dispatch action returned by coordinator actions.")
    dispatch.add_argument("--expected-revision", required=True, help="Ledger revision from the dispatch action.")
    dispatch.add_argument("--generation", required=True, type=int, help="Coordinator generation from the action.")
    dispatch.add_argument("--lease-id", help="Current schema-v2 coordination lease identity.")
    dispatch.add_argument(
        "--checkpoint", required=True, help="Exact checkpoint heading in the canonical attempt brief."
    )
    dispatch.add_argument(
        "--environment",
        required=True,
        type=Path,
        help="repo-work-dispatch/v1 JSON describing the checkout, branch, revision, and permissions.",
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
    migrate = commands.add_parser("migrate", help="Migrate a schema-v1 ledger through one atomic v2 cutover.")
    migrate.add_argument("--to", choices=("v2",), required=True)
    migrate.add_argument("--json", action="store_true")
    _add_coordination_parser(commands)
    _add_attempt_parser(commands)
    _add_resource_parser(commands)
    _add_parallel_parser(commands)
    return parser


def _roots(arguments: CliArguments) -> tuple[Path, Path]:
    project_argument = arguments.project_root
    project = project_argument.resolve() if project_argument is not None else resolve_project_root(Path.cwd())
    work_argument = arguments.work_root
    work = work_argument.resolve() if work_argument is not None else project / ".codex" / "work"
    return project, work


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


def _status_value(work: Path, project: Path) -> StatusView:
    report = validate_work_state(work, project)
    if not report.valid:
        raise ActionError("WORK_STATE_INVALID", report.render())
    authority = resolve_authority(work)
    current_root = authority.work_root
    queue = parse_queue(current_root / "queue.md")
    current = parse_current(current_root / "current.md")
    coordinator_view: CoordinatorView | None
    match authority.version:
        case AuthorityVersion.V1:
            coordinator = read_coordinator(current_root / "coordinator.json")
            coordinator_view = CoordinatorView(coordinator.task_id, coordinator.host_id, coordinator.generation)
        case AuthorityVersion.V2:
            lease = read_coordination_lease(current_root)
            coordinator_view = (
                CoordinatorView(
                    lease.task_id,
                    lease.host_id,
                    lease.generation,
                    lease.lease_id,
                    lease.expires_at.isoformat(),
                    lease.status.value,
                )
                if lease is not None
                else None
            )
        case _ as unreachable:
            assert_never(unreachable)
    return StatusView(
        valid=True,
        project_root=str(project),
        work_root=str(work),
        revision=state_revision(work),
        focus_item=current.focus_item,
        focus_attempt=current.focus_attempt,
        active_attempts=tuple(
            item.attempt for item in queue.items if item.state.value == "active" and item.attempt is not None
        ),
        next_action=current.next_action,
        counts=dict(Counter(item.state.value for item in queue.items)),
        inbox_count=len(list((current_root / "inbox").glob("*.json"))),
        coordinator=coordinator_view,
        authority=authority.version.value,
    )


def _action_from_values(
    action_id: str,
    expected_revision: str,
    generation: int,
    subject_revision: str | None,
    authorization: str = "coordinator",
    lease_id: str | None = None,
    resource_claim_values: list[list[str]] | None = None,
) -> Action:
    if ":" not in action_id:
        raise TransitionError("ACTION_ID_INVALID", "Action identity must be 'kind:subject'.")
    kind_value, subject = action_id.split(":", 1)
    try:
        kind = ActionKind(kind_value)
        authorization_kind = AuthorizationKind(authorization)
    except ValueError as error:
        raise TransitionError("ACTION_ID_INVALID", f"Unknown action or authorization kind: {error}.") from error
    resource_claims: list[ResourceToken] = []
    for resource_id, host_id, resource_lease_id, resource_generation in resource_claim_values or []:
        try:
            parsed_generation = int(resource_generation)
        except ValueError as error:
            raise TransitionError(
                "RESOURCE_CLAIM_INVALID", f"Resource generation '{resource_generation}' is not an integer."
            ) from error
        resource_claims.append(
            ResourceToken(ResourceId(resource_id), HostId(host_id), LeaseId(resource_lease_id), parsed_generation)
        )
    attempt_kinds = {
        ActionKind.ACCEPT_CHECKPOINT,
        ActionKind.BLOCK,
        ActionKind.COMPLETE,
        ActionKind.CONTINUE,
        ActionKind.DISPATCH,
        ActionKind.PAUSE,
        ActionKind.REPORT_BLOCKER,
        ActionKind.RETURN_FOR_CORRECTION,
        ActionKind.SUBMIT_REVIEW,
    }
    proposal_kinds = {
        ActionKind.ACCEPT_PROPOSAL,
        ActionKind.MERGE_PROPOSAL,
        ActionKind.REJECT_PROPOSAL,
        ActionKind.RETURN_PROPOSAL,
    }
    subject_id: SubjectId
    if kind in attempt_kinds:
        subject_id = AttemptId(subject)
    elif kind in proposal_kinds:
        subject_id = ProposalId(subject)
    elif kind in {ActionKind.INSPECT, ActionKind.TRANSFER_COORDINATOR}:
        subject_id = LedgerId(subject)
    else:
        subject_id = ItemId(subject)
    return Action(
        action_id=ActionId(action_id),
        kind=kind,
        subject=subject_id,
        label=action_id,
        expected_revision=expected_revision,
        coordinator_generation=generation,
        subject_revision=subject_revision,
        authorization=authorization_kind,
        lease_id=LeaseId(lease_id) if lease_id is not None else None,
        resource_claims=tuple(resource_claims),
    )


def _root(context: CommandContext) -> int:
    _write_json(RootView(str(context.project), str(context.work)))
    return 0


def _validate(context: CommandContext) -> int:
    report = validate_work_state(context.work, context.project)
    if context.arguments.json:
        _write_json(_diagnostic_view(report))
    else:
        print(report.render())
    return 0 if report.valid else 10


def _status(context: CommandContext) -> int:
    value = _status_value(context.work, context.project)
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK WORK_STATE_VALID revision={value.revision}")
        print(f"focus_item={value.focus_item or 'none'} focus_attempt={value.focus_attempt or 'none'}")
        print(f"next_action={value.next_action} inbox={value.inbox_count}")
    return 0


def _overview(context: CommandContext) -> int:
    overview = read_overview(context.work, context.project)
    if context.arguments.json:
        _write_json(OverviewView.from_overview(overview))
        return 0
    print(f"OK WORK_OVERVIEW revision={overview.revision} authority={overview.authority}")
    if not overview.items:
        print("live_work=none")
    for item in overview.items:
        attempt = f" attempt={item.attempt_id}" if item.attempt_id is not None else ""
        next_action = item.next_action or "none"
        print(f"{item.item_id}\t{item.state.value}\tnext={next_action}{attempt}\t{item.label}")
    print(f"inbox={len(overview.inbox)} immediate_options={len(overview.immediate_options)}")
    return 0


def _close_action(context: CommandContext, lease: LeaseRecord | None = None) -> Action:
    actions = actions_for(
        context.work,
        context.project,
        Role.COORDINATOR,
        lease_id=lease.lease_id if lease is not None else None,
        generation=lease.generation if lease is not None else None,
    )
    action_id = f"close:{context.arguments.item_id}"
    action = next((candidate for candidate in actions if candidate.action_id == action_id), None)
    if action is None:
        raise TransitionError(
            "ACTION_NOT_AVAILABLE",
            f"Item '{context.arguments.item_id}' is not non-active live work that can be closed.",
        )
    return action


@contextlib.contextmanager
def _borrow_coordination(context: CommandContext) -> Generator[LeaseRecord]:
    candidate_lease_id = uuid4().hex
    try:
        lease = acquire_coordination(
            context.work,
            context.arguments.task_id,
            context.arguments.host_id,
            context.arguments.ttl_seconds,
            lease_id=candidate_lease_id,
        )
    except BaseException:
        try:
            current = read_coordination_lease(resolve_authority(context.work).work_root)
            if current is not None and current.lease_id == candidate_lease_id:
                with contextlib.suppress(LeaseError):
                    release_coordination(context.work, current.lease_id, current.generation)
        except LeaseError, OSError:
            pass
        raise
    try:
        yield lease
    except BaseException:
        with contextlib.suppress(LeaseError):
            release_coordination(context.work, lease.lease_id, lease.generation)
        raise
    try:
        release_coordination(context.work, lease.lease_id, lease.generation)
    except BaseException:
        with contextlib.suppress(LeaseError):
            release_coordination(context.work, lease.lease_id, lease.generation)
        raise


def _apply_close(context: CommandContext, lease: LeaseRecord | None = None) -> str:
    payload = msgspec.json.encode(
        {"outcome": context.arguments.outcome, "reason": context.arguments.reason}, order="sorted"
    )
    return apply_action(context.work, context.project, _close_action(context, lease), payload)


def _close(context: CommandContext) -> int:
    authority = resolve_authority(context.work)
    match authority.version:
        case AuthorityVersion.V1:
            revision = _apply_close(context)
        case AuthorityVersion.V2:
            if not context.arguments.task_id or not context.arguments.host_id:
                raise LeaseError(
                    "COORDINATION_IDENTITY_REQUIRED",
                    "Schema-v2 close requires --task-id and --host-id so the command can borrow coordination.",
                )
            with _borrow_coordination(context) as lease:
                revision = _apply_close(context, lease)
        case _ as unreachable:
            assert_never(unreachable)
    value = CloseView(
        context.arguments.item_id,
        context.arguments.outcome,
        context.arguments.reason,
        revision,
    )
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK WORK_ITEM_CLOSED item={value.item_id} outcome={value.outcome} revision={value.revision}")
    return 0


def _actions(context: CommandContext) -> int:
    available = actions_for(
        context.work,
        context.project,
        Role(context.arguments.role),
        lease_id=context.arguments.lease_id,
        generation=context.arguments.generation,
    )
    exact_action_id = context.arguments.action_id
    if exact_action_id is not None:
        available = tuple(action for action in available if action.action_id == exact_action_id)
        if not available:
            raise ActionError(
                "ACTION_NOT_AVAILABLE", f"Action '{exact_action_id}' is not currently legal for this role and lease."
            )
    if context.arguments.json:
        _write_json(
            ActionsView(
                tuple(
                    ActionView.from_action(action, include_input_contract=exact_action_id is not None)
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


def _input_contract(context: CommandContext) -> int:
    value = InputContractView.from_kind(context.arguments.action_kind)
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK INPUT_CONTRACT action_kind={value.action_kind}")
        sys.stdout.write(msgspec.json.format(bytes(value.payload_schema), indent=2).decode() + "\n")
    return 0


def _recover(context: CommandContext) -> int:
    with authority_transaction(context.work) as authority:
        recovered = recover_pending_commit(authority.work_root)
        report = validate_work_state(context.work, context.project)
        if not report.valid:
            raise ActionError("WORK_STATE_INVALID", report.render())
        value = RecoveryView(recovered, state_revision(context.work))
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK WORK_STATE_RECOVERED recovered={str(value.recovered).lower()} revision={value.revision}")
    return 0


def _initialize(context: CommandContext) -> int:
    task_id = context.arguments.coordinator_task_id
    host_id = context.arguments.host_id
    if task_id is None and host_id is None:
        initialized = initialize_work_state_v2(context.project, context.work)
    elif task_id is not None and host_id is not None:
        initialized = initialize_work_state(context.project, task_id, host_id, context.work)
    else:
        raise RegistrationError(
            "COORDINATOR_IDENTITY_INVALID", "Legacy v1 initialization requires both coordinator task and host."
        )
    print(f"OK WORK_STATE_INITIALIZED {initialized}")
    return 0


def _proposal(context: CommandContext) -> int:
    path = context.arguments.file
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot read proposal at '{path}': {error}") from error
    created = create_proposal(context.work, context.project, data)
    print(f"OK PROPOSAL_CREATED {created}")
    return 0


def _transition(context: CommandContext) -> int:
    payload_path = context.arguments.payload
    action = _action_from_values(
        context.arguments.action_id,
        context.arguments.expected_revision,
        context.arguments.generation,
        context.arguments.subject_revision,
        context.arguments.authorization,
        context.arguments.lease_id,
        context.arguments.resource_claim,
    )
    try:
        payload = payload_path.read_bytes()
    except OSError as error:
        raise TransitionError("TRANSITION_INPUT_INVALID", f"Cannot read transition payload: {error}") from error
    revision = apply_action(context.work, context.project, action, payload)
    print(f"OK TRANSITION_APPLIED {action.action_id} revision={revision}")
    return 0


def _prepare_dispatch(context: CommandContext) -> int:
    environment = read_dispatch_environment(context.arguments.environment)
    supplied_prompt: bytes | None = None
    if context.arguments.prompt is not None:
        try:
            supplied_prompt = context.arguments.prompt.read_bytes()
        except OSError as error:
            raise DispatchError(
                "DISPATCH_PROMPT_UNREADABLE",
                f"Cannot read '{context.arguments.prompt}': {error}",
            ) from error
    brief_review: bytes | None = None
    if context.arguments.brief_review is not None:
        try:
            brief_review = context.arguments.brief_review.read_bytes()
        except OSError as error:
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_INVALID",
                f"Cannot read '{context.arguments.brief_review}': {error}",
            ) from error
    prompt = prepare_dispatch(
        context.work,
        context.project,
        _action_from_values(
            context.arguments.action_id,
            context.arguments.expected_revision,
            context.arguments.generation,
            None,
            "coordination" if context.arguments.lease_id is not None else "coordinator",
            context.arguments.lease_id,
        ),
        context.arguments.checkpoint,
        environment,
        supplied_prompt,
        brief_review,
        context.arguments.review_id,
    )
    if supplied_prompt is None:
        sys.stdout.write(prompt)
    else:
        print("OK DISPATCH_READY")
    return 0


type OperationRecord = LeaseRecord | ResourceClaim | ResourceDeclaration


def _lease_value(record: OperationRecord) -> dict[str, str | int]:
    match record:
        case ResourceDeclaration():
            return {"resource_id": record.resource_id, "label": record.label, "scope": record.scope.value}
        case ResourceClaim():
            return {
                "task_id": record.task_id,
                "host_id": record.host_id,
                "lease_id": record.lease_id,
                "generation": record.generation,
                "acquired_at": record.acquired_at.isoformat(),
                "expires_at": record.expires_at.isoformat(),
                "status": record.status.value,
                "resource_id": record.resource_id,
                "attempt_id": record.attempt_id,
                "attempt_lease_id": record.attempt_lease_id,
                "attempt_lease_generation": record.attempt_lease_generation,
            }
        case LeaseRecord():
            values: dict[str, str | int] = {
                "task_id": record.task_id,
                "host_id": record.host_id,
                "lease_id": record.lease_id,
                "generation": record.generation,
                "acquired_at": record.acquired_at.isoformat(),
                "expires_at": record.expires_at.isoformat(),
                "status": record.status.value,
            }
            if record.attempt_id is not None:
                values["attempt_id"] = record.attempt_id
            return values
        case _ as unreachable:
            assert_never(unreachable)


def _emit_operation(value: OperationRecord, as_json: bool) -> int:
    if as_json:
        _write_json(_lease_value(value))
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in _lease_value(value).items()))
    return 0


def _migrate(context: CommandContext) -> int:
    result = migrate_to_v2(context.work, context.project)
    value = {
        "live_items": result.live_items,
        "attempts": result.attempts,
        "proposals": result.proposals,
        "history_items": result.history_items,
        "cutover": result.cutover,
    }
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK MIGRATION_V2 cutover={str(result.cutover).lower()} live_items={result.live_items}")
    return 0


def _lease_command_root(work_root: Path) -> Path:
    authority = resolve_authority(work_root)
    if authority.version != AuthorityVersion.V2:
        raise MigrationError(
            "MIGRATION_REQUIRED",
            "Lease and resource commands require schema v2; run 'pinboard migrate --to v2' first.",
        )
    return authority.work_root


def _coordinated_transition(context: CommandContext) -> CoordinatedTransitionView:
    payload_path = context.arguments.payload
    try:
        payload = payload_path.read_bytes()
    except OSError as error:
        raise TransitionError(
            "TRANSITION_INPUT_INVALID", f"Cannot read transition payload at '{payload_path}': {error}"
        ) from error
    kind_value, separator, _ = context.arguments.action_id.partition(":")
    if not separator:
        raise TransitionError("ACTION_ID_INVALID", "Action identity must be 'kind:subject'.")
    parse_legacy_transition_input(kind_value, payload)
    with _borrow_coordination(context) as lease:
        available = actions_for(
            context.work,
            context.project,
            Role.COORDINATOR,
            lease_id=lease.lease_id,
            generation=lease.generation,
        )
        action = next(
            (candidate for candidate in available if candidate.action_id == context.arguments.action_id), None
        )
        if action is None:
            raise TransitionError(
                "ACTION_NOT_AVAILABLE", f"Action '{context.arguments.action_id}' is not currently legal."
            )
        revision = apply_action(context.work, context.project, action, payload)
    return CoordinatedTransitionView(context.arguments.action_id, revision)


def _coordination(context: CommandContext) -> int:
    root = _lease_command_root(context.work)
    operation = CoordinationOperation(context.arguments.operation)
    match operation:
        case CoordinationOperation.APPLY:
            value = _coordinated_transition(context)
            if context.arguments.json:
                _write_json(value)
            else:
                print(f"OK COORDINATED_TRANSITION action={value.action_id} revision={value.revision}")
            return 0
        case CoordinationOperation.ACQUIRE:
            value = acquire_coordination(
                context.work, context.arguments.task_id, context.arguments.host_id, context.arguments.ttl_seconds
            )
        case CoordinationOperation.RENEW:
            value = renew_coordination(
                context.work,
                context.arguments.lease_id or "",
                context.arguments.generation,
                context.arguments.ttl_seconds,
            )
        case CoordinationOperation.RELEASE:
            value = release_coordination(context.work, context.arguments.lease_id or "", context.arguments.generation)
        case CoordinationOperation.REVOKE:
            value = revoke_coordination(context.work)
        case CoordinationOperation.STATUS:
            current = read_coordination_lease(root)
            if current is None:
                if context.arguments.json:
                    _write_json({"lease": None})
                else:
                    print("OK COORDINATION_AVAILABLE")
                return 0
            value = current
        case _ as unreachable:
            assert_never(unreachable)
    return _emit_operation(value, context.arguments.json)


def _attempt(context: CommandContext) -> int:
    root = _lease_command_root(context.work)
    operation = AttemptOperation(context.arguments.operation)
    match operation:
        case AttemptOperation.ACQUIRE:
            value = acquire_attempt(
                context.work,
                context.arguments.attempt_id,
                context.arguments.task_id,
                context.arguments.host_id,
                context.arguments.ttl_seconds,
            )
        case AttemptOperation.RENEW:
            value = renew_attempt(
                context.work,
                context.arguments.attempt_id,
                context.arguments.lease_id or "",
                context.arguments.generation,
                context.arguments.ttl_seconds,
            )
        case AttemptOperation.RELEASE:
            value = release_attempt(
                context.work,
                context.arguments.attempt_id,
                context.arguments.lease_id or "",
                context.arguments.generation,
            )
        case AttemptOperation.REVOKE:
            value = revoke_attempt(
                context.work,
                context.arguments.attempt_id,
                context.arguments.coordination_lease_id,
                context.arguments.coordination_generation,
            )
        case AttemptOperation.STATUS:
            value = read_attempt_lease(root, context.arguments.attempt_id)
        case _ as unreachable:
            assert_never(unreachable)
    return _emit_operation(value, context.arguments.json)


def _resource(context: CommandContext) -> int:
    root = _lease_command_root(context.work)
    operation = ResourceOperation(context.arguments.operation)
    match operation:
        case ResourceOperation.DECLARE:
            value = declare_resource(
                context.work,
                context.arguments.resource_id,
                context.arguments.label,
                context.arguments.coordination_lease_id,
                context.arguments.coordination_generation,
                scope=ResourceScope(context.arguments.scope),
            )
        case ResourceOperation.CLAIM:
            value = claim_resource(
                context.work,
                context.arguments.resource_id,
                context.arguments.attempt_id,
                context.arguments.task_id,
                context.arguments.host_id,
                context.arguments.ttl_seconds,
                context.arguments.attempt_lease_id,
                context.arguments.attempt_generation,
            )
        case ResourceOperation.RENEW:
            value = renew_resource(
                context.work,
                context.arguments.resource_id,
                context.arguments.host_id,
                context.arguments.lease_id or "",
                context.arguments.generation,
                context.arguments.ttl_seconds,
            )
        case ResourceOperation.RELEASE:
            value = release_resource(
                context.work,
                context.arguments.resource_id,
                context.arguments.host_id,
                context.arguments.lease_id or "",
                context.arguments.generation,
            )
        case ResourceOperation.REVOKE:
            value = revoke_resource(
                context.work,
                context.arguments.resource_id,
                context.arguments.host_id,
                context.arguments.coordination_lease_id,
                context.arguments.coordination_generation,
            )
        case ResourceOperation.STATUS:
            value = (
                read_resource(root, context.arguments.resource_id)
                if context.arguments.host_id is None
                else read_resource_claim(root, context.arguments.resource_id, context.arguments.host_id)
            )
        case _ as unreachable:
            assert_never(unreachable)
    return _emit_operation(value, context.arguments.json)


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


def _parallel(context: CommandContext) -> int:
    operation = ParallelOperation(context.arguments.operation)
    match operation:
        case ParallelOperation.PREVIEW:
            preview = preview_parallel(
                context.work,
                context.project,
                context.arguments.host_id,
                selected=tuple(context.arguments.item),
            )
        case _ as unreachable:
            assert_never(unreachable)
    if context.arguments.json:
        _write_json(ParallelPreviewView.from_preview(preview))
    else:
        print(
            f"OK PARALLEL_PREVIEW revision={preview.revision} selection={preview.selection.value} "
            f"safe={'yes' if preview.safe else 'no'}"
        )
        _print_parallel_group("Ready to launch together", preview.launchable)
        _print_parallel_group("Needs explicit selection", preview.requires_selection)
        _print_parallel_group("Not launchable", preview.excluded)
    return 0


def _dispatch(arguments: CliArguments) -> int:  # noqa: C901, PLR0912 - exhaustive closed command dispatch
    project, work = _roots(arguments)
    context = CommandContext(arguments, project, work)
    match CommandName(arguments.command):
        case CommandName.ROOT:
            return _root(context)
        case CommandName.VALIDATE:
            return _validate(context)
        case CommandName.STATUS:
            return _status(context)
        case CommandName.OVERVIEW:
            return _overview(context)
        case CommandName.CLOSE:
            return _close(context)
        case CommandName.ACTIONS:
            return _actions(context)
        case CommandName.INPUT_CONTRACT:
            return _input_contract(context)
        case CommandName.RECOVER:
            return _recover(context)
        case CommandName.INIT:
            return _initialize(context)
        case CommandName.PROPOSAL:
            return _proposal(context)
        case CommandName.TRANSITION:
            return _transition(context)
        case CommandName.DISPATCH:
            return _prepare_dispatch(context)
        case CommandName.MIGRATE:
            return _migrate(context)
        case CommandName.COORDINATION:
            return _coordination(context)
        case CommandName.ATTEMPT:
            return _attempt(context)
        case CommandName.RESOURCE:
            return _resource(context)
        case CommandName.PARALLEL:
            return _parallel(context)
        case _ as unreachable:
            assert_never(unreachable)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv, namespace=CliArguments())
    try:
        return _dispatch(arguments)
    except (RootError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (
        ActionError,
        TransitionError,
        TransitionInputError,
        LeaseError,
        ResourceError,
        MigrationError,
        ParallelError,
        OverviewError,
        AtomicCommitError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 11
    except RegistrationError as error:
        print(str(error), file=sys.stderr)
        return 12
    except ProposalError as error:
        print(str(error), file=sys.stderr)
        return 2 if error.code == "PROPOSAL_INVALID" else 13
    except DispatchError as error:
        print(str(error), file=sys.stderr)
        return 14
