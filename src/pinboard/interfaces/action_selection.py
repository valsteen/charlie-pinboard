from typing import assert_never

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.actions import discover_actions
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, ItemId, LedgerId, ProposalId, SubjectId
from pinboard.interfaces import cli_commands
from pinboard.interfaces.errors import CommandErrorCode, CommandFailure, CommandResult


def action_from_command(  # noqa: C901, PLR0912, PLR0915
    command: cli_commands.TransitionCommand | cli_commands.DispatchCommand,
) -> CommandResult[decision_models.Action]:
    selected_action_id = command.action_id
    if ":" not in selected_action_id:
        return CommandFailure(CommandErrorCode.ACTION_ID_INVALID, "Action identity must be 'kind:subject'.")
    kind_value, subject = selected_action_id.split(":", 1)
    try:
        kind = decision_models.ActionKind(kind_value)
    except ValueError as error:
        return CommandFailure(CommandErrorCode.ACTION_ID_INVALID, f"Unknown action kind: {error}.")
    match command:
        case cli_commands.CoordinatorTransitionCommand(subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.COORDINATOR
            lease_id = None
        case cli_commands.CoordinationTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.COORDINATION
        case cli_commands.AttemptTransitionCommand(lease_id=lease_id, subject_revision=subject_revision):
            authorization = decision_models.AuthorizationKind.ATTEMPT
        case cli_commands.CoordinatorDispatchCommand() | cli_commands.CoordinatorReviewedDispatchCommand():
            authorization = decision_models.AuthorizationKind.COORDINATOR
            lease_id = None
            subject_revision = None
        case (
            cli_commands.CoordinationDispatchCommand(lease_id=lease_id)
            | cli_commands.CoordinationReviewedDispatchCommand(lease_id=lease_id)
        ):
            authorization = decision_models.AuthorizationKind.COORDINATION
            subject_revision = None
        case _ as unreachable:
            assert_never(unreachable)

    def capability[SubjectT: SubjectId](
        subject_id: SubjectT,
    ) -> decision_models.MutationActionCapability[SubjectT]:
        return decision_models.MutationActionCapability(
            subject_id,
            str(selected_action_id),
            command.expected_revision,
            command.generation,
            subject_revision,
            authorization,
            lease_id,
        )

    match kind:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return decision_models.AcceptCheckpointAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return decision_models.AcceptReviewAndContinueAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.ACCEPT_PROPOSAL:
            return decision_models.AcceptProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.ACTIVATE:
            return decision_models.ActivateAction(capability(ItemId(subject)))
        case decision_models.ActionKind.BLOCK:
            return decision_models.BlockAttemptAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.BLOCK_ITEM:
            return decision_models.BlockItemAction(capability(ItemId(subject)))
        case decision_models.ActionKind.COMPLETE:
            return decision_models.CompleteAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.CLOSE:
            return decision_models.CloseAction(capability(ItemId(subject)))
        case decision_models.ActionKind.CONTINUE:
            return decision_models.ContinueAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.DEFER:
            return decision_models.DeferAction(capability(ItemId(subject)))
        case decision_models.ActionKind.DISPATCH:
            return decision_models.DispatchAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.INSPECT:
            return decision_models.InspectAction(capability(LedgerId(subject)))
        case decision_models.ActionKind.MARK_READY:
            return decision_models.MarkReadyAction(capability(ItemId(subject)))
        case decision_models.ActionKind.MERGE_PROPOSAL:
            return decision_models.MergeProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.PAUSE:
            return decision_models.PauseAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.REJECT_PROPOSAL:
            return decision_models.RejectProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.REOPEN:
            return decision_models.ReopenAction(capability(ItemId(subject)))
        case decision_models.ActionKind.REPORT_BLOCKER:
            return decision_models.ReportBlockerAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.RESUME:
            return decision_models.ResumeAction(capability(ItemId(subject)))
        case decision_models.ActionKind.RETURN_FOR_CORRECTION:
            return decision_models.ReturnForCorrectionAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.RETURN_PROPOSAL:
            return decision_models.ReturnProposalAction(capability(ProposalId(subject)))
        case decision_models.ActionKind.REVISE_ITEM:
            return decision_models.ReviseItemAction(capability(ItemId(subject)))
        case decision_models.ActionKind.SUBMIT_REVIEW:
            return decision_models.SubmitReviewAction(capability(AttemptId(subject)))
        case decision_models.ActionKind.TRANSFER_COORDINATOR:
            return decision_models.TransferCoordinatorAction(capability(LedgerId(subject)))
        case _ as unreachable:
            assert_never(unreachable)


def reselect_action(
    roots: cli_commands.ResolvedRoots,
    supplied: decision_models.Action,
    role: decision_models.Role,
) -> CommandResult[decision_models.Action]:
    supplied_capability = supplied.capability
    available = discover_actions(
        SQLiteWorkStore(roots.work / "state.sqlite3"),
        role,
        lease_id=supplied_capability.lease_id,
        generation=supplied_capability.coordinator_generation,
    )
    if isinstance(available, DecisionFailure):
        return CommandFailure(available.code, available.message)
    current = next(
        (value for value in available if decision_models.action_id(value) == decision_models.action_id(supplied)), None
    )
    if current is None:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied)}' is not currently legal.",
        )
    current_capability = current.capability
    if current_capability.expected_revision != supplied_capability.expected_revision:
        return CommandFailure(CommandErrorCode.STALE_ACTION, "The work ledger changed after this action was selected.")
    supplied_authority = (
        supplied_capability.coordinator_generation,
        supplied_capability.subject_revision,
        supplied_capability.authorization,
        supplied_capability.lease_id,
    )
    current_authority = (
        current_capability.coordinator_generation,
        current_capability.subject_revision,
        current_capability.authorization,
        current_capability.lease_id,
    )
    if current_authority != supplied_authority:
        return CommandFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(supplied)}' no longer has exact current authority.",
        )
    return current
