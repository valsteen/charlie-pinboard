from dataclasses import replace as dataclass_replace
from typing import Any  # noqa: TID251 - fixture corruption intentionally crosses the typed boundary

from charlie_pinboard.domain.decisions import Action as ActionValue
from charlie_pinboard.domain.decisions import ActionKind
from charlie_pinboard.domain.identifiers import (
    ActionId,
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    LedgerId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    AcceptedProposalState,
    AttemptState,
    ScopeArtifact,
    Timing,
)
from charlie_pinboard.domain.model import (
    AcceptProposalInput as AcceptProposalInputValue,
)
from charlie_pinboard.domain.model import (
    AttemptAuthority as AttemptAuthorityValue,
)
from charlie_pinboard.domain.model import (
    AttemptRecord as AttemptRecordValue,
)
from charlie_pinboard.domain.model import (
    DeferInput as DeferInputValue,
)
from charlie_pinboard.domain.model import (
    ItemScope as ItemScopeValue,
)
from charlie_pinboard.domain.model import (
    ProposalRecord as ProposalRecordValue,
)
from charlie_pinboard.domain.model import (
    ScopeAnchor as ScopeAnchorValue,
)
from charlie_pinboard.domain.model import (
    ScopeDependency as ScopeDependencyValue,
)
from charlie_pinboard.domain.model import (
    TransferCoordinatorInput as TransferCoordinatorInputValue,
)


def replace(instance: Any, **changes: Any) -> Any:  # noqa: ANN401
    """Create valid variants and deliberately malformed values for rejection tests."""
    return dataclass_replace(instance, **changes)


def action(kind: ActionKind, subject: str) -> ActionValue:
    if kind in {
        ActionKind.ACCEPT_CHECKPOINT,
        ActionKind.BLOCK,
        ActionKind.COMPLETE,
        ActionKind.CONTINUE,
        ActionKind.DISPATCH,
        ActionKind.PAUSE,
        ActionKind.REPORT_BLOCKER,
        ActionKind.RETURN_FOR_CORRECTION,
        ActionKind.SUBMIT_REVIEW,
    }:
        subject_id = AttemptId(subject)
    elif kind in {
        ActionKind.ACCEPT_PROPOSAL,
        ActionKind.MERGE_PROPOSAL,
        ActionKind.REJECT_PROPOSAL,
        ActionKind.RETURN_PROPOSAL,
    }:
        subject_id = ProposalId(subject)
    elif kind in {ActionKind.INSPECT, ActionKind.TRANSFER_COORDINATOR}:
        subject_id = LedgerId(subject)
    else:
        subject_id = ItemId(subject)
    return ActionValue(ActionId(f"{kind.value}:{subject}"), kind, subject_id, kind.value, "rev", 1)


def item_scope(
    item_id: str,
    user_label: str,
    trigger: str | None,
    why_it_matters: str | None,
    effect: str | None,
    unlock: str | None,
    dependencies: tuple[ScopeDependencyValue, ...] = (),
    artifacts: tuple[ScopeArtifact, ...] = (),
) -> ItemScopeValue:
    return ItemScopeValue(
        ItemId(item_id),
        user_label,
        trigger,
        why_it_matters,
        effect,
        unlock,
        dependencies,
        artifacts,
    )


def scope_dependency(position: int, dependency_id: str) -> ScopeDependencyValue:
    return ScopeDependencyValue(position, ItemId(dependency_id))


def attempt_record(
    attempt: str,
    item: str,
    state: AttemptState,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    protected_candidate_revision: str | None = None,
) -> AttemptRecordValue:
    return AttemptRecordValue(
        AttemptId(attempt),
        ItemId(item),
        state,
        accepted_scope_revision,
        accepted_scope_digest,
        CandidateId(protected_candidate_revision) if protected_candidate_revision is not None else None,
    )


def scope_anchor(item: str, revision: int, digest: str, scope: ItemScopeValue) -> ScopeAnchorValue:
    return ScopeAnchorValue(ItemId(item), revision, digest, scope)


def proposal_record(proposal: str, revision: str) -> ProposalRecordValue:
    return ProposalRecordValue(ProposalId(proposal), revision)


def attempt_authority(
    attempt: str,
    item: str,
    lease_id: str | None,
    generation: int,
) -> AttemptAuthorityValue:
    return AttemptAuthorityValue(
        AttemptId(attempt),
        ItemId(item),
        LeaseId(lease_id) if lease_id is not None else None,
        generation,
    )


def accept_proposal_input(
    item: str,
    state: AcceptedProposalState,
    next_action: str,
    timing: Timing | None = None,
    depends_on: tuple[str, ...] = (),
) -> AcceptProposalInputValue:
    return AcceptProposalInputValue(
        ItemId(item), state, next_action, timing, tuple(ItemId(value) for value in depends_on)
    )


def defer_input(timing: str, reopen_condition: str) -> DeferInputValue:
    return DeferInputValue(Timing(timing), reopen_condition)


def transfer_coordinator_input(task_id: str, host_id: str) -> TransferCoordinatorInputValue:
    return TransferCoordinatorInputValue(TaskId(task_id), HostId(host_id))
