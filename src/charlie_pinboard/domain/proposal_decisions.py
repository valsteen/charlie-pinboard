import re

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import ItemId
from charlie_pinboard.domain.ledger import LedgerSnapshot
from charlie_pinboard.domain.proposal_models import (
    CreateProposalOperation,
    LocalIntakeAuthority,
    PrerequisiteDependencyChange,
    ProposalCreationDecision,
    VisibleProposalItem,
)
from charlie_pinboard.domain.work_models import ItemScope, ProposalRelationKind, ScopeDependency

_PROPOSAL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _proposal_text(value: str) -> bool:
    return bool(value) and value.strip() == value and "|" not in value and "\n" not in value


def decide_proposal_creation(  # noqa: C901, PLR0912
    authority: LocalIntakeAuthority,
    current_project_revision: int,
    current_host_epoch: int,
    snapshot: LedgerSnapshot,
    operation: CreateProposalOperation,
) -> DecisionResult[ProposalCreationDecision]:
    intake = operation.intake
    if authority != LocalIntakeAuthority(current_project_revision, current_host_epoch):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Local intake authority is stale.")
    if not _PROPOSAL_ID.fullmatch(str(intake.proposal_id)):
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_IDENTITY_INVALID,
            "Proposal identity must be a canonical lowercase hyphenated identifier.",
        )
    if snapshot.proposal(intake.proposal_id) is not None:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, "Proposal identity already exists.")
    item_id = ItemId(intake.proposal_id)
    if snapshot.item(item_id) is not None or item_id in snapshot.history_items:
        return DecisionFailure(DecisionFailureCode.ITEM_ALREADY_EXISTS, "Proposal identity already names a work item.")
    text = (
        intake.user_label,
        intake.trigger,
        intake.why_it_matters,
        intake.effect,
        intake.unlock,
        intake.urgency_evidence,
        *intake.evidence,
        *intake.freshness_assumptions,
    )
    if not all(_proposal_text(value) for value in text):
        return DecisionFailure(DecisionFailureCode.PROPOSAL_INVALID, "Proposal text must be canonical and nonempty.")
    if len(intake.evidence) != len(set(intake.evidence)) or len(intake.freshness_assumptions) != len(
        set(intake.freshness_assumptions)
    ):
        return DecisionFailure(DecisionFailureCode.PROPOSAL_INVALID, "Proposal evidence must be ordered and unique.")
    relation_without_item = intake.relation in {
        ProposalRelationKind.INDEPENDENT,
        ProposalRelationKind.CLARIFICATION,
    }
    if relation_without_item != (intake.relation_item is None):
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            "Independent and clarification proposals omit a related item; other relations require one.",
        )
    if (
        intake.relation_item is not None
        and snapshot.item(intake.relation_item) is None
        and intake.relation_item not in snapshot.history_items
    ):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, "The related work item does not exist.")
    live_count = len(snapshot.items)
    position = intake.position if intake.position is not None else live_count + 1
    if position < 1 or position > live_count + 1:
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            f"Proposal position must be between 1 and {live_count + 1}.",
        )
    dependencies = (
        (intake.relation_item,)
        if intake.relation == ProposalRelationKind.FOLLOW_UP and intake.relation_item is not None
        else ()
    )
    scope = ItemScope(
        item_id,
        intake.user_label,
        intake.trigger,
        intake.why_it_matters,
        intake.effect,
        intake.unlock,
        tuple(ScopeDependency(index, dependency) for index, dependency in enumerate(dependencies)),
    )
    digest = item_scope_digest(scope)
    if isinstance(digest, DecisionFailure):
        return digest
    prerequisite_change: PrerequisiteDependencyChange | None = None
    if intake.relation == ProposalRelationKind.PREREQUISITE and intake.relation_item is not None:
        target = snapshot.item(intake.relation_item)
        anchor = next((value for value in snapshot.scopes if value.item == intake.relation_item), None)
        if target is not None and anchor is not None:
            dependency_position = len(anchor.scope.dependencies)
            changed_scope = ItemScope(
                anchor.scope.item_id,
                anchor.scope.user_label,
                anchor.scope.trigger,
                anchor.scope.why_it_matters,
                anchor.scope.effect,
                anchor.scope.unlock,
                (*anchor.scope.dependencies, ScopeDependency(dependency_position, item_id)),
                anchor.scope.artifacts,
            )
            changed_digest = item_scope_digest(changed_scope)
            if isinstance(changed_digest, DecisionFailure):
                return changed_digest
            prerequisite_change = PrerequisiteDependencyChange(
                intake.relation_item,
                item_id,
                dependency_position,
                anchor.revision,
                anchor.digest,
                changed_digest,
            )
    return ProposalCreationDecision(
        intake,
        VisibleProposalItem(item_id, position, dependencies, digest),
        prerequisite_change,
        intake.evidence,
        intake.freshness_assumptions,
    )
