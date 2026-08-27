import argparse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import msgspec


class CommandName(Enum):
    ROOT = "root"
    VALIDATE = "validate"
    STATUS = "status"
    OVERVIEW = "overview"
    ITEM = "item"
    CLOSE = "close"
    ACTIONS = "actions"
    INPUT_CONTRACT = "input-contract"
    BRIEF_SOURCES = "brief-sources"
    BRIEF = "brief"
    INIT = "init"
    PROPOSAL = "proposal"
    TRANSITION = "transition"
    DISPATCH = "dispatch"
    COORDINATION = "coordination"
    ATTEMPT = "attempt"
    PARALLEL = "parallel"
    VIEWS = "views"


class CoordinationOperation(Enum):
    APPLY = "apply"
    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    STATUS = "status"


class AttemptOperation(Enum):
    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    STATUS = "status"


class ItemOperation(Enum):
    STATUS = "status"


class BriefOperation(Enum):
    PUBLISH = "publish"


class CliArguments(argparse.Namespace):
    command: str
    project_root: Path | None
    work_root: Path | None
    json: bool
    role: str
    host_id: str
    file: Path
    action_id: str
    action_kind: str
    expected_revision: str
    generation: int
    subject_revision: str | None
    payload: Path
    checkpoint: str
    environment: Path
    prompt: Path | None
    brief_review: Path | None
    review_id: str | None
    operation: str
    lease_id: str | None
    authorization: str
    task_id: str
    ttl_seconds: int
    attempt_id: str
    coordination_lease_id: str
    coordination_generation: int
    item: list[str]
    item_id: str
    outcome: str
    reason: str
    max_batch_bytes: int
    emit_batch: int | None


class BriefPublicationView(msgspec.Struct, frozen=True):
    artifact_ref_id: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int


@dataclass(frozen=True, slots=True)
class CommandContext:
    arguments: CliArguments
    project: Path
    work: Path


class RootView(msgspec.Struct, frozen=True):
    project_root: str
    work_root: str


class DiagnosticView(msgspec.Struct, frozen=True):
    code: str
    severity: str
    path: str
    message: str
    hint: str | None


class ValidationView(msgspec.Struct, frozen=True):
    valid: bool
    diagnostics: tuple[DiagnosticView, ...]


class CoordinatorView(msgspec.Struct, frozen=True):
    task_id: str
    host_id: str
    generation: int
    lease_id: str
    expires_at: str
    status: str


class StatusView(msgspec.Struct, frozen=True):
    valid: bool
    project_root: str
    work_root: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    next_action: str
    counts: dict[str, int]
    inbox_count: int
    coordinator: CoordinatorView | None
    authority: str = "v1"


class OverviewItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    timing: str | None
    depends_on: tuple[str, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str


class OverviewView(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItemView, ...]
    inbox: tuple[str, ...]
    immediate_options: tuple[str, ...]


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    payload_schema: msgspec.Raw


class BriefSourceSegmentView(msgspec.Struct, frozen=True):
    authority_id: str
    selector: str
    index: int
    start_line: int
    end_line: int
    content_byte_count: int
    content_sha256: str


class BriefSourceView(msgspec.Struct, frozen=True):
    authority_id: str
    selector: str
    families: tuple[str, ...]
    selected_sha256: str
    selected_byte_count: int
    start_line: int
    end_line: int
    whole_file: bool
    segments: tuple[BriefSourceSegmentView, ...]


class BriefSourceBatchView(msgspec.Struct, frozen=True):
    index: int
    content_byte_count: int
    estimated_rendered_byte_count: int
    segments: tuple[BriefSourceSegmentView, ...]


class BriefSourcePlanView(msgspec.Struct, frozen=True):
    schema: str
    manifest_sha256: str
    max_batch_bytes: int
    sources: tuple[BriefSourceView, ...]
    batches: tuple[BriefSourceBatchView, ...]


class ActionView(msgspec.Struct, frozen=True, omit_defaults=True):
    action_id: str
    kind: str
    subject: str
    label: str
    expected_revision: str
    coordinator_generation: int
    subject_revision: str
    authorization: str
    lease_id: str
    input_contract: InputContractView | None = None


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView, ...]


class CoordinatedTransitionView(msgspec.Struct, frozen=True):
    action_id: str
    revision: str


class ParallelReasonView(msgspec.Struct, frozen=True):
    code: str
    message: str


class ParallelItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    outcome: str
    reasons: tuple[ParallelReasonView, ...]


class ParallelPreviewView(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: str
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]
