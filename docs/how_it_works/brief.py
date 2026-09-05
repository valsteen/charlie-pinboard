from pinboard.interfaces import work_brief_models, work_briefs

from .model import Box, Connector, Diagram, Guide, Note, Section

BRIEF_REASONING_FIELDS = frozenset(
    {
        "outcome",
        "product_decision_and_provenance",
        "scope",
        "non_goals",
        "testing_strategy",
        "remaining_work",
    }
)

CHECKPOINT_REASONING_FIELDS = frozenset(
    {
        "contracts",
        "acceptance_criteria",
        "reviewed_authorities",
        "coverage",
        "lifecycle_partition",
        "verification",
    }
)

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "WorkBrief": work_brief_models.WorkBrief.__name__,
    "CrossBoundaryCheckpoint": work_brief_models.CrossBoundaryCheckpoint.__name__,
    "decode_work_brief": work_briefs.decode_work_brief.__name__,
    "canonical_work_brief_bytes": work_briefs.canonical_work_brief_bytes.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"brief visual references renamed source symbols: {', '.join(renamed)}")
    missing_brief_fields = BRIEF_REASONING_FIELDS.difference(work_brief_models.WorkBrief.__struct_fields__)
    missing_checkpoint_fields = CHECKPOINT_REASONING_FIELDS.difference(
        work_brief_models.CrossBoundaryCheckpoint.__struct_fields__
    )
    if missing_brief_fields or missing_checkpoint_fields:
        missing = sorted(missing_brief_fields | missing_checkpoint_fields)
        raise ValueError(f"brief visual references missing reasoning fields: {', '.join(missing)}")


DIAGRAM = Diagram(
    slug="brief",
    title="One strict brief works as a reasoning scaffold and a validated envelope",
    description=(
        "The same canonical work brief is interpreted by a human and an LLM through explicit semantic sections, while "
        "Pinboard code checks only its shape, internal references, and immutable identity. Implementation, review, and "
        "resume receive those same distinctions; code does not decide whether the prose is true."
    ),
    width=1400,
    height=860,
    sections=(
        Section("One accepted artifact", "the same distinctions travel with the attempt", 28, 44),
        Section("Meaning carried forward", "human + LLM semantic responsibility", 28, 430),
        Section("Checks code can perform", "machine-enforced envelope", 730, 430),
    ),
    guides=(
        Guide((220, 40), (1372, 40)),
        Guide((205, 426), (680, 426)),
        Guide((900, 426), (1372, 426)),
        Guide((700, 396), (700, 800)),
    ),
    connectors=(
        Connector(((600, 200), (600, 215), (370, 215), (370, 250)), "brief", "reasoning", "interpreted", (485, 204)),
        Connector(((800, 200), (800, 215), (1030, 215), (1030, 250)), "brief", "validation", "validated", (915, 204)),
        Connector(((250, 370), (250, 410), (200, 410), (200, 470)), "reasoning", "intent"),
        Connector(((490, 370), (490, 410), (540, 410), (540, 470)), "reasoning", "proof"),
        Connector(((370, 370), (370, 625), (370, 660)), "reasoning", "remainder"),
        Connector(((910, 370), (910, 410), (860, 410), (860, 470)), "validation", "shape"),
        Connector(((1150, 370), (1150, 410), (1200, 410), (1200, 470)), "validation", "references"),
        Connector(((1030, 370), (1030, 625), (1030, 660)), "validation", "identity"),
    ),
    boxes=(
        Box(
            "brief",
            "Canonical work brief",
            "One accepted artifact",
            ("travels with the attempt", "reread for implementation + review"),
            ("pinboard-work-brief/v2",),
            520,
            80,
            360,
            120,
        ),
        Box(
            "reasoning",
            "Semantic responsibility",
            "Human + LLM interpret it",
            ("labels + hierarchy guide attention", "each stage sees the same distinctions"),
            ("meaning remains a judgment",),
            130,
            250,
            480,
            120,
        ),
        Box(
            "validation",
            "Mechanical responsibility",
            "Pinboard checks the envelope",
            ("decode · cross-check · canonicalize", "never infer whether prose is true"),
            ("code-owned guarantees",),
            790,
            250,
            480,
            120,
        ),
        Box(
            "intent",
            "Intent + boundary",
            "Say what the work means",
            ("outcome + provenance", "scope + non-goals"),
            ("interpreted semantics",),
            50,
            470,
            300,
            120,
        ),
        Box(
            "proof",
            "Obligations + proof",
            "Say what earns acceptance",
            ("contracts + criteria", "sources + tests + verification"),
            ("interpreted semantics",),
            390,
            470,
            300,
            120,
        ),
        Box(
            "remainder",
            "Honest remainder",
            "Say what is still undone",
            ("remaining work + deferrals",),
            ("interpreted semantics",),
            220,
            660,
            300,
            115,
        ),
        Box(
            "shape",
            "Exact shape",
            "Reject malformed briefs",
            ("required fields present", "unknown fields rejected"),
            ("typed decode",),
            710,
            470,
            300,
            120,
        ),
        Box(
            "references",
            "Coherent references",
            "Reject mismatched links",
            ("scope + authority identities", "coverage owners + unique IDs"),
            ("cross-field checks",),
            1050,
            470,
            300,
            120,
        ),
        Box(
            "identity",
            "Stable identity",
            "Bind the exact artifact",
            ("canonical bytes + SHA-256", "immutable publication"),
            ("byte identity",),
            880,
            660,
            300,
            120,
        ),
    ),
    notes=(
        Note(
            "IMPLEMENTATION · REVIEW · RESUME RECEIVE THE SAME DISTINCTIONS — CODE PROVES THE ENVELOPE, NOT THE MEANING",
            700,
            825,
            11,
            "middle",
            True,
        ),
    ),
)
