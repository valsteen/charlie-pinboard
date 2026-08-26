from datetime import UTC, datetime, timedelta

from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AuthorityRecords,
    CanonicalJson,
    ItemArtifactLink,
    ItemDependency,
    ItemScopeRevision,
    LifecycleRecords,
    ProjectRecord,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.authority_decisions import AttemptLeaseStatus
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    AttemptState,
    CoordinationLeaseStatus,
    ProposalRelationKind,
    Timing,
)

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

SQLITE_NOW = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
SQLITE_DIGEST = "a" * 64


def _stored_item(
    item_id: ItemId,
    state: StoredWorkItemState,
    *,
    outcome_evidence: str | None = None,
    sparse: bool = False,
) -> StoredWorkItem:
    return StoredWorkItem(
        item_id,
        f"Work {item_id}",
        state,
        None if sparse else Timing.MUST_NOW,
        None if sparse else "accepted requirement",
        None if sparse else "A verified observation",
        None if sparse else "The workflow needs this fact.",
        None if sparse else "The state becomes explicit.",
        None if sparse else "The next decision can run.",
        outcome_evidence,
        "continue" if state == StoredWorkItemState.ACTIVE else "activate",
        None if sparse else "Current work remains bounded.",
        1,
        SQLITE_DIGEST,
        7,
        SQLITE_NOW,
        SQLITE_NOW,
    )


def complete_sqlite_state() -> StoredWorkState:
    item_a = ItemId("work-a")
    item_b = ItemId("work-b")
    item_c = ItemId("work-c")
    intake_item = ItemId("intake-work")
    attempt_id = AttemptId("work-a-1")
    attempt_lease_id = LeaseId("attempt-lease-a")
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
            _stored_item(intake_item, StoredWorkItemState.INTAKE, sparse=True),
            _stored_item(item_a, StoredWorkItemState.ACTIVE),
            _stored_item(item_b, StoredWorkItemState.SUPERSEDED, outcome_evidence="work-b superseded"),
            _stored_item(item_c, StoredWorkItemState.READY),
        ),
        tuple(
            ItemScopeRevision(item, 1, SQLITE_DIGEST, 3, SQLITE_NOW) for item in (intake_item, item_a, item_b, item_c)
        ),
        (ItemDependency(item_a, item_c, 0),),
        (ItemArtifactLink(item_a, design.artifact_ref_id, ArtifactRole.DESIGN, 0),),
        (
            StoredAttempt(
                attempt_id,
                item_a,
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
            ),
        ),
    )
    proposal_id = ProposalId("proposal-a")
    proposals = ProposalRecords(
        (
            StoredProposal(
                proposal_id,
                SQLITE_NOW,
                SQLITE_NOW,
                TaskId("source-task"),
                "Proposal A",
                "A related observation",
                "It may affect work C.",
                ProposalRelationKind.FOLLOW_UP,
                item_c,
                "Record the follow-up.",
                "A later coordinator can assess it.",
                "No immediate scheduling impact.",
                None,
                None,
                None,
                4,
                None,
            ),
        ),
        (ProposalEvidence(proposal_id, 0, "evidence:observation"),),
        (ProposalFreshness(proposal_id, 0, "Work C remains live."),),
    )
    authority = AuthorityRecords(
        StoredCoordinationLease(
            LeaseId("coordination-a"),
            TaskId("coordinator"),
            HostId("host-a"),
            9,
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=5),
            CoordinationLeaseStatus.ACTIVE,
        ),
        (AttemptLeaseCounter(attempt_id, 3),),
        (AttemptLeaseGeneration(attempt_id, 3, attempt_lease_id, TaskId("worker"), HostId("host-a")),),
        (StoredAttemptLease(attempt_id, 3, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=5), AttemptLeaseStatus.ACTIVE),),
    )
    transition_receipts = (
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
    return StoredWorkState(
        lifecycle,
        proposals,
        (brief, design, evidence),
        authority,
        transition_receipts,
        StoredFocus(item_a, attempt_id, "continue", 6),
    )
