from dataclasses import dataclass

from charlie_pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, ProposalId
from charlie_pinboard.domain.work_models import (
    ArtifactRecord,
    AttemptAuthority,
    AttemptRecord,
    CommandAttemptAuthority,
    CoordinationCommandAuthority,
    CoordinationLeaseAuthority,
    ProposalRecord,
    ScopeAnchor,
    SubjectRevision,
    WorkItem,
)


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    revision: str
    generation: int
    items: tuple[WorkItem, ...]
    attempts: tuple[AttemptRecord, ...] = ()
    artifacts: tuple[ArtifactRecord, ...] = ()
    proposals: tuple[ProposalRecord, ...] = ()
    subject_revisions: tuple[SubjectRevision, ...] = ()
    attempt_authorities: tuple[AttemptAuthority, ...] = ()
    command_attempt_authorities: tuple[CommandAttemptAuthority, ...] = ()
    coordination_authority: CoordinationCommandAuthority | None = None
    history_items: tuple[ItemId, ...] = ()
    scopes: tuple[ScopeAnchor, ...] = ()
    host_epoch: int = 0
    focus_item: ItemId | None = None
    focus_attempt: AttemptId | None = None
    can_transfer_coordinator: bool = False
    coordination_lease: CoordinationLeaseAuthority | None = None

    def items_by_id(self) -> dict[ItemId, WorkItem]:
        return {item.item: item for item in self.items}

    def item(self, item_id: ItemId) -> WorkItem | None:
        return next((item for item in self.items if item.item == item_id), None)

    def item_for_attempt(self, attempt_id: AttemptId) -> WorkItem | None:
        return next((item for item in self.items if item.attempt == attempt_id), None)

    def attempts_by_id(self) -> dict[AttemptId, AttemptRecord]:
        return {attempt.attempt: attempt for attempt in self.attempts}

    def attempt(self, attempt_id: AttemptId) -> AttemptRecord | None:
        return next((attempt for attempt in self.attempts if attempt.attempt == attempt_id), None)

    def proposal(self, proposal_id: ProposalId) -> ProposalRecord | None:
        return next((proposal for proposal in self.proposals if proposal.proposal == proposal_id), None)

    def subject_revision(self, subject: ItemId | AttemptId | ProposalId) -> str | None:
        return next((value.revision for value in self.subject_revisions if value.subject == subject), None)

    def authority_for(self, attempt: AttemptId, lease_id: LeaseId | None, generation: int) -> AttemptAuthority | None:
        return next(
            (
                authority
                for authority in self.attempt_authorities
                if authority.attempt == attempt
                and authority.lease_id == lease_id
                and authority.generation == generation
            ),
            None,
        )
