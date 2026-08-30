"""The complete installed command grammar and its exact leaf decoder.

This module parses untyped command-line values into one closed CliInvocation.
Argument parsing may terminate through argparse, and msgspec validation failures
are converted to the selected parser's normal usage error. It does not resolve
roots, open resources, dispatch commands, or perform product decisions.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, assert_never, cast

import msgspec

from pinboard import __version__
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ReviewId
from pinboard.interfaces import cli_commands, transition_input

type RawCliValue = str | int | bool | Path | list[str] | None
type RawCliValues = dict[str, RawCliValue]


class _RawCliArguments(argparse.Namespace):
    route: cli_commands.CliRoute | None
    selected_parser: argparse.ArgumentParser | None

    def __init__(self) -> None:
        super().__init__()
        self.route = None
        self.selected_parser = None


class DispatchArgumentError(Exception):
    """A dispatch argument combination rejected before a command is constructed."""


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
    lease_id: cli_commands.StableLeaseId | None
    generation: int | None
    action_id: cli_commands.StableActionId | None
    json: bool

    def __post_init__(self) -> None:
        if (self.lease_id is None) != (self.generation is None):
            raise ValueError("--lease-id and --generation must be supplied together")


class _TransitionArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: cli_commands.StableActionId
    expected_revision: str
    generation: int
    payload: Path
    subject_revision: str | None
    lease_id: cli_commands.StableLeaseId | None
    authorization: Literal["coordinator", "coordination", "attempt"]

    def __post_init__(self) -> None:
        requires_lease = self.authorization != "coordinator"
        if requires_lease != (self.lease_id is not None):
            raise ValueError(f"--lease-id must be supplied exactly for {self.authorization} authorization")


class _DispatchArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: cli_commands.StableActionId
    expected_revision: str
    generation: int
    checkpoint: str
    environment: Path
    lease_id: cli_commands.StableLeaseId | None
    prompt: Path | None
    brief_review: Path | None
    review_id: str | None

    def __post_init__(self) -> None:
        if self.brief_review is None and self.review_id is not None:
            raise ValueError("--review-id is only valid with --brief-review")


def _required_dispatch_review_id(review_id: str | None) -> ReviewId:
    if (
        review_id is None
        or not review_id
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in review_id)
    ):
        raise DispatchArgumentError("--brief-review requires one kebab-case --review-id.")
    return ReviewId(review_id)


class _AttemptAcquireArguments(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: cli_commands.StableAttemptId
    task_id: cli_commands.StableTaskId
    host_id: cli_commands.StableHostId
    ttl_seconds: int
    coordination_lease_id: cli_commands.StableLeaseId | None
    coordination_generation: int | None
    json: bool

    def __post_init__(self) -> None:
        if (self.coordination_lease_id is None) != (self.coordination_generation is None):
            raise ValueError("--coordination-lease-id and --coordination-generation must be supplied together")


def _decode_brief_sources(
    values: RawCliValues,
) -> cli_commands.BriefSourcesPlanCommand | cli_commands.BriefSourcesEmitCommand:
    arguments = msgspec.convert(values, type=_BriefSourcesArguments, strict=True)
    if arguments.emit_batch is None:
        return cli_commands.BriefSourcesPlanCommand(file=arguments.file, max_batch_bytes=arguments.max_batch_bytes)
    return cli_commands.BriefSourcesEmitCommand(
        file=arguments.file,
        emit_batch=arguments.emit_batch,
        max_batch_bytes=arguments.max_batch_bytes,
    )


def _decode_actions(values: RawCliValues) -> cli_commands.ActionsCommand | cli_commands.LeasedActionsCommand:
    arguments = msgspec.convert(values, type=_ActionsArguments, strict=True)
    if arguments.lease_id is None or arguments.generation is None:
        return cli_commands.ActionsCommand(role=arguments.role, action_id=arguments.action_id, json=arguments.json)
    return cli_commands.LeasedActionsCommand(
        role=arguments.role,
        lease_id=arguments.lease_id,
        generation=arguments.generation,
        action_id=arguments.action_id,
        json=arguments.json,
    )


def _decode_transition(values: RawCliValues) -> cli_commands.TransitionCommand:
    arguments = msgspec.convert(values, type=_TransitionArguments, strict=True)
    match arguments.authorization:
        case "coordinator":
            return cli_commands.CoordinatorTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                subject_revision=arguments.subject_revision,
            )
        case "coordination":
            lease_id = cast("cli_commands.StableLeaseId", arguments.lease_id)
            return cli_commands.CoordinationTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=lease_id,
                subject_revision=arguments.subject_revision,
            )
        case "attempt":
            lease_id = cast("cli_commands.StableLeaseId", arguments.lease_id)
            return cli_commands.AttemptTransitionCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                payload=arguments.payload,
                lease_id=lease_id,
                subject_revision=arguments.subject_revision,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _decode_dispatch(values: RawCliValues) -> cli_commands.DispatchCommand:
    arguments = msgspec.convert(values, type=_DispatchArguments, strict=True)
    if arguments.lease_id is None:
        if arguments.brief_review is None:
            return cli_commands.CoordinatorDispatchCommand(
                action_id=arguments.action_id,
                expected_revision=arguments.expected_revision,
                generation=arguments.generation,
                checkpoint=arguments.checkpoint,
                environment=arguments.environment,
                prompt=arguments.prompt,
            )
        return cli_commands.CoordinatorReviewedDispatchCommand(
            action_id=arguments.action_id,
            expected_revision=arguments.expected_revision,
            generation=arguments.generation,
            checkpoint=arguments.checkpoint,
            environment=arguments.environment,
            brief_review=arguments.brief_review,
            prompt=arguments.prompt,
            review_id=_required_dispatch_review_id(arguments.review_id),
        )
    if arguments.brief_review is None:
        return cli_commands.CoordinationDispatchCommand(
            action_id=arguments.action_id,
            expected_revision=arguments.expected_revision,
            generation=arguments.generation,
            lease_id=arguments.lease_id,
            checkpoint=arguments.checkpoint,
            environment=arguments.environment,
            prompt=arguments.prompt,
        )
    return cli_commands.CoordinationReviewedDispatchCommand(
        action_id=arguments.action_id,
        expected_revision=arguments.expected_revision,
        generation=arguments.generation,
        lease_id=arguments.lease_id,
        checkpoint=arguments.checkpoint,
        environment=arguments.environment,
        brief_review=arguments.brief_review,
        prompt=arguments.prompt,
        review_id=_required_dispatch_review_id(arguments.review_id),
    )


def _decode_attempt_acquire(
    values: RawCliValues,
) -> cli_commands.AttemptAcquireCommand | cli_commands.CoordinatedAttemptAcquireCommand:
    arguments = msgspec.convert(values, type=_AttemptAcquireArguments, strict=True)
    if arguments.coordination_lease_id is None or arguments.coordination_generation is None:
        return cli_commands.AttemptAcquireCommand(
            attempt_id=arguments.attempt_id,
            task_id=arguments.task_id,
            host_id=arguments.host_id,
            ttl_seconds=arguments.ttl_seconds,
            json=arguments.json,
        )
    return cli_commands.CoordinatedAttemptAcquireCommand(
        attempt_id=arguments.attempt_id,
        task_id=arguments.task_id,
        host_id=arguments.host_id,
        ttl_seconds=arguments.ttl_seconds,
        coordination_lease_id=arguments.coordination_lease_id,
        coordination_generation=arguments.coordination_generation,
        json=arguments.json,
    )


def _decode_command(route: cli_commands.CliRoute, values: RawCliValues) -> cli_commands.CliCommand:  # noqa: C901, PLR0912
    match route:
        case cli_commands.CliRoute.ROOT:
            return msgspec.convert(values, type=cli_commands.RootCommand, strict=True)
        case cli_commands.CliRoute.VALIDATE:
            return msgspec.convert(values, type=cli_commands.ValidateCommand, strict=True)
        case cli_commands.CliRoute.STATUS:
            return msgspec.convert(values, type=cli_commands.StatusCommand, strict=True)
        case cli_commands.CliRoute.OVERVIEW:
            return msgspec.convert(values, type=cli_commands.OverviewCommand, strict=True)
        case cli_commands.CliRoute.ITEM_STATUS:
            return msgspec.convert(values, type=cli_commands.ItemStatusCommand, strict=True)
        case cli_commands.CliRoute.ITEM_REVISE:
            return msgspec.convert(values, type=cli_commands.ItemReviseCommand, strict=True)
        case cli_commands.CliRoute.ITEM_DEFINITION:
            return msgspec.convert(values, type=cli_commands.ItemDefinitionCommand, strict=True)
        case cli_commands.CliRoute.ITEM_DEFINITION_HISTORY:
            return msgspec.convert(values, type=cli_commands.ItemDefinitionHistoryCommand, strict=True)
        case cli_commands.CliRoute.CLOSE:
            return msgspec.convert(values, type=cli_commands.CloseCommand, strict=True)
        case cli_commands.CliRoute.ACTIONS:
            return _decode_actions(values)
        case cli_commands.CliRoute.INPUT_CONTRACT:
            return msgspec.convert(values, type=cli_commands.InputContractCommand, strict=True)
        case cli_commands.CliRoute.BRIEF_SOURCES:
            return _decode_brief_sources(values)
        case cli_commands.CliRoute.BRIEF_PUBLISH:
            return msgspec.convert(values, type=cli_commands.BriefPublishCommand, strict=True)
        case cli_commands.CliRoute.INITIALIZE:
            return msgspec.convert(values, type=cli_commands.InitializeCommand, strict=True)
        case cli_commands.CliRoute.PROPOSAL:
            return msgspec.convert(values, type=cli_commands.ProposalCommand, strict=True)
        case cli_commands.CliRoute.TRANSITION:
            return _decode_transition(values)
        case cli_commands.CliRoute.DISPATCH:
            return _decode_dispatch(values)
        case cli_commands.CliRoute.COORDINATION_APPLY:
            return msgspec.convert(values, type=cli_commands.CoordinationApplyCommand, strict=True)
        case cli_commands.CliRoute.COORDINATION_ACQUIRE:
            return msgspec.convert(values, type=cli_commands.CoordinationAcquireCommand, strict=True)
        case cli_commands.CliRoute.COORDINATION_RENEW:
            return msgspec.convert(values, type=cli_commands.CoordinationRenewCommand, strict=True)
        case cli_commands.CliRoute.COORDINATION_RELEASE:
            return msgspec.convert(values, type=cli_commands.CoordinationReleaseCommand, strict=True)
        case cli_commands.CliRoute.COORDINATION_REVOKE:
            return msgspec.convert(values, type=cli_commands.CoordinationRevokeCommand, strict=True)
        case cli_commands.CliRoute.COORDINATION_STATUS:
            return msgspec.convert(values, type=cli_commands.CoordinationStatusCommand, strict=True)
        case cli_commands.CliRoute.ATTEMPT_ACQUIRE:
            return _decode_attempt_acquire(values)
        case cli_commands.CliRoute.ATTEMPT_RENEW:
            return msgspec.convert(values, type=cli_commands.AttemptRenewCommand, strict=True)
        case cli_commands.CliRoute.ATTEMPT_RELEASE:
            return msgspec.convert(values, type=cli_commands.AttemptReleaseCommand, strict=True)
        case cli_commands.CliRoute.ATTEMPT_REVOKE:
            return msgspec.convert(values, type=cli_commands.AttemptRevokeCommand, strict=True)
        case cli_commands.CliRoute.ATTEMPT_STATUS:
            return msgspec.convert(values, type=cli_commands.AttemptStatusCommand, strict=True)
        case cli_commands.CliRoute.PARALLEL_PREVIEW:
            return msgspec.convert(values, type=cli_commands.ParallelPreviewCommand, strict=True)
        case cli_commands.CliRoute.REBUILD_VIEWS:
            return msgspec.convert(values, type=cli_commands.RebuildViewsCommand, strict=True)
        case _ as unreachable:
            assert_never(unreachable)


def _select(parser: argparse.ArgumentParser, route: cli_commands.CliRoute) -> None:
    parser.set_defaults(route=route, selected_parser=parser)


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
    _select(apply, cli_commands.CliRoute.COORDINATION_APPLY)
    acquire = operations.add_parser("acquire")
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--host-id", required=True)
    acquire.add_argument("--ttl-seconds", required=True, type=int)
    acquire.add_argument("--json", action="store_true")
    _select(acquire, cli_commands.CliRoute.COORDINATION_ACQUIRE)
    renew = operations.add_parser("renew")
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select(renew, cli_commands.CliRoute.COORDINATION_RENEW)
    release = operations.add_parser("release")
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select(release, cli_commands.CliRoute.COORDINATION_RELEASE)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--json", action="store_true")
    _select(revoke, cli_commands.CliRoute.COORDINATION_REVOKE)
    status = operations.add_parser("status")
    status.add_argument("--json", action="store_true")
    _select(status, cli_commands.CliRoute.COORDINATION_STATUS)


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
    _select(acquire, cli_commands.CliRoute.ATTEMPT_ACQUIRE)
    renew = operations.add_parser("renew")
    renew.add_argument("--attempt-id", required=True)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--ttl-seconds", required=True, type=int)
    renew.add_argument("--json", action="store_true")
    _select(renew, cli_commands.CliRoute.ATTEMPT_RENEW)
    release = operations.add_parser("release")
    release.add_argument("--attempt-id", required=True)
    release.add_argument("--lease-id", required=True)
    release.add_argument("--generation", required=True, type=int)
    release.add_argument("--json", action="store_true")
    _select(release, cli_commands.CliRoute.ATTEMPT_RELEASE)
    revoke = operations.add_parser("revoke")
    revoke.add_argument("--attempt-id", required=True)
    revoke.add_argument("--lease-id", required=True)
    revoke.add_argument("--generation", required=True, type=int)
    revoke.add_argument("--coordination-lease-id", required=True)
    revoke.add_argument("--coordination-generation", required=True, type=int)
    revoke.add_argument("--json", action="store_true")
    _select(revoke, cli_commands.CliRoute.ATTEMPT_REVOKE)
    status = operations.add_parser("status")
    status.add_argument("--attempt-id", required=True)
    status.add_argument("--json", action="store_true")
    _select(status, cli_commands.CliRoute.ATTEMPT_STATUS)


def _add_item_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    item = commands.add_parser("item", help="Inspect one exact live or terminal item.")
    operations = item.add_subparsers(required=True)
    status = operations.add_parser("status", help="Show authoritative status for one exact item.")
    status.add_argument("--item-id", required=True)
    status.add_argument("--json", action="store_true")
    _select(status, cli_commands.CliRoute.ITEM_STATUS)
    revise = operations.add_parser("revise", help="Replace one nonterminal item's complete accepted definition.")
    revise.add_argument("--file", required=True, type=Path)
    revise.add_argument("--task-id", required=True)
    revise.add_argument("--host-id", required=True)
    revise.add_argument("--ttl-seconds", type=int, default=60)
    revise.add_argument("--json", action="store_true")
    _select(revise, cli_commands.CliRoute.ITEM_REVISE)
    definition = operations.add_parser("definition", help="Show one item's complete current accepted definition.")
    definition.add_argument("--item-id", required=True)
    definition.add_argument("--json", action="store_true")
    _select(definition, cli_commands.CliRoute.ITEM_DEFINITION)
    history = operations.add_parser("definition-history", help="Show newest-first immutable definition revisions.")
    history.add_argument("--item-id", required=True)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--before-revision", type=int)
    history.add_argument("--json", action="store_true")
    _select(history, cli_commands.CliRoute.ITEM_DEFINITION_HISTORY)


def _add_parallel_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parallel = commands.add_parser("parallel", help="Preview structurally independent work without launching it.")
    operations = parallel.add_subparsers(required=True)
    preview = operations.add_parser("preview")
    preview.add_argument("--item", action="append", default=[])
    preview.add_argument("--json", action="store_true")
    _select(preview, cli_commands.CliRoute.PARALLEL_PREVIEW)


def _add_chat_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    overview = commands.add_parser("overview", help="Show one coherent live-work snapshot.")
    overview.add_argument("--json", action="store_true")
    _select(overview, cli_commands.CliRoute.OVERVIEW)
    close = commands.add_parser("close", help="Record a terminal decision for non-active work.")
    close.add_argument("item_id")
    close.add_argument("--outcome", choices=tuple(outcome.value for outcome in work_models.CloseOutcome), required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--task-id", required=True)
    close.add_argument("--host-id", required=True)
    close.add_argument("--ttl-seconds", type=int, default=60)
    close.add_argument("--json", action="store_true")
    _select(close, cli_commands.CliRoute.CLOSE)


def _add_inspection_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    root = commands.add_parser("root", help="Resolve the source checkout, shared repository, and work roots.")
    _select(root, cli_commands.CliRoute.ROOT)
    validate = commands.add_parser("validate", help="Validate work state without modifying it.")
    validate.add_argument("--json", action="store_true")
    _select(validate, cli_commands.CliRoute.VALIDATE)
    status = commands.add_parser("status", help="Show bounded current work facts.")
    status.add_argument("--json", action="store_true")
    _select(status, cli_commands.CliRoute.STATUS)
    _add_chat_parser(commands)
    _add_item_parser(commands)
    actions = commands.add_parser("actions", help="List the legal contextual actions.")
    actions.add_argument("--role", choices=tuple(role.value for role in decision_models.Role), required=True)
    actions.add_argument("--lease-id")
    actions.add_argument("--generation", type=int)
    actions.add_argument("--action-id", help="Return only this exact currently legal action.")
    actions.add_argument("--json", action="store_true")
    _select(actions, cli_commands.CliRoute.ACTIONS)
    input_contract = commands.add_parser(
        "input-contract", help="Show the canonical payload and semantics for one action kind."
    )
    input_contract.add_argument("action_kind", choices=transition_input.INPUT_CONTRACT_ACTION_KINDS)
    input_contract.add_argument("--json", action="store_true")
    _select(input_contract, cli_commands.CliRoute.INPUT_CONTRACT)
    brief_sources = commands.add_parser(
        "brief-sources",
        help="Plan or emit deterministic context-bounded authority source batches.",
    )
    brief_sources.add_argument("--file", type=Path, required=True, help="pinboard-brief-sources/v1 manifest.")
    brief_sources.add_argument("--max-batch-bytes", type=int, default=24_000)
    brief_source_output = brief_sources.add_mutually_exclusive_group(required=True)
    brief_source_output.add_argument("--json", action="store_true", help="Print the complete batch plan.")
    brief_source_output.add_argument("--emit-batch", type=int, help="Print exactly one zero-based planned batch.")
    _select(brief_sources, cli_commands.CliRoute.BRIEF_SOURCES)


def _add_brief_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    brief = commands.add_parser("brief", help="Publish canonical typed work briefs without scheduling them.")
    operations = brief.add_subparsers(required=True)
    publish = operations.add_parser("publish", help="Validate and immutably publish one pinboard-work-brief/v2 file.")
    publish.add_argument("--file", type=Path, required=True)
    publish.add_argument("--json", action="store_true")
    _select(publish, cli_commands.CliRoute.BRIEF_PUBLISH)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pinboard", description="Inspect and transition one pinboard.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--project-root", type=Path, help="Select the exact source checkout for authority reads.")
    parser.add_argument("--work-root", type=Path)
    commands = parser.add_subparsers(required=True)
    _add_inspection_parsers(commands)
    initialize = commands.add_parser("init", help="Create an empty current SQLite work state.")
    _select(initialize, cli_commands.CliRoute.INITIALIZE)
    _add_brief_parser(commands)
    proposal = commands.add_parser("proposal", help="Create one visible intake candidate without activating it.")
    proposal.add_argument("--file", type=Path, required=True)
    _select(proposal, cli_commands.CliRoute.PROPOSAL)
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
    _select(transition, cli_commands.CliRoute.TRANSITION)
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
    _select(dispatch, cli_commands.CliRoute.DISPATCH)
    _add_coordination_parser(commands)
    _add_attempt_parser(commands)
    _add_parallel_parser(commands)
    views = commands.add_parser("views", help="Repair generated human-readable views.")
    rebuild = views.add_subparsers(required=True).add_parser("rebuild")
    _select(rebuild, cli_commands.CliRoute.REBUILD_VIEWS)
    return parser


def _decode_invocation(
    parser: argparse.ArgumentParser,
    raw: _RawCliArguments,
) -> cli_commands.CliInvocation:
    route = raw.route
    selected_parser = raw.selected_parser
    if selected_parser is None or route is None:
        parser.error("the selected command has no decoder")
    untyped_values = vars(raw).copy()
    for metadata_name in ("route", "selected_parser"):
        untyped_values.pop(metadata_name, None)
    values = cast("RawCliValues", untyped_values)
    root_values: RawCliValues = {
        "project_root": values.pop("project_root", None),
        "work_root": values.pop("work_root", None),
    }
    try:
        roots = msgspec.convert(root_values, type=cli_commands.RootSelection, strict=True)
        command = _decode_command(route, values)
    except msgspec.ValidationError as error:
        selected_parser.error(str(error))
    return cli_commands.CliInvocation(roots, command)


def parse_invocation(argv: Sequence[str] | None = None) -> cli_commands.CliInvocation:
    """Parse one invocation without resolving roots or executing the command."""
    parser = build_parser()
    raw = parser.parse_args(argv, namespace=_RawCliArguments())
    return _decode_invocation(parser, raw)
