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
        schema = root.joinpath("src/charlie_pinboard/adapters/sqlite/schema.sql").read_text(encoding="utf-8")
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
        "Sixteen SQLite tables preserve work identity, scope, proposals, artifacts, authority, and history. "
        "Relationship families are grouped for readability while the source seed accounts for every foreign key."
    ),
    width=1200,
    height=780,
    sections=(
        Section("Discovery", "findings before and after scheduling", 28, 42),
        Section("Current work", "identity that survives execution", 424, 42),
        Section("Scope + relationships", "accepted intent and dependency", 824, 42),
        Section("Durable knowledge", "immutable bytes connected by reference", 28, 398),
        Section("Authority", "ownership and fencing history", 424, 398),
        Section("Integrity + time", "revision and append-only history", 824, 398),
    ),
    guides=(
        Guide((400, 32), (400, 680)),
        Guide((800, 32), (800, 680)),
        Guide((24, 356), (1176, 356)),
    ),
    connectors=(
        Connector(((255, 146), (450, 146)), "proposals", "work-items", "may become work", (352, 134)),
        Connector(((750, 146), (880, 146)), "attempts", "scopes", "accepted scope", (815, 134)),
        Connector(((610, 146), (590, 146)), "attempts", "work-items"),
        Connector(((115, 245), (115, 218), (150, 218), (150, 190)), "proposal-evidence", "proposals"),
        Connector(((265, 245), (265, 218), (210, 218), (210, 190)), "proposal-freshness", "proposals"),
        Connector(((575, 245), (575, 218), (520, 218), (520, 190)), "focus", "work-items"),
        Connector(((645, 245), (645, 218), (680, 218), (680, 190)), "focus", "attempts"),
        Connector(((200, 494), (170, 494)), "item-artifacts", "artifact-refs"),
        Connector(((680, 540), (680, 554)), "lease-counters", None, arrow=False),
        Connector(((680, 554), (515, 554), (515, 585)), None, "lease-generations"),
        Connector(((680, 554), (715, 554), (715, 585)), None, "attempt-leases"),
    ),
    boxes=(
        Box("proposals", "", "Proposals", ("may relate to work",), ("proposals",), 105, 110, 150, 80),
        Box("proposal-evidence", "", "Evidence", (), ("proposal_evidence",), 50, 245, 130, 70),
        Box("proposal-freshness", "", "Freshness", (), ("proposal_freshness",), 190, 245, 150, 70),
        Box("work-items", "", "Work items", ("current identity",), ("work_items",), 450, 110, 140, 80),
        Box("attempts", "", "Attempts", ("executes one item",), ("attempts",), 610, 110, 140, 80),
        Box("focus", "", "Current focus", (), ("current_focus",), 535, 245, 150, 70),
        Box("scopes", "", "Scope revisions", ("accepted history",), ("item_scope_revisions",), 880, 110, 150, 80),
        Box("dependencies", "", "Dependencies", ("item ↔ item graph",), ("item_dependencies",), 1045, 110, 130, 80),
        Box("artifact-refs", "", "Artifact refs", (), ("artifact_refs",), 50, 462, 120, 64),
        Box("item-artifacts", "", "Item artifacts", (), ("item_artifacts",), 200, 462, 130, 64),
        Box("coordination", "", "Coordination", ("project ownership",), ("coordination_lease",), 440, 462, 150, 78),
        Box("lease-counters", "", "Counters", (), ("attempt_lease_", "counters"), 610, 462, 140, 78),
        Box("lease-generations", "", "Generations", (), ("attempt_lease_", "generations"), 445, 585, 140, 78),
        Box("attempt-leases", "", "Attempt leases", (), ("attempt_leases",), 645, 585, 140, 78),
        Box("meta", "", "Project meta", ("current revision",), ("project_meta",), 850, 462, 140, 78),
        Box("history", "", "Transition history", ("append-only account",), ("transition_history",), 1015, 462, 150, 78),
    ),
    notes=(
        Note("CROSS-GROUP LINKS STATED WITHOUT LONG ROUTES", 28, 710, 11, meta=True),
        Note(
            "Attempts retain brief and result artifact references · attempt authority is fenced by counters and generations · history may retain accepted evidence.",
            28,
            738,
            11,
        ),
    ),
)
