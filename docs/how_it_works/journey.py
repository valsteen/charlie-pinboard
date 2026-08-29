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
        "A submit-review request is decoded, rechecked against current mutation ownership, decided in the domain, "
        "projected into a stored mutation, committed atomically, and returned as a durable receipt."
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
        Connector(((600, 620), (600, 346)), "stored-facts", "use-case", "snapshot", (640, 566)),
        Connector(((570, 346), (570, 388), (700, 388), (700, 430)), "use-case", "decision"),
        Connector(((650, 472), (560, 472)), "decision", "rejection", "rejected", (605, 458)),
        Connector(((750, 430), (750, 294), (800, 294)), "decision", "mutation", "accepted", (710, 356)),
        Connector(((870, 346), (870, 620)), "mutation", "transaction", "commit", (840, 554)),
        Connector(
            ((900, 620), (900, 550), (1100, 550), (1100, 172)),
            "transaction",
            "result",
            "receipt",
            (1000, 538),
        ),
        Connector(((950, 680), (1000, 680)), "transaction", "views", "refresh", (975, 668)),
    ),
    boxes=(
        Box("request", "Request", "submit-review", (), ("CLI / JSON",), 170, 88, 150, 84, "muted"),
        Box("command", "Exact command", "Fields decoded once", (), ("TransitionCommand",), 360, 88, 210, 84),
        Box("result", "Applied result", "Receipt + revision", (), (), 1010, 88, 180, 84),
        Box(
            "use-case",
            "Write operation",
            "Read current state",
            ("inside one transaction",),
            ("service.execute",),
            440,
            244,
            180,
            102,
        ),
        Box(
            "mutation",
            "Stored mutation",
            "Complete stored change",
            ("before + after + receipt",),
            ("project_transition_mutation",),
            800,
            244,
            240,
            102,
        ),
        Box(
            "rejection",
            "Expected rejection",
            "No stored change",
            ("reason returned to caller",),
            (),
            350,
            430,
            210,
            102,
            "muted",
        ),
        Box(
            "decision",
            "Domain evaluation",
            "Is submission legal?",
            ("change or expected rejection",),
            ("decide",),
            650,
            430,
            200,
            102,
        ),
        Box(
            "stored-facts",
            "Transaction snapshot",
            "Stored facts",
            ("revision · scope · lease",),
            (),
            440,
            620,
            190,
            102,
        ),
        Box(
            "transaction",
            "Transaction",
            "Commit together",
            ("or change nothing",),
            ("SQLiteWorkStore.write",),
            760,
            620,
            190,
            102,
        ),
        Box(
            "views",
            "Generated views",
            "Refresh projection",
            ("after commit · repairable",),
            (),
            1000,
            620,
            180,
            102,
            "muted",
        ),
    ),
    notes=(
        Note(
            "An expected rejection skips mutation and commit. View refresh happens after the accepted transaction and can be rebuilt.",
            190,
            783,
            11,
        ),
    ),
)
