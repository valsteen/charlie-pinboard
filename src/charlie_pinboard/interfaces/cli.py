import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import assert_never
from uuid import uuid4

import msgspec

from charlie_pinboard import __version__
from charlie_pinboard.adapters.files.artifacts import ArtifactError, ArtifactRepository
from charlie_pinboard.adapters.files.file_io import FileIOError, resolve_durable_roots
from charlie_pinboard.adapters.files.root import RootError, resolve_project_root
from charlie_pinboard.adapters.files.views import AffectedViews
from charlie_pinboard.adapters.files.views import rebuild as rebuild_views
from charlie_pinboard.adapters.files.views import refresh as refresh_views
from charlie_pinboard.adapters.sqlite.database import StorageError
from charlie_pinboard.adapters.sqlite.registration import initialize_work_state
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import ActionQueryError, discover_actions
from charlie_pinboard.application.decision_projection import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from charlie_pinboard.application.dispatch import DispatchError, prepare_sqlite_dispatch
from charlie_pinboard.application.queries import (
    DetailLevel,
    QueryError,
    overview_from_state,
    read_resource_conflict,
)
from charlie_pinboard.application.queries import (
    OverviewItem as SQLiteOverviewItem,
)
from charlie_pinboard.application.queries import (
    ParallelItem as SQLiteParallelItem,
)
from charlie_pinboard.application.queries import (
    ParallelPreview as SQLiteParallelPreview,
)
from charlie_pinboard.application.queries import (
    WorkOverview as SQLiteWorkOverview,
)
from charlie_pinboard.application.queries import (
    preview_parallel as preview_sqlite_parallel,
)
from charlie_pinboard.application.queries import (
    read_overview as read_sqlite_overview,
)
from charlie_pinboard.application.registration import InitializationError
from charlie_pinboard.application.service import (
    change_attempt_authority,
    change_coordination_authority,
)
from charlie_pinboard.application.service import create_proposal as create_sqlite_proposal
from charlie_pinboard.application.service import (
    execute as execute_sqlite_transition,
)
from charlie_pinboard.application.stored_state import ResourceInstanceState
from charlie_pinboard.application.validation import ValidationReport, validate_sqlite_work_state
from charlie_pinboard.domain.authority_decisions import (
    AcquireCoordinationAuthority,
    AcquireInitialAttemptAuthority,
    ReleaseAttemptAuthority,
    ReleaseCoordinationAuthority,
    RenewAttemptAuthority,
    RenewCoordinationAuthority,
    RevokeAttemptAuthority,
    RevokeCoordinationAuthority,
    TransferAttemptAuthority,
)
from charlie_pinboard.domain.decisions import Action, ActionKind, AuthorizationKind, Role, bind_transition
from charlie_pinboard.domain.errors import DecisionFailure
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
    TaskId,
)
from charlie_pinboard.domain.model import CoordinationCommandAuthority, ProposalRelationKind
from charlie_pinboard.domain.proposal_decisions import CreateProposalOperation, ProposalIntake
from charlie_pinboard.domain.resource_decisions import ResourceToken
from charlie_pinboard.identity import PROGRAM_NAME
from charlie_pinboard.interfaces.dispatch_brief import prepare_dispatch_from_artifact, read_dispatch_environment
from charlie_pinboard.interfaces.errors import ActionError, LeaseError, ProposalError, ResourceError, TransitionError
from charlie_pinboard.interfaces.proposals import parse_proposal
from charlie_pinboard.interfaces.transition_input import (
    TRANSITION_ACTION_KINDS,
    CloseOutcome,
    TransitionInputError,
    encoded_transition_input_schema,
    parse_transition_input,
)


class CommandName(Enum):
    ROOT = "root"
    VALIDATE = "validate"
    STATUS = "status"
    OVERVIEW = "overview"
    CLOSE = "close"
    ACTIONS = "actions"
    INPUT_CONTRACT = "input-contract"
    INIT = "init"
    PROPOSAL = "proposal"
    TRANSITION = "transition"
    DISPATCH = "dispatch"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    RESOURCE = "resource"
    PARALLEL = "parallel"
    VIEWS = "views"


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


class ViewsOperation(Enum):
    REBUILD = "rebuild"


class CliArguments(argparse.Namespace):
    command: str
    project_root: Path | None
    work_root: Path | None
    json: bool
    role: str
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
    detail: str


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
    lease_id: str
    expires_at: str
    status: str


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
    def from_item(cls, item: SQLiteOverviewItem) -> OverviewItemView:
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
    def from_overview(cls, overview: SQLiteWorkOverview) -> OverviewView:
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
        return cls(kind, msgspec.Raw(encoded_transition_input_schema(kind)))


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
    def from_action(
        cls,
        action: Action,
        *,
        include_input_contract: bool = False,
    ) -> ActionView:
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
    def from_item(cls, item: SQLiteParallelItem) -> ParallelItemView:
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
    def from_preview(cls, preview: SQLiteParallelPreview) -> ParallelPreviewView:
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
    acquire.add_argument("--coordination-lease-id")
    acquire.add_argument("--coordination-generation", type=int)
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
    revoke.add_argument("--lease-id", required=True)
    revoke.add_argument("--generation", required=True, type=int)
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
    status.add_argument(
        "--detail", choices=tuple(value.value for value in DetailLevel), default=DetailLevel.COMPACT.value
    )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description="Inspect and transition one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    _add_inspection_parsers(commands)
    commands.add_parser("init", help="Create an empty current SQLite work state.")
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
    dispatch.add_argument("--lease-id", help="Current coordination lease identity.")
    dispatch.add_argument(
        "--checkpoint", required=True, help="Exact checkpoint heading in the canonical attempt brief."
    )
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
    _add_coordination_parser(commands)
    _add_attempt_parser(commands)
    _add_resource_parser(commands)
    _add_parallel_parser(commands)
    views = commands.add_parser("views", help="Repair generated human-readable views.")
    views.add_subparsers(dest="operation", required=True).add_parser("rebuild")
    return parser


def _roots(arguments: CliArguments) -> tuple[Path, Path]:
    project_argument = arguments.project_root
    project = project_argument.resolve() if project_argument is not None else resolve_project_root(Path.cwd())
    work_argument = arguments.work_root
    work = work_argument.resolve() if work_argument is not None else project / ".codex" / "work"
    return project, work


def _stable_identifier(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\x00" in value:
        raise LeaseError("IDENTITY_INVALID", f"{label} must be one stable opaque identity.")
    return value


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
    state = SQLiteWorkStore(work / "state.sqlite3").snapshot()
    overview = overview_from_state(state)
    coordinator = state.authority.coordination
    return StatusView(
        valid=True,
        project_root=str(project),
        work_root=str(work),
        revision=str(state.lifecycle.project.revision),
        focus_item=overview.focus_item,
        focus_attempt=overview.focus_attempt,
        active_attempts=overview.active_attempts,
        next_action=state.focus.next_action,
        counts=dict(Counter(item.state.value for item in state.lifecycle.work_items)),
        inbox_count=len(overview.inbox),
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


def _reselect_sqlite_action(context: CommandContext, supplied: Action, role: Role) -> Action:
    try:
        available = discover_actions(
            SQLiteWorkStore(context.work / "state.sqlite3"),
            role,
            lease_id=supplied.lease_id,
            generation=supplied.coordinator_generation,
        )
    except ActionQueryError as error:
        raise ActionError(error.code, str(error).partition(": ")[2]) from error
    current = next((value for value in available if value.action_id == supplied.action_id), None)
    if current is None:
        raise ActionError("ACTION_NOT_AVAILABLE", f"Action '{supplied.action_id}' is not currently legal.")
    if current.expected_revision != supplied.expected_revision:
        raise ActionError("STALE_ACTION", "The work ledger changed after this action was selected.")
    supplied_capability = (
        supplied.kind,
        supplied.subject,
        supplied.coordinator_generation,
        supplied.subject_revision,
        supplied.authorization,
        supplied.lease_id,
        supplied.resource_claims,
    )
    current_capability = (
        current.kind,
        current.subject,
        current.coordinator_generation,
        current.subject_revision,
        current.authorization,
        current.lease_id,
        current.resource_claims,
    )
    if current_capability != supplied_capability:
        raise ActionError(
            "ACTION_NOT_AVAILABLE", f"Action '{supplied.action_id}' no longer has exact current authority."
        )
    return current


def _root(context: CommandContext) -> int:
    _write_json(RootView(str(context.project), str(context.work)))
    return 0


def _validate(context: CommandContext) -> int:
    report = validate_sqlite_work_state(context.work)
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
    overview = read_sqlite_overview(SQLiteWorkStore(context.work / "state.sqlite3"))
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


def _close(context: CommandContext) -> int:
    if not context.arguments.task_id or not context.arguments.host_id:
        raise LeaseError(
            "COORDINATION_IDENTITY_REQUIRED",
            "Close requires --task-id and --host-id so the command can borrow coordination.",
        )
    payload = msgspec.json.encode(
        {"outcome": context.arguments.outcome, "reason": context.arguments.reason}, order="sorted"
    )
    revision = _execute_borrowed_coordination(
        context,
        f"close:{context.arguments.item_id}",
        payload,
    )
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
    available = discover_actions(
        SQLiteWorkStore(context.work / "state.sqlite3"),
        Role(context.arguments.role),
        lease_id=LeaseId(context.arguments.lease_id) if context.arguments.lease_id is not None else None,
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
                    ActionView.from_action(
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


def _input_contract(context: CommandContext) -> int:
    value = InputContractView.from_kind(context.arguments.action_kind)
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK INPUT_CONTRACT action_kind={value.action_kind}")
        sys.stdout.write(msgspec.json.format(bytes(value.payload_schema), indent=2).decode() + "\n")
    return 0


def _initialize(context: CommandContext) -> int:
    selected_work = context.work if context.arguments.work_root is not None else None
    receipt = initialize_work_state(context.project, selected_work)
    initialized = receipt.work_root
    print(f"OK WORK_STATE_INITIALIZED {initialized}")
    return 0


def _proposal(context: CommandContext) -> int:
    path = context.arguments.file
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot read proposal at '{path}': {error}") from error
    proposal = parse_proposal(data)
    try:
        created_at = datetime.fromisoformat(proposal.created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProposalError("PROPOSAL_INVALID", "Proposal created_at must be an ISO timestamp.") from error
    if created_at.tzinfo is None:
        if len(proposal.created_at) == 10:
            created_at = created_at.replace(tzinfo=UTC)
        else:
            raise ProposalError("PROPOSAL_INVALID", "Proposal created_at must include a timezone.")
    intake = ProposalIntake(
        ProposalId(proposal.proposal_id),
        created_at.astimezone(UTC),
        TaskId(proposal.source_task_id),
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        proposal.effect,
        proposal.unlock,
        ProposalRelationKind(proposal.relation.kind.value),
        None if proposal.relation.item is None else ItemId(proposal.relation.item),
        proposal.urgency_evidence,
        proposal.evidence,
        proposal.freshness_assumptions,
    )
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    result = create_sqlite_proposal(store, CreateProposalOperation(intake), datetime.now(UTC))
    if isinstance(result, DecisionFailure):
        raise ProposalError(result.code.value, result.message)
    view_result = refresh_views(store, context.work, AffectedViews(queue=True, history=True))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    print(f"OK PROPOSAL_CREATED {proposal.proposal_id}")
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
    role = Role.WORKER if action.authorization == AuthorizationKind.ATTEMPT else Role.COORDINATOR
    action = _reselect_sqlite_action(context, action, role)
    parsed = parse_transition_input(action.kind.value, payload)
    command = bind_transition(action, parsed)
    if isinstance(command, DecisionFailure):
        raise TransitionError(command.code.value, command.message)
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    result = execute_sqlite_transition(store, command, datetime.now(UTC))
    if isinstance(result, DecisionFailure):
        raise TransitionError(result.code.value, result.message)
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
    view_result = refresh_views(store, context.work, affected)
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    revision = str(state.lifecycle.project.revision)
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
    action = _action_from_values(
        context.arguments.action_id,
        context.arguments.expected_revision,
        context.arguments.generation,
        None,
        "coordination" if context.arguments.lease_id is not None else "coordinator",
        context.arguments.lease_id,
    )
    action = _reselect_sqlite_action(context, action, Role.COORDINATOR)
    prompt = prepare_sqlite_dispatch(
        SQLiteWorkStore(context.work / "state.sqlite3"),
        ArtifactRepository(resolve_durable_roots(context.project, context.work)),
        prepare_dispatch_from_artifact,
        context.project,
        action,
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


def _coordination(context: CommandContext) -> int:
    return _sqlite_coordination(context)


def _sqlite_coordination_values(context: CommandContext) -> dict[str, str | int] | None:
    state = SQLiteWorkStore(context.work / "state.sqlite3").snapshot()
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


def _emit_sqlite_coordination(context: CommandContext) -> int:
    values = _sqlite_coordination_values(context)
    if context.arguments.json:
        _write_json({"lease": None} if values is None else values)
    elif values is None:
        print("OK COORDINATION_AVAILABLE")
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _current_coordination_token(
    context: CommandContext,
    *,
    supplied_identity: bool = False,
) -> CoordinationCommandAuthority:
    state = SQLiteWorkStore(context.work / "state.sqlite3").snapshot()
    current = state.authority.coordination
    if current is None:
        raise LeaseError("COORDINATION_LEASE_REQUIRED", "Coordination authority does not exist.")
    return CoordinationCommandAuthority(
        state.lifecycle.project.host_epoch,
        current.task_id,
        current.host_id,
        LeaseId(context.arguments.lease_id or "") if supplied_identity else current.lease_id,
        context.arguments.generation if supplied_identity else current.generation,
        current.expires_at,
    )


def _sqlite_coordination(context: CommandContext) -> int:
    operation = CoordinationOperation(context.arguments.operation)
    if operation == CoordinationOperation.STATUS:
        return _emit_sqlite_coordination(context)
    if operation == CoordinationOperation.APPLY:
        return _sqlite_coordinated_transition(context)
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    state = store.snapshot()
    now = datetime.now(UTC)
    match operation:
        case CoordinationOperation.ACQUIRE:
            authority_operation = AcquireCoordinationAuthority(
                state.lifecycle.project.host_epoch,
                TaskId(_stable_identifier(context.arguments.task_id, label="Task identity")),
                HostId(_stable_identifier(context.arguments.host_id, label="Host identity")),
                LeaseId(uuid4().hex),
                now,
                now + timedelta(seconds=context.arguments.ttl_seconds),
            )
        case CoordinationOperation.RENEW:
            authority_operation = RenewCoordinationAuthority(
                _current_coordination_token(context, supplied_identity=True),
                now,
                now + timedelta(seconds=context.arguments.ttl_seconds),
            )
        case CoordinationOperation.RELEASE:
            authority_operation = ReleaseCoordinationAuthority(
                _current_coordination_token(context, supplied_identity=True), now
            )
        case CoordinationOperation.REVOKE:
            current = _current_coordination_token(context)
            authority_operation = RevokeCoordinationAuthority(current.lease_id, current.generation, now)
        case _ as unreachable:
            assert_never(unreachable)
    result = change_coordination_authority(store, authority_operation)
    if isinstance(result, DecisionFailure):
        raise LeaseError(result.code.value, result.message)
    view_result = refresh_views(store, context.work, AffectedViews(queue=True, current_focus=True, history=True))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    return _emit_sqlite_coordination(context)


def _sqlite_coordinated_transition(context: CommandContext) -> int:
    payload_path = context.arguments.payload
    try:
        payload = payload_path.read_bytes()
    except OSError as error:
        raise TransitionError("TRANSITION_INPUT_INVALID", f"Cannot read transition payload: {error}") from error
    transition_revision = _execute_borrowed_coordination(context, context.arguments.action_id, payload)
    value = CoordinatedTransitionView(context.arguments.action_id, transition_revision)
    if context.arguments.json:
        _write_json(value)
    else:
        print(f"OK COORDINATED_TRANSITION action={value.action_id} revision={value.revision}")
    return 0


def _execute_borrowed_coordination(context: CommandContext, action_id: str, payload: bytes) -> str:
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    now = datetime.now(UTC)
    state = store.snapshot()
    acquire = AcquireCoordinationAuthority(
        state.lifecycle.project.host_epoch,
        TaskId(_stable_identifier(context.arguments.task_id, label="Task identity")),
        HostId(_stable_identifier(context.arguments.host_id, label="Host identity")),
        LeaseId(uuid4().hex),
        now,
        now + timedelta(seconds=context.arguments.ttl_seconds),
    )
    acquired = change_coordination_authority(store, acquire)
    if isinstance(acquired, DecisionFailure):
        raise LeaseError(acquired.code.value, acquired.message)
    transition_revision: str | None = None
    try:
        current_state = store.snapshot()
        coordination = current_state.authority.coordination
        assert coordination is not None
        available = discover_actions(
            store,
            Role.COORDINATOR,
            lease_id=coordination.lease_id,
            generation=coordination.generation,
        )
        action = next(
            (candidate for candidate in available if candidate.action_id == action_id),
            None,
        )
        if action is None:
            raise TransitionError("ACTION_NOT_AVAILABLE", f"Action '{action_id}' is not currently legal.")
        parsed = parse_transition_input(action.kind.value, payload)
        command = bind_transition(action, parsed)
        if isinstance(command, DecisionFailure):
            raise TransitionError(command.code.value, command.message)
        result = execute_sqlite_transition(store, command, datetime.now(UTC))
        if isinstance(result, DecisionFailure):
            raise TransitionError(result.code.value, result.message)
        transition_revision = str(store.snapshot().lifecycle.project.revision)
    finally:
        current = store.snapshot().authority.coordination
        if current is not None and current.lease_id == acquire.lease_id:
            released = change_coordination_authority(
                store,
                ReleaseCoordinationAuthority(
                    CoordinationCommandAuthority(
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
                raise LeaseError(released.code.value, released.message)
    view_result = rebuild_views(store, context.work)
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    assert transition_revision is not None
    return transition_revision


def _attempt(context: CommandContext) -> int:
    return _sqlite_attempt(context)


def _sqlite_attempt(context: CommandContext) -> int:  # noqa: C901, PLR0912, PLR0915 - closed authority lifecycle
    attempt_id = AttemptId(_stable_identifier(context.arguments.attempt_id, label="Attempt identity"))
    operation = AttemptOperation(context.arguments.operation)
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    state = store.snapshot()
    if operation != AttemptOperation.STATUS:
        now = datetime.now(UTC)
        snapshot = project_decision_snapshot(state)
        attempt = next((value for value in state.lifecycle.attempts if value.attempt_id == attempt_id), None)
        if attempt is None:
            raise LeaseError("ATTEMPT_LEASE_REQUIRED", f"Attempt '{attempt_id}' is not current.")
        retained = next((value for value in snapshot.command_attempt_authorities if value.attempt == attempt_id), None)
        retained_record = next(
            (value for value in state.authority.attempt_leases if value.attempt_id == attempt_id),
            None,
        )
        match operation:
            case AttemptOperation.ACQUIRE:
                task_id = TaskId(_stable_identifier(context.arguments.task_id, label="Task identity"))
                host_id = HostId(_stable_identifier(context.arguments.host_id, label="Host identity"))
                lease_id = LeaseId(uuid4().hex)
                if retained_record is None:
                    authority_operation = AcquireInitialAttemptAuthority(
                        state.lifecycle.project.host_epoch,
                        attempt_id,
                        attempt.item_id,
                        task_id,
                        host_id,
                        lease_id,
                        now,
                        now + timedelta(seconds=context.arguments.ttl_seconds),
                    )
                else:
                    inactive = project_inactive_attempt_authority(state, attempt_id, now)
                    if isinstance(inactive, DecisionFailure):
                        raise LeaseError(inactive.code.value, inactive.message)
                    coordination = state.authority.coordination
                    if coordination is None:
                        raise LeaseError("COORDINATION_LEASE_REQUIRED", "Attempt reacquisition requires coordination.")
                    if (
                        context.arguments.coordination_lease_id is None
                        or context.arguments.coordination_generation is None
                    ):
                        raise LeaseError(
                            "COORDINATION_LEASE_REQUIRED",
                            "Attempt reacquisition requires the exact coordination lease and generation.",
                        )
                    authority_operation = TransferAttemptAuthority(
                        inactive,
                        CoordinationCommandAuthority(
                            state.lifecycle.project.host_epoch,
                            coordination.task_id,
                            coordination.host_id,
                            LeaseId(context.arguments.coordination_lease_id),
                            context.arguments.coordination_generation,
                            coordination.expires_at,
                        ),
                        task_id,
                        host_id,
                        lease_id,
                        now,
                        now + timedelta(seconds=context.arguments.ttl_seconds),
                    )
            case AttemptOperation.RENEW:
                if retained is None:
                    raise LeaseError("ATTEMPT_LEASE_REQUIRED", "Attempt authority is not active.")
                authority_operation = RenewAttemptAuthority(
                    replace(
                        retained,
                        lease_id=LeaseId(context.arguments.lease_id or ""),
                        generation=context.arguments.generation,
                    ),
                    now,
                    now + timedelta(seconds=context.arguments.ttl_seconds),
                )
            case AttemptOperation.RELEASE:
                if retained is None:
                    raise LeaseError("ATTEMPT_LEASE_REQUIRED", "Attempt authority is not active.")
                authority_operation = ReleaseAttemptAuthority(
                    replace(
                        retained,
                        lease_id=LeaseId(context.arguments.lease_id or ""),
                        generation=context.arguments.generation,
                    ),
                    now,
                )
            case AttemptOperation.REVOKE:
                coordination = state.authority.coordination
                if coordination is None:
                    raise LeaseError("COORDINATION_LEASE_REQUIRED", "Coordination authority is absent.")
                authority_operation = RevokeAttemptAuthority(
                    attempt_id,
                    LeaseId(context.arguments.lease_id or ""),
                    context.arguments.generation,
                    CoordinationCommandAuthority(
                        state.lifecycle.project.host_epoch,
                        coordination.task_id,
                        coordination.host_id,
                        LeaseId(context.arguments.coordination_lease_id),
                        context.arguments.coordination_generation,
                        coordination.expires_at,
                    ),
                    now,
                )
            case _ as unreachable:
                assert_never(unreachable)
        result = change_attempt_authority(store, authority_operation)
        if isinstance(result, DecisionFailure):
            raise LeaseError(result.code.value, result.message)
        refresh_result = refresh_views(store, context.work, AffectedViews(queue=True, current_focus=True, history=True))
        if refresh_result.warning is not None:
            print(refresh_result.warning.message, file=sys.stderr)
        state = store.snapshot()
    lease = next((value for value in state.authority.attempt_leases if value.attempt_id == attempt_id), None)
    if lease is None:
        raise LeaseError("ATTEMPT_LEASE_REQUIRED", f"Attempt '{attempt_id}' has no retained authority.")
    anchor = next(
        (
            value
            for value in state.authority.attempt_generations
            if value.attempt_id == attempt_id and value.generation == lease.generation
        ),
        None,
    )
    if anchor is None:
        raise LeaseError("WORK_STATE_INVALID", "Attempt authority has no exact identity anchor.")
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
    if context.arguments.json:
        _write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _resource(context: CommandContext) -> int:
    return _sqlite_resource(context)


def _sqlite_resource(context: CommandContext) -> int:
    operation = ResourceOperation(context.arguments.operation)
    if operation != ResourceOperation.STATUS:
        raise ResourceError("ACTION_NOT_AVAILABLE", "SQLite resource changes require a current typed operation.")
    resource_id = ResourceId(_stable_identifier(context.arguments.resource_id, label="Resource identity"))
    if context.arguments.host_id is not None:
        _stable_identifier(context.arguments.host_id, label="Host identity")
    store = SQLiteWorkStore(context.work / "state.sqlite3")
    state = store.snapshot()
    definition = next((value for value in state.resources.definitions if value.resource_id == resource_id), None)
    if definition is None:
        raise ResourceError("RESOURCE_INSTANCE_REQUIRED", f"Resource '{resource_id}' is not defined.")
    values: dict[str, str | int] = {
        "resource_id": str(resource_id),
        "label": definition.description,
        "scope": "host-local",
    }
    if context.arguments.host_id is not None:
        instance = next(
            (
                value
                for value in state.resources.instances
                if value.resource_id == resource_id
                and value.host_id == context.arguments.host_id
                and value.state == ResourceInstanceState.ACTIVE
            ),
            None,
        )
        if instance is None:
            raise ResourceError("RESOURCE_INSTANCE_REQUIRED", f"Resource '{resource_id}' has no instance on this host.")
        conflict = read_resource_conflict(
            store,
            instance.instance_id,
            DetailLevel(context.arguments.detail),
        )
        if context.arguments.json:
            _write_json(conflict)
        else:
            print(
                f"OK RESOURCE_CONFLICT detail={conflict.detail} instance_id={conflict.instance_id} "
                f"consequence={conflict.consequence}"
            )
        return 0
    if context.arguments.detail == DetailLevel.DETAILED.value:
        raise ResourceError(
            "RESOURCE_INSTANCE_REQUIRED", "Detailed resource status requires an explicit host instance."
        )
    if context.arguments.json:
        _write_json(values)
    else:
        print("OK " + " ".join(f"{key}={value}" for key, value in values.items()))
    return 0


def _print_parallel_group(title: str, items: tuple[SQLiteParallelItem, ...]) -> None:
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
            preview = preview_sqlite_parallel(
                SQLiteWorkStore(context.work / "state.sqlite3"),
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


def _views(context: CommandContext) -> int:
    operation = ViewsOperation(context.arguments.operation)
    match operation:
        case ViewsOperation.REBUILD:
            result = rebuild_views(SQLiteWorkStore(context.work / "state.sqlite3"), context.work)
    if result.warning is not None:
        raise InitializationError(result.warning.code, result.warning.message)
    print(f"OK VIEWS_REBUILT revision={result.database_revision}")
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
        case CommandName.INIT:
            return _initialize(context)
        case CommandName.PROPOSAL:
            return _proposal(context)
        case CommandName.TRANSITION:
            return _transition(context)
        case CommandName.DISPATCH:
            return _prepare_dispatch(context)
        case CommandName.COORDINATION:
            return _coordination(context)
        case CommandName.ATTEMPT:
            return _attempt(context)
        case CommandName.RESOURCE:
            return _resource(context)
        case CommandName.PARALLEL:
            return _parallel(context)
        case CommandName.VIEWS:
            return _views(context)
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
        ActionQueryError,
        QueryError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 11
    except (InitializationError, StorageError, ArtifactError, FileIOError) as error:
        print(str(error), file=sys.stderr)
        return 12
    except ProposalError as error:
        print(str(error), file=sys.stderr)
        return 2 if error.code == "PROPOSAL_INVALID" else 13
    except DispatchError as error:
        print(str(error), file=sys.stderr)
        return 14
