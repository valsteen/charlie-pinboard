import sqlite3
from pathlib import Path

from .model import Box, Connector, Diagram, Guide, Note, Section

TABLE_GROUPS: dict[str, str] = {
    "work_items": "current work",
    "attempts": "current work",
    "current_focus": "current work",
    "item_scope_revisions": "scope and relationships",
    "item_dependencies": "scope and relationships",
    "proposals": "discovery",
    "proposal_evidence": "discovery",
    "proposal_freshness": "discovery",
    "artifact_refs": "durable knowledge",
    "item_artifacts": "durable knowledge",
    "coordination_lease": "authority",
    "attempt_lease_counters": "authority",
    "attempt_lease_generations": "authority",
    "attempt_leases": "authority",
    "project_meta": "integrity and time",
    "transition_history": "integrity and time",
}

RELATION_ROLES: dict[tuple[str, str], str] = {
    ("attempt_lease_counters", "attempts"): "authority belongs to an attempt",
    ("attempt_lease_generations", "attempt_lease_counters"): "generations never move backwards",
    ("attempt_leases", "attempt_lease_counters"): "one current lease per attempt",
    ("attempt_leases", "attempt_lease_generations"): "lease identity is fenced by generation",
    ("attempts", "artifact_refs"): "brief and result evidence",
    ("attempts", "item_scope_revisions"): "attempt uses one accepted scope",
    ("attempts", "work_items"): "attempt executes one item",
    ("current_focus", "attempts"): "focus may name the live attempt",
    ("current_focus", "work_items"): "focus names the work",
    ("item_artifacts", "artifact_refs"): "item knowledge resolves to immutable bytes",
    ("item_artifacts", "work_items"): "knowledge belongs to an item",
    ("item_dependencies", "work_items"): "items form a dependency graph",
    ("item_scope_revisions", "work_items"): "scope history belongs to an item",
    ("proposal_evidence", "proposals"): "discovery retains its evidence",
    ("proposal_freshness", "proposals"): "discovery retains assumptions",
    ("proposals", "work_items"): "proposal may relate to or resolve as work",
    ("transition_history", "artifact_refs"): "history may retain accepted evidence",
    ("work_items", "item_scope_revisions"): "item points to its current scope",
}


def _schema_shape(root: Path) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        schema = root.joinpath("src/pinboard/adapters/sqlite/schema.sql").read_text(encoding="utf-8")
        connection.executescript(schema)
        tables = frozenset(
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        relations: set[tuple[str, str]] = set()
        for table in tables:
            quoted = table.replace('"', '""')
            for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                relations.add((table, str(row["table"])))
        return tables, frozenset(relations)
    finally:
        connection.close()


def validate(root: Path) -> None:
    tables, relations = _schema_shape(root)
    if set(TABLE_GROUPS) != tables:
        missing = sorted(tables - set(TABLE_GROUPS))
        removed = sorted(set(TABLE_GROUPS) - tables)
        raise ValueError(f"database visual table coverage changed; ungrouped={missing}, removed={removed}")
    if set(RELATION_ROLES) != relations:
        missing = sorted(relations - set(RELATION_ROLES))
        removed = sorted(set(RELATION_ROLES) - relations)
        raise ValueError(f"database visual relationship coverage changed; ungrouped={missing}, removed={removed}")


DIAGRAM = Diagram(
    slug="database",
    title="Six kinds of memory in one relational ledger",
    description=(
        "Sixteen SQLite tables preserve work identity, scope, proposals, artifacts, mutation ownership, and history. "
        "Relationship families are grouped for readability while the source seed accounts for every foreign key."
    ),
    width=1200,
    height=850,
    sections=(
        Section("Discovery", "findings before and after scheduling", 28, 42),
        Section("Current work", "identity that survives execution", 424, 42),
        Section("Scope + relationships", "accepted intent and dependency", 824, 42),
        Section("Accepted files", "brief contracts and ready-review evidence", 28, 432),
        Section("Mutation ownership", "temporary shared authority · independent attempt owners", 424, 432),
        Section("Integrity + time", "current revision and committed receipts", 824, 432),
    ),
    guides=(
        Guide((400, 32), (400, 770)),
        Guide((800, 32), (800, 770)),
        Guide((24, 390), (1176, 390)),
    ),
    connectors=(
        Connector(((270, 155), (430, 155)), "proposals", "work-items", "may become work", (350, 143)),
        Connector(((640, 155), (570, 155)), "attempts", "work-items", "executes", (605, 143)),
        Connector(((780, 155), (830, 155)), "attempts", "scopes", "scope", (805, 143)),
        Connector(((500, 110), (500, 80), (1100, 80), (1100, 110)), "work-items", "dependencies"),
        Connector(
            ((105, 260), (105, 230), (140, 230), (140, 200)), "proposal-evidence", "proposals", "supports", (72, 226)
        ),
        Connector(
            ((275, 260), (275, 230), (220, 230), (220, 200)), "proposal-freshness", "proposals", "recheck", (310, 226)
        ),
        Connector(((540, 270), (540, 200)), "focus", "work-items", "item", (522, 238)),
        Connector(
            ((640, 270), (640, 240), (700, 240), (700, 200)), "focus", "attempts", "optional attempt", (700, 228)
        ),
        Connector(((200, 640), (200, 610)), "item-artifacts", "artifact-refs", "resolves", (235, 628)),
        Connector(
            ((680, 590), (680, 630), (540, 630), (540, 660)),
            "lease-counters",
            "lease-generations",
            "issues",
            (610, 620),
        ),
        Connector(((630, 710), (610, 710)), "attempt-leases", "lease-generations"),
        Connector(((975, 590), (975, 650)), "meta", "history", "each revision", (1017, 620)),
    ),
    boxes=(
        Box("proposals", "", "Proposals", ("possible work",), ("may relate to an item",), 90, 110, 180, 90),
        Box("proposal-evidence", "", "Evidence", ("why it was raised",), ("proposal_evidence",), 40, 260, 150, 90),
        Box("proposal-freshness", "", "Assumptions", ("facts to recheck",), ("proposal_freshness",), 210, 260, 170, 90),
        Box("work-items", "", "Work items", ("durable identity",), ("work_items",), 430, 110, 140, 90),
        Box("attempts", "", "Attempts", ("one execution",), ("attempts",), 640, 110, 140, 90),
        Box("focus", "", "Current focus", ("advisory pointer",), ("item + optional attempt",), 500, 270, 180, 80),
        Box("scopes", "", "Accepted versions", ("current scope",), ("plus its history",), 830, 110, 175, 90),
        Box("dependencies", "", "Dependencies", ("item → prerequisite",), ("item_dependencies",), 1020, 110, 160, 90),
        Box(
            "artifact-refs",
            "",
            "Accepted artifacts",
            ("brief · ready-review evidence", "path · revision · digest"),
            ("artifact_refs",),
            50,
            500,
            300,
            110,
        ),
        Box(
            "item-artifacts",
            "",
            "Item evidence link",
            ("ready review → work item", "role + position"),
            ("item_artifacts",),
            50,
            640,
            300,
            110,
        ),
        Box(
            "coordination",
            "",
            "Shared authority",
            ("any task borrows briefly", "exclusive graph change"),
            ("coordination_lease",),
            415,
            500,
            175,
            110,
        ),
        Box(
            "lease-counters",
            "",
            "Generation counter",
            ("highest ownership epoch",),
            ("attempt_lease_counters",),
            605,
            500,
            185,
            90,
        ),
        Box(
            "lease-generations",
            "",
            "Ownership history",
            ("task + host + lease id",),
            ("attempt_lease_generations",),
            410,
            660,
            200,
            90,
        ),
        Box(
            "attempt-leases",
            "",
            "Attempt lease",
            ("owns implementation",),
            ("generation + expiry", "attempt_leases"),
            630,
            660,
            160,
            100,
        ),
        Box("meta", "", "Project state", ("revision + host epoch",), ("project_meta",), 900, 500, 160, 90),
        Box(
            "history", "", "Committed history", ("input + outcome + actor",), ("transition_history",), 890, 650, 170, 90
        ),
    ),
    notes=(
        Note("SELECTED RELATIONSHIPS SHOWN · EVERY FOREIGN KEY STILL CHECKED BY THE SEED", 28, 790, 11, meta=True),
        Note(
            "Briefs are selected by attempts. Ready-review evidence links to the item. Shared authority is temporary, not a coordinator task.",
            28,
            816,
            11,
        ),
    ),
)
