from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ActionId, AttemptId, HostId, ItemId, LeaseId, ReviewId, TaskId

_STABLE_ID = msgspec.Meta(min_length=1, pattern=r"^(?!\.{1,2}$)[^/\x00]+$")
type StableActionId = Annotated[ActionId, _STABLE_ID]
type StableAttemptId = Annotated[AttemptId, _STABLE_ID]
type StableHostId = Annotated[HostId, _STABLE_ID]
type StableItemId = Annotated[ItemId, _STABLE_ID]
type StableLeaseId = Annotated[LeaseId, _STABLE_ID]
type StableTaskId = Annotated[TaskId, _STABLE_ID]


class CliRoute(Enum):
    ROOT = "root"
    VALIDATE = "validate"
    STATUS = "status"
    OVERVIEW = "overview"
    ITEM_STATUS = "item-status"
    ITEM_DEFINITION = "item-definition"
    ITEM_DEFINITION_HISTORY = "item-definition-history"
    ITEM_REVISE = "item-revise"
    CLOSE = "close"
    ACTIONS = "actions"
    INPUT_CONTRACT = "input-contract"
    BRIEF_SOURCES = "brief-sources"
    BRIEF_PUBLISH = "brief-publish"
    INITIALIZE = "initialize"
    PROPOSAL = "proposal"
    TRANSITION = "transition"
    DISPATCH = "dispatch"
    COORDINATION_APPLY = "coordination-apply"
    COORDINATION_ACQUIRE = "coordination-acquire"
    COORDINATION_RENEW = "coordination-renew"
    COORDINATION_RELEASE = "coordination-release"
    COORDINATION_REVOKE = "coordination-revoke"
    COORDINATION_STATUS = "coordination-status"
    ATTEMPT_ACQUIRE = "attempt-acquire"
    ATTEMPT_RENEW = "attempt-renew"
    ATTEMPT_RELEASE = "attempt-release"
    ATTEMPT_REVOKE = "attempt-revoke"
    ATTEMPT_STATUS = "attempt-status"
    PARALLEL_PREVIEW = "parallel-preview"
    REBUILD_VIEWS = "rebuild-views"


class RootSelection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: Path | None = None
    work_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRoots:
    source_checkout: Path
    shared_repository: Path
    work: Path
    explicit_work_root: bool


class RootCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ValidateCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class StatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class OverviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class ItemStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    json: bool = False


class ItemReviseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int = 60
    json: bool = False


class ItemDefinitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    json: bool = False


class ItemDefinitionHistoryCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    limit: int = 20
    before_revision: int | None = None
    json: bool = False


class CloseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    outcome: work_models.CloseOutcome
    reason: str
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int = 60
    json: bool = False


class ActionsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    action_id: StableActionId | None = None
    json: bool = False


class LeasedActionsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    lease_id: StableLeaseId
    generation: int
    action_id: StableActionId | None = None
    json: bool = False


type ActionQueryCommand = ActionsCommand | LeasedActionsCommand


class InputContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: decision_models.ActionKind
    json: bool = False


class BriefSourcesPlanCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    max_batch_bytes: int = 24_000


class BriefSourcesEmitCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    emit_batch: int
    max_batch_bytes: int = 24_000


class BriefPublishCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    json: bool = False


class InitializeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ProposalCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path


class CoordinatorTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    payload: Path
    subject_revision: str | None = None


class CoordinationTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    payload: Path
    lease_id: StableLeaseId
    subject_revision: str | None = None


class AttemptTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    payload: Path
    lease_id: StableLeaseId
    subject_revision: str | None = None


type TransitionCommand = CoordinatorTransitionCommand | CoordinationTransitionCommand | AttemptTransitionCommand


class CoordinatorDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    checkpoint: str
    environment: Path
    prompt: Path | None = None


class CoordinatorReviewedDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    checkpoint: str
    environment: Path
    brief_review: Path
    review_id: ReviewId
    prompt: Path | None = None


class CoordinationDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    lease_id: StableLeaseId
    checkpoint: str
    environment: Path
    prompt: Path | None = None


class CoordinationReviewedDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    expected_revision: str
    generation: int
    lease_id: StableLeaseId
    checkpoint: str
    environment: Path
    brief_review: Path
    review_id: ReviewId
    prompt: Path | None = None


type DispatchCommand = (
    CoordinatorDispatchCommand
    | CoordinatorReviewedDispatchCommand
    | CoordinationDispatchCommand
    | CoordinationReviewedDispatchCommand
)


class CoordinationApplyCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: StableTaskId
    host_id: StableHostId
    action_id: StableActionId
    payload: Path
    ttl_seconds: int = 60
    json: bool = False


class CoordinationAcquireCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    json: bool = False


class CoordinationRenewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    lease_id: StableLeaseId
    generation: int
    ttl_seconds: int
    json: bool = False


class CoordinationReleaseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    lease_id: StableLeaseId
    generation: int
    json: bool = False


class CoordinationRevokeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class CoordinationStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


type CoordinationCommand = (
    CoordinationApplyCommand
    | CoordinationAcquireCommand
    | CoordinationRenewCommand
    | CoordinationReleaseCommand
    | CoordinationRevokeCommand
    | CoordinationStatusCommand
)


class AttemptAcquireCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    json: bool = False


class CoordinatedAttemptAcquireCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    coordination_lease_id: StableLeaseId
    coordination_generation: int
    json: bool = False


class AttemptRenewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    ttl_seconds: int
    json: bool = False


class AttemptReleaseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    json: bool = False


class AttemptRevokeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    coordination_lease_id: StableLeaseId
    coordination_generation: int
    json: bool = False


class AttemptStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    json: bool = False


type AttemptCommand = (
    AttemptAcquireCommand
    | CoordinatedAttemptAcquireCommand
    | AttemptRenewCommand
    | AttemptReleaseCommand
    | AttemptRevokeCommand
    | AttemptStatusCommand
)


class ParallelPreviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item: list[StableItemId] = []
    json: bool = False


class RebuildViewsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


type CliCommand = (
    RootCommand
    | ValidateCommand
    | StatusCommand
    | OverviewCommand
    | ItemStatusCommand
    | ItemReviseCommand
    | ItemDefinitionCommand
    | ItemDefinitionHistoryCommand
    | CloseCommand
    | ActionQueryCommand
    | InputContractCommand
    | BriefSourcesPlanCommand
    | BriefSourcesEmitCommand
    | BriefPublishCommand
    | InitializeCommand
    | ProposalCommand
    | TransitionCommand
    | DispatchCommand
    | CoordinationCommand
    | AttemptCommand
    | ParallelPreviewCommand
    | RebuildViewsCommand
)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    roots: RootSelection
    command: CliCommand
