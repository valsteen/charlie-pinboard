"""Installed read-only work-inspection presentation records."""

import msgspec

from pinboard.application import query_models
from pinboard.domain import decision_models


class CoordinatorView(msgspec.Struct, frozen=True):
    task_id: str
    host_id: str
    generation: int
    lease_id: str
    expires_at: str
    status: str


class StatusView(msgspec.Struct, frozen=True):
    valid: bool
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    next_action: str
    counts: dict[str, int]
    intake_item_count: int
    coordinator: CoordinatorView | None
    authority: str = "v1"


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    semantics: decision_models.ActionSemantics
    payload_schema: msgspec.Raw | None


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
    semantics: decision_models.ActionSemantics
    input_contract: InputContractView | None = None


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView, ...]


class ParallelItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    outcome: str
    reasons: tuple[query_models.ParallelReason, ...]


class ParallelPreviewView(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: str
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]
