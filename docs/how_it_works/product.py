from charlie_pinboard.domain import decision_models, work_models

from .model import Box, Connector, Diagram, Guide, Section

WORK_STATE_ROLES: dict[work_models.WorkState, str] = {
    work_models.WorkState.INTAKE: "visible discovery",
    work_models.WorkState.READY: "accepted and schedulable",
    work_models.WorkState.ACTIVE: "currently being attempted",
    work_models.WorkState.PAUSED: "preserved interruption",
    work_models.WorkState.BLOCKED: "waiting on a condition",
    work_models.WorkState.DEFERRED: "saved for a later decision",
    work_models.WorkState.REVIEW: "protected candidate awaiting a decision",
}

ATTEMPT_STATE_ROLES: dict[work_models.AttemptState, str] = {
    work_models.AttemptState.ACTIVE: "worker owns the attempt",
    work_models.AttemptState.PAUSED: "same attempt, temporarily stopped",
    work_models.AttemptState.BLOCKED: "same attempt, named condition",
    work_models.AttemptState.REVIEW: "candidate frozen for review",
    work_models.AttemptState.DONE: "terminal execution record",
}

ACTION_GROUPS: dict[decision_models.ActionKind, str] = {
    decision_models.ActionKind.ACCEPT_CHECKPOINT: "review",
    decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE: "review",
    decision_models.ActionKind.ACCEPT_PROPOSAL: "proposal",
    decision_models.ActionKind.ACTIVATE: "lifecycle",
    decision_models.ActionKind.BLOCK: "lifecycle",
    decision_models.ActionKind.BLOCK_ITEM: "lifecycle",
    decision_models.ActionKind.COMPLETE: "terminal",
    decision_models.ActionKind.CLOSE: "terminal",
    decision_models.ActionKind.CONTINUE: "advisory",
    decision_models.ActionKind.DEFER: "lifecycle",
    decision_models.ActionKind.DISPATCH: "advisory",
    decision_models.ActionKind.INSPECT: "advisory",
    decision_models.ActionKind.MARK_READY: "lifecycle",
    decision_models.ActionKind.MERGE_PROPOSAL: "proposal",
    decision_models.ActionKind.PAUSE: "lifecycle",
    decision_models.ActionKind.REJECT_PROPOSAL: "proposal",
    decision_models.ActionKind.REOPEN: "lifecycle",
    decision_models.ActionKind.REPORT_BLOCKER: "advisory",
    decision_models.ActionKind.RESUME: "lifecycle",
    decision_models.ActionKind.RETURN_FOR_CORRECTION: "review",
    decision_models.ActionKind.RETURN_PROPOSAL: "proposal",
    decision_models.ActionKind.SUBMIT_REVIEW: "review",
    decision_models.ActionKind.TRANSFER_COORDINATOR: "authority",
}


def validate() -> None:
    if set(WORK_STATE_ROLES) != set(work_models.WorkState):
        raise ValueError("product visual must account for every current work state")
    if set(ATTEMPT_STATE_ROLES) != set(work_models.AttemptState):
        raise ValueError("product visual must account for every current attempt state")
    if set(ACTION_GROUPS) != set(decision_models.ActionKind):
        raise ValueError("product visual must disposition every current action")


DIAGRAM = Diagram(
    slug="product",
    title="Work items, attempts, and related facts",
    description=(
        "A work item moves through a visible lifecycle while its execution attempt, accepted scope, authority, and "
        "evidence remain separate related facts."
    ),
    width=1200,
    height=760,
    sections=(
        Section("Work item · main route", "", 28, 38),
        Section("Reversible side states", "", 962, 224),
        Section("Attempt · same execution identity", "", 28, 410),
        Section("Related facts · not another status", "", 28, 580),
    ),
    guides=(
        Guide((212, 34), (1172, 34)),
        Guide((304, 406), (1172, 406)),
        Guide((300, 576), (1172, 576)),
    ),
    connectors=(
        Connector(((232, 122), (282, 122)), "intake", "ready"),
        Connector(((442, 122), (492, 122)), "ready", "active"),
        Connector(((672, 122), (732, 122)), "active", "review"),
        Connector(((927, 122), (982, 122)), "review", "done"),
        Connector(((152, 169), (152, 248)), "intake", "deferred", "defer", (178, 214)),
        Connector(((552, 169), (552, 248)), "active", "paused", "pause", (578, 214)),
        Connector(((232, 480), (282, 480)), "attempt-active", "attempt-paused"),
        Connector(((442, 480), (492, 480)), "attempt-paused", "attempt-blocked"),
        Connector(((672, 480), (742, 480)), "attempt-blocked", "attempt-review"),
        Connector(((902, 480), (962, 480)), "attempt-review", "attempt-done"),
    ),
    boxes=(
        Box("intake", "Intake", "Visible finding", (), ("WorkState.INTAKE",), 72, 78, 160, 91),
        Box("ready", "Ready", "Accepted work", (), ("WorkState.READY",), 282, 78, 160, 91),
        Box("active", "Active", "Attempt underway", (), ("WorkState.ACTIVE",), 492, 78, 180, 91),
        Box("review", "Review", "Candidate protected", (), ("return · correction",), 732, 78, 195, 91),
        Box("done", "Terminal", "Done", (), ("accepted outcome",), 982, 78, 150, 91, "muted"),
        Box(
            "deferred",
            "Deferred",
            "Saved for later",
            ("entered · defer", "returns via · reopen"),
            (),
            72,
            248,
            190,
            105,
            "muted",
        ),
        Box(
            "paused",
            "Paused",
            "Safe interruption",
            ("entered · pause", "returns via · resume"),
            (),
            472,
            248,
            190,
            105,
            "muted",
        ),
        Box(
            "blocked",
            "Blocked",
            "Named condition",
            ("entered · block", "returns via · resume"),
            (),
            762,
            248,
            200,
            105,
            "muted",
        ),
        Box("attempt-active", "", "Active", ("worker owns attempt",), ("AttemptState.ACTIVE",), 72, 440, 160, 80),
        Box("attempt-paused", "", "Paused", ("identity retained",), ("AttemptState.PAUSED",), 282, 440, 160, 80),
        Box("attempt-blocked", "", "Blocked", ("condition retained",), ("AttemptState.BLOCKED",), 492, 440, 180, 80),
        Box("attempt-review", "", "Review", ("candidate bound",), ("AttemptState.REVIEW",), 742, 440, 160, 80),
        Box("attempt-done", "", "Done", ("execution record",), ("AttemptState.DONE",), 962, 440, 160, 80),
        Box("proposal", "Proposal", "Why the work exists", ("trigger · evidence · consequence",), (), 72, 610, 220, 96),
        Box("scope", "Accepted scope", "What was authorized", ("revision bound to an attempt",), (), 338, 610, 220, 96),
        Box("authority", "Authority", "Who may change it", ("coordination · attempt lease",), (), 604, 610, 220, 96),
        Box(
            "evidence",
            "Candidate + evidence",
            "What review can accept",
            ("protected result · durable references",),
            (),
            870,
            610,
            250,
            96,
        ),
    ),
)
