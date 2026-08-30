"""Create one proposal from its installed boundary representation.

This outer command owner reads and decodes the candidate, performs the explicit
boundary-to-domain conversion, opens the concrete store, invokes the proposal
use case, and refreshes its affected views. Expected proposal rejections are
returned as values; infrastructure failures remain exceptions.
"""

import sys
from datetime import UTC, datetime
from typing import assert_never

from pinboard.adapters.files import models as file_models
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import service
from pinboard.domain import proposal_models as domain_proposal_models
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId
from pinboard.interfaces import cli_commands, proposal_models, proposals, work_views
from pinboard.interfaces.errors import ProposalFailure, ProposalResult


def _domain_proposal_relation(value: proposal_models.ProposalRelation) -> work_models.ProposalRelation:
    match value:
        case proposal_models.IndependentProposalRelation():
            return work_models.IndependentProposalRelation()
        case proposal_models.PrerequisiteProposalRelation(item=item):
            return work_models.PrerequisiteProposalRelation(ItemId(item))
        case proposal_models.FollowUpProposalRelation(item=item):
            return work_models.FollowUpProposalRelation(ItemId(item))
        case proposal_models.DuplicateProposalRelation(item=item):
            return work_models.DuplicateProposalRelation(ItemId(item))
        case proposal_models.ContradictionProposalRelation(item=item):
            return work_models.ContradictionProposalRelation(ItemId(item))
        case proposal_models.ClarificationProposalRelation():
            return work_models.ClarificationProposalRelation()
        case _ as unreachable:
            assert_never(unreachable)


def create_proposal(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.ProposalCommand,
) -> ProposalResult[int]:
    path = command.file
    try:
        data = path.read_bytes()
    except OSError as error:
        return ProposalFailure(DecisionFailureCode.PROPOSAL_INVALID, f"Cannot read proposal at '{path}': {error}")
    proposal = proposals.parse_proposal(data)
    if isinstance(proposal, ProposalFailure):
        return proposal
    try:
        created_at = datetime.fromisoformat(proposal.created_at.replace("Z", "+00:00"))
    except ValueError:
        return ProposalFailure(DecisionFailureCode.PROPOSAL_INVALID, "Proposal created_at must be an ISO timestamp.")
    if created_at.tzinfo is None:
        if len(proposal.created_at) == 10:
            created_at = created_at.replace(tzinfo=UTC)
        else:
            return ProposalFailure(
                DecisionFailureCode.PROPOSAL_INVALID,
                "Proposal created_at must include a timezone.",
            )
    intake = domain_proposal_models.ProposalIntake(
        ProposalId(proposal.proposal_id),
        created_at.astimezone(UTC),
        TaskId(proposal.source_task_id),
        proposal.user_label,
        proposal.trigger,
        proposal.why_it_matters,
        proposal.effect,
        proposal.unlock,
        _domain_proposal_relation(proposal.relation),
        proposal.urgency_evidence,
        proposal.evidence,
        proposal.freshness_assumptions,
        proposal.position,
    )
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    result = service.create_proposal(
        store,
        domain_proposal_models.CreateProposalOperation(intake),
        datetime.now(UTC),
    )
    if isinstance(result, DecisionFailure):
        return ProposalFailure(result.code, result.message)
    view_result = work_views.refresh(roots, store, file_models.AffectedViews(queue=True, history=True))
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    visible = next(
        value for value in store.snapshot().lifecycle.work_items if str(value.item_id) == proposal.proposal_id
    )
    print(f"OK PROPOSAL_CREATED {proposal.proposal_id} position={visible.queue_position} state={visible.state.value}")
    return 0
