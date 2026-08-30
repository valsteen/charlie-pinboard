import msgspec


class BriefPublicationView(msgspec.Struct, frozen=True):
    artifact_ref_id: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int


class RootView(msgspec.Struct, frozen=True):
    source_checkout_root: str
    shared_repository_root: str
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
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    next_action: str
    counts: dict[str, int]
    visible_candidate_count: int
    coordinator: CoordinatorView | None
    authority: str = "v1"


class DependencyReasonView(msgspec.Struct, frozen=True):
    item_id: str
    reason: str


class ReviewFlagView(msgspec.Struct, frozen=True):
    kind: str
    related_item: str | None
    reason: str


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


class OverviewView(msgspec.Struct, frozen=True):
    schema: str
    authority: str
    revision: str
    focus_item: str | None
    focus_attempt: str | None
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItemView, ...]
    immediate_options: tuple[str, ...]


class CloseView(msgspec.Struct, frozen=True):
    item_id: str
    outcome: str
    reason: str
    revision: str


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
    semantics: ActionSemanticsView
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
