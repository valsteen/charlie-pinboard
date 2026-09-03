from pinboard.application import handover as application_handover
from pinboard.application import stored_state
from pinboard.interfaces import cli_commands, project_handover

from .model import Box, Connector, Diagram, Guide, Note, Section

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "StoredWorkState": stored_state.StoredWorkState.__name__,
    "HandoverCommand": cli_commands.HandoverCommand.__name__,
    "export": project_handover.export.__name__,
    "ProjectHandover": application_handover.ProjectHandover.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"handover visual references renamed source symbols: {', '.join(renamed)}")


DIAGRAM = Diagram(
    slug="handover",
    title="One complete snapshot crosses a read-only boundary",
    description=(
        "The handover command reads one complete SQLite snapshot, verifies every accepted artifact, and emits one "
        "revision-stamped portable JSON package. A human or another tool decides how to use it; export changes no "
        "Pinboard state and writes to no receiving system."
    ),
    width=1200,
    height=560,
    sections=(
        Section("Pinboard authority", "current state and accepted evidence", 28, 54),
        Section("Read-only handover", "complete before any output", 420, 54),
        Section("Portable boundary", "the recipient owns the next step", 760, 54),
    ),
    guides=(
        Guide((390, 42), (390, 470)),
        Guide((730, 42), (730, 470)),
    ),
    connectors=(
        Connector(((300, 200), (420, 200)), "ledger", "validation", "complete snapshot", (360, 186)),
        Connector(((300, 390), (350, 390), (350, 280), (420, 280)), "artifacts", "validation", "verify", (330, 338)),
        Connector(((670, 235), (740, 235)), "validation", "package", "emit", (705, 221)),
        Connector(((970, 235), (1010, 235)), "package", "consumer", "use", (990, 221)),
    ),
    boxes=(
        Box(
            "ledger",
            "One stored revision",
            "SQLite ledger",
            ("work · proposals · decisions", "relationships · current ownership"),
            ("StoredWorkState",),
            50,
            120,
            250,
            120,
        ),
        Box(
            "artifacts",
            "Accepted evidence",
            "Immutable artifacts",
            ("briefs · results · reviews",),
            ("verified exact bytes",),
            50,
            340,
            250,
            100,
        ),
        Box(
            "validation",
            "Read-only command",
            "Validate one snapshot",
            ("load all stored facts", "verify every artifact"),
            ("pinboard handover --json",),
            420,
            170,
            250,
            130,
        ),
        Box(
            "package",
            "Portable output",
            "One JSON package",
            ("revision-stamped", "tool-neutral"),
            ("pinboard-project-handover/v1",),
            740,
            170,
            230,
            130,
        ),
        Box(
            "consumer",
            "User-selected",
            "Team tool",
            ("human or LLM maps", "the complete package"),
            (),
            1010,
            170,
            160,
            130,
            "muted",
        ),
    ),
    notes=(Note("NO LIFECYCLE CHANGE · NO REMOTE WRITE · NO TOOL CHOSEN BY PINBOARD", 600, 505, 11, "middle", True),),
)
