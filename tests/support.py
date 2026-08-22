import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    ArtifactRecords,
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    AuthorityRecords,
    CanonicalJson,
    CoordinationLeaseState,
    HistoryRecords,
    ItemArtifactLink,
    ItemDependency,
    ItemResourceRequirement,
    ItemScopeRevision,
    LifecycleRecords,
    MutationIntentState,
    OriginKind,
    PlanningObligationState,
    PlanningRecords,
    ProjectRecord,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    ProposalRelation,
    ResourceInstanceLocator,
    ResourceInstanceState,
    ResourceMutationIntent,
    ResourceRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredPlanningImpact,
    StoredPlanningObligation,
    StoredPlanningReplacement,
    StoredProposal,
    StoredReservationCounter,
    StoredResourceDefinition,
    StoredResourceInstance,
    StoredResourceReservation,
    StoredResourceUseLease,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    MutationIntentId,
    PlanningImpactId,
    ProposalId,
    ReservationId,
    ResourceId,
    ResourceInstanceId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    PlanningDisposition,
    ReservationState,
    Timing,
    UseLeaseGenerationKind,
    UseLeaseState,
)
from charlie_pinboard.interfaces.transitions import apply_action as apply_serialized_action
from charlie_pinboard.legacy.actions import Action
from charlie_pinboard.legacy.proposals import create_proposal as create_serialized_proposal
from charlie_pinboard.legacy.transaction_store import CommitFailpoint

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

SQLITE_NOW = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
SQLITE_DIGEST = "a" * 64

QUEUE_TEMPLATE = """\
---
kind: work-queue
schema: repo-work/v1
updated: "2026-08-16"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
{rows}
"""

CURRENT_TEMPLATE = """\
---
kind: work-current
schema: repo-work/v1
updated: "2026-08-16"
focus_item: {focus_item}
focus_attempt: {focus_attempt}
next_action: {next_action}
---

# Current Work
"""

ITEM_TEMPLATE = """\
---
kind: work-item
schema: repo-work/v1
item: {item}
user_label: "{label}"
updated: "2026-08-16"
---

# {label}

## Context arc

Before and trigger. Why it matters. After and trajectory.
"""

ATTEMPT_TEMPLATE = """\
---
kind: work-attempt
schema: repo-work/v1
attempt: {attempt}
item: {item}
state: active
branch: codex/{item}
base_revision: abc123
owner: worker-task
updated: "2026-08-16"
---

# Attempt
"""


def create_state(
    rows: list[str],
    *,
    focus_item: str = "null",
    focus_attempt: str = "null",
    create_active_attempt: bool = False,
) -> tuple[Path, Path]:
    project = Path(tempfile.mkdtemp()).resolve()
    work = project / ".codex" / "work"
    (work / "items").mkdir(parents=True)
    (work / "attempts").mkdir()
    (work / "inbox").mkdir()
    (work / "history" / "items").mkdir(parents=True)
    (work / "queue.md").write_text(QUEUE_TEMPLATE.format(rows="\n".join(rows)), encoding="utf-8")
    (work / "current.md").write_text(
        CURRENT_TEMPLATE.format(
            focus_item=focus_item,
            focus_attempt=focus_attempt,
            next_action="continue" if focus_item != "null" else "select",
        ),
        encoding="utf-8",
    )
    for row in rows:
        item = row.split("|")[1].strip()
        (work / "items" / f"{item}.md").write_text(
            ITEM_TEMPLATE.format(item=item, label=item.replace("-", " ").title()),
            encoding="utf-8",
        )
    (work / "coordinator.json").write_text(
        json.dumps(
            {
                "schema": "repo-work/v1",
                "project_root": str(project),
                "task_id": "coordinator-task",
                "host_id": "local-host",
                "generation": 1,
                "registered_at": "2026-08-16T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if create_active_attempt:
        attempt_dir = work / "attempts" / focus_attempt
        attempt_dir.mkdir()
        (attempt_dir / "attempt.md").write_text(
            ATTEMPT_TEMPLATE.format(attempt=focus_attempt, item=focus_item),
            encoding="utf-8",
        )
    return project, work


def apply_action(
    work: Path,
    project: Path,
    action: Action,
    payload: JsonObject,
    *,
    failpoint: CommitFailpoint | None = None,
) -> None:
    apply_serialized_action(work, project, action, json.dumps(payload), failpoint=failpoint)


def create_proposal(work: Path, project: Path, value: JsonObject) -> Path:
    return create_serialized_proposal(work, project, json.dumps(value))


def _stored_item(
    item_id: ItemId,
    state: StoredWorkItemState,
    *,
    outcome_evidence: str | None = None,
    legacy: bool = False,
) -> StoredWorkItem:
    return StoredWorkItem(
        item_id,
        OriginKind.LEGACY_IMPORT if legacy else OriginKind.NATIVE,
        f"Work {item_id}",
        state,
        None if legacy else Timing.MUST_NOW,
        None if legacy else "accepted requirement",
        None if legacy else "A verified observation",
        None if legacy else "The workflow needs this fact.",
        None if legacy else "The state becomes explicit.",
        None if legacy else "The next decision can run.",
        outcome_evidence,
        "continue" if state == StoredWorkItemState.ACTIVE else "activate",
        None if legacy else "Current work remains bounded.",
        1,
        SQLITE_DIGEST,
        7,
        None if legacy else SQLITE_NOW,
        None if legacy else SQLITE_NOW,
        SQLITE_NOW,
        SQLITE_NOW,
    )


def complete_sqlite_state() -> StoredWorkState:
    item_a = ItemId("work-a")
    item_b = ItemId("work-b")
    item_c = ItemId("work-c")
    legacy_item = ItemId("legacy-work")
    attempt_id = AttemptId("work-a-1")
    impact_id = PlanningImpactId("impact-a")
    resource_id = ResourceId("workspace")
    instance_id = ResourceInstanceId("workspace-on-host")
    retired_instance_id = ResourceInstanceId("retired-workspace-on-host")
    reservation_id = ReservationId("reservation-a")
    attempt_lease_id = LeaseId("attempt-lease-a")
    grant_lease_id = LeaseId("use-grant")
    brief = ArtifactReference(
        ArtifactRefId(1), "work-a-brief", 1, ArtifactKind.BRIEF, "artifacts/brief.md", SQLITE_DIGEST, 100, 3, SQLITE_NOW
    )
    design = ArtifactReference(
        ArtifactRefId(2),
        "work-a-design",
        1,
        ArtifactKind.DESIGN,
        "artifacts/design.md",
        SQLITE_DIGEST,
        200,
        3,
        SQLITE_NOW,
    )
    evidence = ArtifactReference(
        ArtifactRefId(3),
        "work-a-evidence",
        1,
        ArtifactKind.EVIDENCE,
        "artifacts/evidence.md",
        SQLITE_DIGEST,
        50,
        4,
        SQLITE_NOW,
    )
    lifecycle = LifecycleRecords(
        ProjectRecord("charlie-pinboard", 1, 12, 2, SQLITE_NOW, SQLITE_NOW),
        (
            _stored_item(legacy_item, StoredWorkItemState.INTAKE, legacy=True),
            _stored_item(item_a, StoredWorkItemState.ACTIVE),
            _stored_item(item_b, StoredWorkItemState.SUPERSEDED, outcome_evidence="work-b superseded"),
            _stored_item(item_c, StoredWorkItemState.READY),
        ),
        tuple(
            ItemScopeRevision(item, 1, SQLITE_DIGEST, 3, SQLITE_NOW) for item in (legacy_item, item_a, item_b, item_c)
        ),
        (ItemDependency(item_a, item_c, 0),),
        (ItemArtifactLink(item_a, design.artifact_ref_id, ArtifactRole.DESIGN, 0),),
        (
            StoredAttempt(
                attempt_id,
                item_a,
                OriginKind.NATIVE,
                AttemptState.ACTIVE,
                "codex/work-a",
                "base-revision",
                "source-task",
                brief.artifact_ref_id,
                None,
                None,
                None,
                None,
                1,
                SQLITE_DIGEST,
                8,
                SQLITE_NOW,
                SQLITE_NOW,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
        ),
    )
    proposal_id = ProposalId("proposal-a")
    proposals = ProposalRecords(
        (
            StoredProposal(
                proposal_id,
                OriginKind.NATIVE,
                SQLITE_NOW,
                SQLITE_NOW,
                TaskId("source-task"),
                "Proposal A",
                "A related observation",
                "It may affect work C.",
                ProposalRelation.FOLLOW_UP,
                item_c,
                "Record the follow-up.",
                "A later coordinator can assess it.",
                "No immediate scheduling impact.",
                None,
                None,
                None,
                4,
                None,
                None,
            ),
        ),
        (ProposalEvidence(proposal_id, 0, "evidence:observation"),),
        (ProposalFreshness(proposal_id, 0, "Work C remains live."),),
    )
    planning = PlanningRecords(
        (
            StoredPlanningImpact(
                impact_id, item_a, attempt_id, 1, SQLITE_DIGEST, item_b, "Impact", "Evidence", 6, SQLITE_NOW
            ),
        ),
        (
            StoredPlanningObligation(
                impact_id,
                item_b,
                0,
                1,
                SQLITE_DIGEST,
                PlanningObligationState.RESOLVED,
                PlanningDisposition.SUPERSEDED,
                1,
                SQLITE_DIGEST,
                None,
                None,
                item_c,
                "work-b superseded",
                "The accepted replacement owns the outcome.",
                7,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
        ),
        (StoredPlanningReplacement(impact_id, item_b, item_c, 0),),
    )
    authority = AuthorityRecords(
        StoredCoordinationLease(
            LeaseId("coordination-a"),
            TaskId("coordinator"),
            HostId("host-a"),
            9,
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=5),
            CoordinationLeaseState.ACTIVE,
        ),
        (AttemptLeaseCounter(attempt_id, 3),),
        (AttemptLeaseGeneration(attempt_id, 3, attempt_lease_id, TaskId("worker"), HostId("host-a")),),
        (StoredAttemptLease(attempt_id, 3, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=5), AttemptLeaseState.ACTIVE),),
    )
    grant = StoredResourceUseLease(
        reservation_id,
        instance_id,
        1,
        attempt_id,
        HostId("host-a"),
        4,
        2,
        SQLITE_DIGEST,
        TaskId("worker"),
        attempt_lease_id,
        3,
        grant_lease_id,
        1,
        UseLeaseGenerationKind.GRANT,
        2,
        SQLITE_NOW,
        SQLITE_NOW + timedelta(minutes=5),
        UseLeaseState.REVOKED,
    )
    fence = StoredResourceUseLease(
        reservation_id,
        instance_id,
        1,
        attempt_id,
        HostId("host-a"),
        4,
        2,
        SQLITE_DIGEST,
        TaskId("worker"),
        attempt_lease_id,
        3,
        LeaseId("use-fence"),
        2,
        UseLeaseGenerationKind.FENCE,
        2,
        SQLITE_NOW,
        SQLITE_NOW + timedelta(minutes=5),
        UseLeaseState.REVOKED,
    )
    successor = StoredResourceUseLease(
        reservation_id,
        instance_id,
        1,
        attempt_id,
        HostId("host-a"),
        4,
        2,
        SQLITE_DIGEST,
        TaskId("worker"),
        attempt_lease_id,
        3,
        LeaseId("use-successor"),
        3,
        UseLeaseGenerationKind.GRANT,
        2,
        SQLITE_NOW,
        SQLITE_NOW + timedelta(minutes=5),
        UseLeaseState.ACTIVE,
    )
    resources = ResourceRecords(
        (
            StoredResourceDefinition(
                resource_id,
                OriginKind.NATIVE,
                "workspace",
                "One exclusive workspace",
                3,
                SQLITE_NOW,
                SQLITE_NOW,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
        ),
        (ItemResourceRequirement(item_a, resource_id, 0),),
        (
            StoredResourceInstance(
                retired_instance_id,
                resource_id,
                HostId("host-a"),
                "workspace",
                "retired-fingerprint",
                ResourceInstanceState.RETIRED,
                3,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
            StoredResourceInstance(
                instance_id,
                resource_id,
                HostId("host-a"),
                "workspace",
                "fingerprint",
                ResourceInstanceState.ACTIVE,
                4,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
        ),
        (
            ResourceInstanceLocator(
                retired_instance_id,
                HostId("host-a"),
                "workspace/v1",
                CanonicalJson(b"{}"),
                1,
                SQLITE_DIGEST,
                SQLITE_NOW,
            ),
            ResourceInstanceLocator(
                instance_id, HostId("host-a"), "workspace/v1", CanonicalJson(b"{}"), 2, SQLITE_DIGEST, SQLITE_NOW
            ),
        ),
        (StoredReservationCounter(retired_instance_id, 0), StoredReservationCounter(instance_id, 1)),
        (
            StoredResourceReservation(
                reservation_id,
                instance_id,
                resource_id,
                HostId("host-a"),
                1,
                attempt_id,
                item_a,
                ReservationState.ACTIVE,
                5,
                SQLITE_NOW,
                None,
            ),
        ),
        (grant, fence, successor),
        (
            ResourceMutationIntent(
                MutationIntentId("intent-a"),
                reservation_id,
                1,
                instance_id,
                attempt_id,
                HostId("host-a"),
                1,
                grant_lease_id,
                TaskId("worker"),
                attempt_lease_id,
                3,
                4,
                2,
                SQLITE_DIGEST,
                "mutation-policy/v1",
                CanonicalJson(b"{}"),
                SQLITE_DIGEST,
                MutationIntentState.PLANNED,
                SQLITE_NOW,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
    )
    history = HistoryRecords(
        (
            StoredTransitionReceipt(
                HistoryId(1),
                11,
                ActionId("continue:work-a-1"),
                TransitionHistoryActionKind.CONTINUE,
                HistorySubjectId("work-a-1"),
                evidence.artifact_ref_id,
                TransitionHistoryAuthorizationKind.ATTEMPT,
                TaskId("worker"),
                HostId("host-a"),
                "empty/v1",
                CanonicalJson(b"{}"),
                "continued/v1",
                CanonicalJson(b"{}"),
                SQLITE_NOW,
            ),
        )
    )
    return StoredWorkState(
        lifecycle,
        proposals,
        planning,
        ArtifactRecords((brief, design, evidence)),
        authority,
        resources,
        history,
        StoredFocus(item_a, attempt_id, "continue", 6),
    )
