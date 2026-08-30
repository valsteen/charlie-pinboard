from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]
type Identity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type TimingPayload = Literal["must-now", "cheaper-now", "safe-to-defer"]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"^[0-9a-f]{64}$")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


class EmptyInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ResumeInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)] | None = None


class StoredActivateInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt: Identity
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner: NonEmptyLine
    brief_artifact_ref_id: Annotated[int, msgspec.Meta(ge=1)]


class SubmitReviewInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine


class ReasonInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine


class BlockInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reason: NonEmptyLine
    depends_on: tuple[Identity, ...] = ()


class EvidenceInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    evidence: NonEmptyLine


class AcceptCheckpointInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    checkpoint: Identity
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class AcceptReviewAndContinueInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    candidate: NonEmptyLine
    evidence: NonEmptyLine


class CloseInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    outcome: work_models.CloseOutcome
    reason: NonEmptyLine


class DeferInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    timing: TimingPayload
    reopen_condition: NonEmptyLine


class AcceptProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    item: Identity
    state: work_models.AcceptedProposalState
    next_action: NonEmptyLine
    timing: TimingPayload | None = None
    depends_on: tuple[Identity, ...] = ()


class MergeProposalInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target: Identity


class TransferCoordinatorInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: NonEmptyLine
    host_id: NonEmptyLine


class WorkItemDefinitionPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-item-definition/v1"]
    title: NonEmptyLine
    objective: NonEmptyLine
    hypothesis: NonEmptyLine
    evidence: tuple[NonEmptyLine, ...]
    scope: Annotated[tuple[NonEmptyLine, ...], msgspec.Meta(min_length=1)]
    non_scope: tuple[NonEmptyLine, ...]
    acceptance_criteria: Annotated[tuple[NonEmptyLine, ...], msgspec.Meta(min_length=1)]
    dependencies: tuple[Identity, ...]
    effect: NonEmptyLine
    unlock: NonEmptyLine


class ReviseItemInputPayload(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-revision/v1"]
    item_id: Identity
    expected_revision: PositiveInt
    expected_digest: Sha256
    source_task: NonEmptyLine
    reason: NonEmptyLine
    definition: WorkItemDefinitionPayload


type InputPayload = (
    EmptyInputPayload
    | ResumeInputPayload
    | StoredActivateInputPayload
    | SubmitReviewInputPayload
    | ReasonInputPayload
    | BlockInputPayload
    | EvidenceInputPayload
    | AcceptCheckpointInputPayload
    | AcceptReviewAndContinueInputPayload
    | CloseInputPayload
    | DeferInputPayload
    | AcceptProposalInputPayload
    | MergeProposalInputPayload
    | TransferCoordinatorInputPayload
    | ReviseItemInputPayload
)
type InputModel = type[InputPayload]


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


class CoordinatedTransitionView(msgspec.Struct, frozen=True):
    action_id: str
    revision: str


class ItemRevisionView(msgspec.Struct, frozen=True):
    item_id: str
    definition_revision: int
    definition_digest: str
    project_revision: str
