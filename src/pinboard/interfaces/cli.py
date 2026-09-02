"""Installed command router and process exit boundary.

This module owns the single exhaustive command-family branch and final rendering
of typed failures. Command grammar and use-case composition live with their
thematic interface owners; this root performs no storage or domain work itself.
"""

import sys
from collections.abc import Sequence
from typing import assert_never

from pinboard.adapters.files.errors import ArtifactError, FileIOError, RootError
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.domain.errors import DecisionFailureCode
from pinboard.interfaces import (
    attempt_authority,
    brief_source_commands,
    cli_commands,
    cli_parser,
    coordination_authority,
    dispatch_brief,
    preparation_authority,
    project_handover,
    proposal_commands,
    transitions,
    work_brief_publication,
    work_inspection,
    work_state_commands,
)
from pinboard.interfaces.errors import (
    BriefSourceError,
    CliResult,
    CommandFailure,
    DispatchFailure,
    ProposalFailure,
    WorkBriefError,
)

build_parser = cli_parser.build_parser


def _dispatch(  # noqa: C901, PLR0912 - one visible exhaustive command-family router
    invocation: cli_commands.CliInvocation,
) -> CliResult[int]:
    roots = work_state_commands.resolve_roots(invocation.roots)
    match invocation.command:
        case cli_commands.RootCommand() as command:
            return work_state_commands.root(roots, command)
        case cli_commands.ValidateCommand() as command:
            return work_state_commands.validate(roots, command)
        case cli_commands.StatusCommand() as command:
            return work_inspection.status(roots, command)
        case cli_commands.OverviewCommand() as command:
            return work_inspection.overview(roots, command)
        case cli_commands.ItemStatusCommand() as command:
            return work_inspection.item_status(roots, command)
        case cli_commands.ItemReviseCommand() as command:
            return transitions.revise_item(roots, command)
        case cli_commands.ItemDefinitionCommand() as command:
            return work_inspection.item_definition(roots, command)
        case cli_commands.ItemDefinitionHistoryCommand() as command:
            return work_inspection.item_definition_history(roots, command)
        case cli_commands.CloseCommand() as command:
            return transitions.close(roots, command)
        case cli_commands.ActionsCommand() | cli_commands.LeasedActionsCommand() as command:
            return work_inspection.actions(roots, command)
        case cli_commands.InputContractCommand() as command:
            return work_inspection.input_contract(roots, command)
        case (cli_commands.BriefSourcesPlanCommand() | cli_commands.BriefSourcesEmitCommand()) as command:
            return brief_source_commands.run_brief_sources(roots, command)
        case cli_commands.BriefPublishCommand() as command:
            return work_brief_publication.publish_brief(roots, command)
        case cli_commands.HandoverCommand() as command:
            return project_handover.export(roots, command)
        case cli_commands.InitializeCommand() as command:
            return work_state_commands.initialize(roots, command)
        case cli_commands.ProposalCommand() as command:
            return proposal_commands.create_proposal(roots, command)
        case (
            cli_commands.CoordinatorTransitionCommand()
            | cli_commands.CoordinationTransitionCommand()
            | cli_commands.AttemptTransitionCommand()
            | cli_commands.PreparationTransitionCommand()
        ) as command:
            return transitions.transition(roots, command)
        case (
            cli_commands.CoordinatorDispatchCommand()
            | cli_commands.CoordinatorReviewedDispatchCommand()
            | cli_commands.CoordinationDispatchCommand()
            | cli_commands.CoordinationReviewedDispatchCommand()
        ) as command:
            return dispatch_brief.prepare_dispatch_command(roots, command)
        case cli_commands.CoordinationApplyCommand() as command:
            return transitions.coordinated_transition(roots, command)
        case (
            cli_commands.CoordinationAcquireCommand()
            | cli_commands.CoordinationRenewCommand()
            | cli_commands.CoordinationReleaseCommand()
            | cli_commands.CoordinationRevokeCommand()
            | cli_commands.CoordinationStatusCommand()
        ) as command:
            return coordination_authority.change_coordination_authority(roots, command)
        case cli_commands.AttemptStatusCommand() as command:
            return attempt_authority.attempt_status(roots, command)
        case (
            cli_commands.AttemptAcquireCommand()
            | cli_commands.CoordinatedAttemptAcquireCommand()
            | cli_commands.AttemptRenewCommand()
            | cli_commands.AttemptReleaseCommand()
            | cli_commands.AttemptRevokeCommand()
        ) as command:
            return attempt_authority.change_attempt_authority(roots, command)
        case cli_commands.PreparationStatusCommand() as command:
            return preparation_authority.preparation_status(roots, command)
        case (
            cli_commands.CoordinatorPreparationAcquireCommand()
            | cli_commands.CoordinatedPreparationTransferCommand()
            | cli_commands.PreparationRenewCommand()
            | cli_commands.PreparationReleaseCommand()
            | cli_commands.PreparationRevokeCommand()
        ) as command:
            return preparation_authority.change_preparation_authority(roots, command)
        case cli_commands.ParallelPreviewCommand() as command:
            return work_inspection.parallel(roots, command)
        case cli_commands.RebuildViewsCommand() as command:
            return work_state_commands.rebuild_views(roots, command)
        case _ as unreachable:
            assert_never(unreachable)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _dispatch(cli_parser.parse_invocation(argv))
        match result:
            case int():
                return result
            case CommandFailure():
                exit_code = 11
            case ProposalFailure(code=DecisionFailureCode.PROPOSAL_INVALID):
                exit_code = 2
            case ProposalFailure():
                exit_code = 13
            case DispatchFailure():
                exit_code = 14
            case _ as unreachable:
                assert_never(unreachable)
        print(str(result), file=sys.stderr)
        return exit_code
    except (RootError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (StorageError, ArtifactError, FileIOError) as error:
        print(str(error), file=sys.stderr)
        return 12
    except BriefSourceError as error:
        print(str(error), file=sys.stderr)
        return 15
    except WorkBriefError as error:
        print(str(error), file=sys.stderr)
        return 16
