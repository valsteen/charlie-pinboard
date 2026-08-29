from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.mutations import project_transition_mutation
from charlie_pinboard.application.service import execute
from charlie_pinboard.domain import decision_models
from charlie_pinboard.interfaces.cli_commands import TransitionCommand

from .model import Box, Connector, Diagram, Guide, Note, Section

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "TransitionCommand": TransitionCommand.__name__,
    "SubmitReviewCommand": decision_models.SubmitReviewCommand.__name__,
    "execute": execute.__name__,
    "ReviewSubmissionChange": decision_models.ReviewSubmissionChange.__name__,
    "project_transition_mutation": project_transition_mutation.__name__,
    "SQLiteWorkStore": SQLiteWorkStore.__name__,
    "write": SQLiteWorkStore.write.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"journey visual references renamed source symbols: {', '.join(renamed)}")


DIAGRAM = Diagram(
    slug="journey",
    title="A review submission through four layers",
    description=(
        "A submit-review request is decoded, rechecked against current authority, decided in the domain, projected "
        "into a stored mutation, committed atomically, and returned as a durable receipt."
    ),
    width=1200,
    height=820,
    sections=(
        Section("Interface", "decode / render", 28, 118),
        Section("Application", "coordinate / project", 28, 298),
        Section("Domain", "decide / reject", 28, 478),
        Section("Adapter", "commit / reload", 28, 658),
    ),
    guides=(
        Guide((150, 48), (150, 764)),
        Guide((24, 218), (1176, 218)),
        Guide((24, 398), (1176, 398)),
        Guide((24, 578), (1176, 578)),
    ),
    connectors=(
        Connector(((320, 130), (360, 130)), "request", "command"),
        Connector(((465, 172), (465, 214), (530, 214), (530, 244)), "command", "use-case"),
        Connector(((530, 620), (530, 346)), "stored-facts", "use-case", "current snapshot", (594, 566)),
        Connector(((570, 346), (570, 388), (690, 388), (690, 430)), "use-case", "decision"),
        Connector(((640, 472), (590, 472)), "decision", "rejection"),
        Connector(((740, 430), (740, 294), (800, 294)), "decision", "mutation"),
        Connector(((900, 346), (900, 620)), "mutation", "transaction", "atomic change", (952, 554)),
        Connector(((1000, 671), (1020, 671)), "transaction", "fresh-read"),
        Connector(((1100, 620), (1100, 172)), "fresh-read", "result"),
    ),
    boxes=(
        Box("request", "Request", "submit-review", (), ("CLI / JSON",), 170, 88, 150, 84, "muted"),
        Box("command", "Exact command", "Fields decoded once", (), ("TransitionCommand",), 360, 88, 210, 84),
        Box("result", "Durable result", "Receipt + view", (), (), 1010, 88, 180, 84),
        Box(
            "use-case",
            "Locked use case",
            "Recheck action",
            ("against current state",),
            ("service.execute",),
            440,
            244,
            180,
            102,
        ),
        Box(
            "mutation",
            "Stored mutation",
            "One closed change",
            ("ready for persistence",),
            ("project_transition_mutation",),
            800,
            244,
            200,
            102,
        ),
        Box("rejection", "Rejection", "Explicit failure", (), (), 420, 434, 170, 76, "muted"),
        Box(
            "decision",
            "Legal decision",
            "Active → review",
            ("candidate protected",),
            ("ReviewSubmissionChange",),
            640,
            430,
            190,
            102,
        ),
        Box(
            "stored-facts",
            "Stored facts",
            "Snapshot",
            ("revision · scope · authority",),
            (),
            440,
            620,
            180,
            102,
        ),
        Box(
            "transaction",
            "Transaction",
            "Commit together",
            ("or change nothing",),
            ("SQLiteWorkStore.write",),
            800,
            620,
            200,
            102,
        ),
        Box("fresh-read", "Fresh read", "Reload view", ("committed facts",), (), 1020, 620, 160, 102),
    ),
    notes=(
        Note(
            "Rows identify owners · progress generally moves right · vertical movement crosses a boundary · the long return carries the committed result.",
            190,
            783,
            11,
        ),
    ),
)
