from pathlib import Path

from .model import Box, Connector, Diagram, Guide, Note, Section

LAYER_DIRECTORIES: dict[str, str] = {
    "interfaces": "Turn outside input into exact commands and exact output.",
    "application": "Sequence complete use cases and project accepted changes into storage mutations.",
    "domain": "Decide what is legal without reading files, issuing SQL, or presenting commands.",
    "adapters": "Store and recover accepted facts without deciding workflow policy.",
}

REQUIRED_ARCHITECTURE_HEADINGS = (
    "## Dependency direction",
    "### Domain",
    "### Application",
    "### Adapters",
    "### Interfaces",
)


def validate(root: Path) -> None:
    package = root / "src" / "charlie_pinboard"
    missing = tuple(name for name in LAYER_DIRECTORIES if not package.joinpath(name).is_dir())
    if missing:
        raise ValueError(f"layer visual references missing package directories: {', '.join(missing)}")
    architecture = root.joinpath("ARCHITECTURE.md").read_text(encoding="utf-8")
    missing_headings = tuple(heading for heading in REQUIRED_ARCHITECTURE_HEADINGS if heading not in architecture)
    if missing_headings:
        raise ValueError(f"layer visual lost its architecture authority: {', '.join(missing_headings)}")


DIAGRAM = Diagram(
    slug="layers",
    title="Four package layers with distinct responsibilities",
    description=(
        "Interfaces make requests exact, application services make operations coherent, the domain makes decisions "
        "legal, and adapters make accepted facts durable."
    ),
    width=1200,
    height=820,
    sections=(
        Section("Outside the package", "callers and durable systems", 28, 38),
        Section("Inside the package", "each layer removes a different ambiguity", 28, 150),
    ),
    guides=(
        Guide((176, 34), (1172, 34)),
        Guide((170, 146), (1172, 146)),
        Guide((180, 310), (1172, 310)),
        Guide((180, 470), (1172, 470)),
    ),
    connectors=(
        Connector(((580, 118), (580, 174)), "inputs", "interfaces", "decode", (618, 151)),
        Connector(((600, 286), (600, 334)), "interfaces", "application", "exact command", (666, 316)),
        Connector(((600, 446), (600, 494)), "application", "domain", "decision facts", (664, 476)),
        Connector(((870, 390), (1010, 390), (1010, 494)), "application", "adapters", "ports", (944, 378), True, False),
        Connector(((900, 550), (870, 550)), "adapters", "domain"),
        Connector(((1070, 606), (1070, 674)), "adapters", "durable-world", "commit / reload", (1124, 646)),
    ),
    boxes=(
        Box("inputs", "Inputs", "CLI · JSON · project files", (), (), 430, 46, 300, 72, "muted"),
        Box(
            "interfaces",
            "Interfaces",
            "Make the request exact",
            ("decode one command · present one result",),
            ("src/charlie_pinboard/interfaces",),
            330,
            174,
            540,
            112,
        ),
        Box(
            "application",
            "Application",
            "Make the operation coherent",
            ("reselect · sequence · project one mutation",),
            ("src/charlie_pinboard/application",),
            330,
            334,
            540,
            112,
        ),
        Box(
            "domain",
            "Domain",
            "Make the change legal",
            ("closed decision or expected rejection",),
            ("src/charlie_pinboard/domain",),
            330,
            494,
            540,
            112,
        ),
        Box(
            "adapters",
            "Adapters",
            "Persist accepted facts",
            ("SQLite and files implement capabilities",),
            ("src/charlie_pinboard/adapters",),
            900,
            494,
            250,
            112,
        ),
        Box("durable-world", "Durable world", "SQLite · artifacts · views", (), (), 920, 674, 250, 72, "muted"),
    ),
    notes=(
        Note("The arrows show dependency or data direction; the rows show where ambiguity is removed.", 28, 780, 12),
    ),
)
