"""Installed read-only work-inspection presentation records."""

import msgspec


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


class DependencyReasonView(msgspec.Struct, frozen=True):
    item_id: str
    reason: str


class ReviewFlagView(msgspec.Struct, frozen=True):
    kind: str
    related_item: str | None
    reason: str


class PreparationView(msgspec.Struct, frozen=True):
    definition_revision: int
    definition_digest: str
    task_id: str
    host_id: str
    lease_id: str
    generation: int
    expires_at: str
    status: str


class OverviewItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    position: int
    eligible: bool
    timing: str | None
    depends_on: tuple[str, ...]
    dependency_reasons: tuple[DependencyReasonView, ...]
    review_flags: tuple[ReviewFlagView, ...]
    attempt_id: str | None
    next_action: str | None
    notes: str
    preparation: PreparationView | None


class OverviewView(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItemView, ...]
    immediate_options: tuple[str, ...]


class ActionSemanticsView(msgspec.Struct, frozen=True):
    use_case: str
    effect: str
    permitted_roles: tuple[str, ...]
    subject_kind: str
    lifecycle_precondition: str
    practical_result: str


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    semantics: ActionSemanticsView
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
    semantics: ActionSemanticsView
    input_contract: InputContractView | None = None


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView, ...]


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
