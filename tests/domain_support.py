from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from typing import Any  # noqa: TID251 - fixture corruption intentionally crosses the typed boundary

from charlie_pinboard.domain import decision_models, work_models
from charlie_pinboard.domain.errors import DecisionFailure, DecisionResult
from charlie_pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    ProposalId,
    SubjectId,
    TaskId,
)


def replace(instance: Any, **changes: Any) -> Any:  # noqa: ANN401
    """Create valid variants and deliberately malformed values for rejection tests."""
    return dataclass_replace(instance, **changes)


def expect_success[T](value: DecisionResult[T]) -> T:
    if isinstance(value, DecisionFailure):
        raise AssertionError(f"Expected success, received {value.code.value}: {value.message}")
    return value


def action[SubjectT: SubjectId, ActionT](
    constructor: Callable[[decision_models.ActionCapability[SubjectT]], ActionT],
    subject: SubjectT,
) -> ActionT:
    return constructor(decision_models.ActionCapability(subject, "test action", "rev", 1))


def item_scope(
    item_id: str,
    user_label: str,
    trigger: str | None,
    why_it_matters: str | None,
    effect: str | None,
    unlock: str | None,
    dependencies: tuple[work_models.ScopeDependency, ...] = (),
    artifacts: tuple[work_models.ScopeArtifact, ...] = (),
) -> work_models.ItemScope:
    return work_models.ItemScope(
        ItemId(item_id),
        user_label,
        trigger,
        why_it_matters,
        effect,
        unlock,
        dependencies,
        artifacts,
    )


def scope_dependency(position: int, dependency_id: str) -> work_models.ScopeDependency:
    return work_models.ScopeDependency(position, ItemId(dependency_id))


def attempt_record(
    attempt: str,
    item: str,
    state: work_models.AttemptState,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    protected_candidate_revision: str | None = None,
) -> work_models.AttemptRecord:
    return work_models.AttemptRecord(
        AttemptId(attempt),
        ItemId(item),
        state,
        accepted_scope_revision,
        accepted_scope_digest,
        CandidateId(protected_candidate_revision) if protected_candidate_revision is not None else None,
    )


def scope_anchor(item: str, revision: int, digest: str, scope: work_models.ItemScope) -> work_models.ScopeAnchor:
    return work_models.ScopeAnchor(ItemId(item), revision, digest, scope)


def proposal_record(proposal: str, revision: str) -> work_models.ProposalRecord:
    return work_models.ProposalRecord(ProposalId(proposal), revision)


def accept_proposal_input(
    item: str,
    state: work_models.AcceptedProposalState,
    next_action: str,
    timing: work_models.Timing | None = None,
    depends_on: tuple[str, ...] = (),
) -> work_models.AcceptProposalInput:
    return work_models.AcceptProposalInput(
        ItemId(item), state, next_action, timing, tuple(ItemId(value) for value in depends_on)
    )


def defer_input(timing: str, reopen_condition: str) -> work_models.DeferInput:
    return work_models.DeferInput(work_models.Timing(timing), reopen_condition)


def transfer_coordinator_input(task_id: str, host_id: str) -> work_models.TransferCoordinatorInput:
    return work_models.TransferCoordinatorInput(TaskId(task_id), HostId(host_id))
