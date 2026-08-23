import re
from dataclasses import dataclass
from datetime import datetime

from charlie_pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from charlie_pinboard.domain.identifiers import ItemId, ProposalId, TaskId
from charlie_pinboard.domain.model import ProposalRelationKind

_PROPOSAL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class SQLiteLocalIntakeAuthority:
    project_revision: int
    host_epoch: int


@dataclass(frozen=True, slots=True)
class ProposalIntake:
    proposal_id: ProposalId
    created_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    effect: str
    unlock: str
    relation: ProposalRelationKind
    relation_item: ItemId | None
    urgency_evidence: str
    evidence: tuple[str, ...]
    freshness_assumptions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateProposalOperation:
    intake: ProposalIntake


@dataclass(frozen=True, slots=True)
class ProposalCreationDecision:
    proposal: ProposalIntake
    evidence: tuple[str, ...]
    freshness: tuple[str, ...]


def _proposal_text(value: str) -> bool:
    return bool(value) and value.strip() == value and "|" not in value and "\n" not in value


def decide_proposal_creation(
    authority: SQLiteLocalIntakeAuthority,
    current_project_revision: int,
    current_host_epoch: int,
    existing: tuple[ProposalId, ...],
    operation: CreateProposalOperation,
) -> ProposalCreationDecision | DecisionFailure:
    intake = operation.intake
    if authority != SQLiteLocalIntakeAuthority(current_project_revision, current_host_epoch):
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "SQLite intake authority is stale.")
    if not _PROPOSAL_ID.fullmatch(str(intake.proposal_id)):
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_IDENTITY_INVALID,
            "Proposal identity must be a canonical lowercase hyphenated identifier.",
        )
    if intake.proposal_id in existing:
        return DecisionFailure(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, "Proposal identity already exists.")
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
    if (intake.relation == ProposalRelationKind.INDEPENDENT) != (intake.relation_item is None):
        return DecisionFailure(
            DecisionFailureCode.PROPOSAL_INVALID,
            "Only an independent proposal omits its related item.",
        )
    return ProposalCreationDecision(intake, intake.evidence, intake.freshness_assumptions)
