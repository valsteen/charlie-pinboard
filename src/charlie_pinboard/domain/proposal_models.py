from dataclasses import dataclass
from datetime import datetime

from charlie_pinboard.domain.identifiers import ItemId, ProposalId, TaskId
from charlie_pinboard.domain.work_models import ProposalRelationKind


@dataclass(frozen=True, slots=True)
class LocalIntakeAuthority:
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
