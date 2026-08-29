from dataclasses import dataclass
from datetime import datetime

from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId


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
    relation: work_models.ProposalRelation
    urgency_evidence: str
    evidence: tuple[str, ...]
    freshness_assumptions: tuple[str, ...]
    position: int | None = None


@dataclass(frozen=True, slots=True)
class VisibleProposalItem:
    item_id: ItemId
    position: int
    dependencies: tuple[ItemId, ...]
    scope_digest: str


@dataclass(frozen=True, slots=True)
class PrerequisiteDependencyChange:
    item_id: ItemId
    dependency_id: ItemId
    position: int
    scope_revision: int
    scope_digest_before: str
    scope_digest_after: str


@dataclass(frozen=True, slots=True)
class CreateProposalOperation:
    intake: ProposalIntake


@dataclass(frozen=True, slots=True)
class ProposalCreationDecision:
    proposal: ProposalIntake
    visible_item: VisibleProposalItem
    prerequisite_change: PrerequisiteDependencyChange | None
    evidence: tuple[str, ...]
    freshness: tuple[str, ...]
