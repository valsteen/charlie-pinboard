import re
from enum import Enum
from pathlib import Path
from typing import Final, override

import msgspec

from repo_work.atomic import atomic_create
from repo_work.model import SCHEMA_V1
from repo_work.records import JsonRecord
from repo_work.validate import validate_work_state

IDENTITY_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_TEXT_FIELDS: Final = frozenset(
    {
        "schema",
        "proposal_id",
        "created_at",
        "source_task_id",
        "user_label",
        "trigger",
        "why_it_matters",
        "effect",
        "unlock",
        "urgency_evidence",
    }
)


class ProposalError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RelationKind(Enum):
    INDEPENDENT = "independent"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow-up"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"


class ProposalDispositionKind(Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    RETURNED = "returned"
    REJECTED = "rejected"


class ProposalRelation(JsonRecord):
    kind: RelationKind
    item: str | None

    def __post_init__(self) -> None:
        if self.item is not None and not IDENTITY_PATTERN.fullmatch(self.item):
            raise ProposalError("PROPOSAL_RELATION_INVALID", "relation.item must be null or a work item identity.")


class Proposal(JsonRecord):
    schema: str
    proposal_id: str
    created_at: str
    source_task_id: str
    user_label: str
    trigger: str
    evidence: tuple[str, ...]
    why_it_matters: str
    relation: ProposalRelation
    effect: str
    unlock: str
    urgency_evidence: str
    freshness_assumptions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_V1:
            raise ProposalError("PROPOSAL_SCHEMA_INVALID", f"Proposal must use '{SCHEMA_V1}'.")
        if not IDENTITY_PATTERN.fullmatch(self.proposal_id):
            raise ProposalError("PROPOSAL_ID_INVALID", f"Invalid proposal identity '{self.proposal_id}'.")
        fields = (
            ("proposal_id", self.proposal_id),
            ("created_at", self.created_at),
            ("source_task_id", self.source_task_id),
            ("user_label", self.user_label),
            ("trigger", self.trigger),
            ("why_it_matters", self.why_it_matters),
            ("effect", self.effect),
            ("unlock", self.unlock),
            ("urgency_evidence", self.urgency_evidence),
        )
        for name, value in fields:
            if not value.strip():
                raise ProposalError("PROPOSAL_FIELD_REQUIRED", f"'{name}' must be a non-empty string.")
            if "\n" in value or "|" in value:
                raise ProposalError("PROPOSAL_FIELD_INVALID", f"'{name}' cannot contain a newline or pipe.")
        if not all(self.evidence) or not all(self.freshness_assumptions):
            raise ProposalError("PROPOSAL_FIELD_INVALID", "List fields must contain non-empty strings.")

    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


class ProposalHistory(Proposal):
    disposition: ProposalDispositionKind
    target: str | None
    coordinator_reason: str | None = None

    @classmethod
    def record(
        cls,
        proposal: Proposal,
        disposition: ProposalDispositionKind,
        target: str | None,
        coordinator_reason: str | None = None,
    ) -> ProposalHistory:
        return cls(
            schema=proposal.schema,
            proposal_id=proposal.proposal_id,
            created_at=proposal.created_at,
            source_task_id=proposal.source_task_id,
            user_label=proposal.user_label,
            trigger=proposal.trigger,
            evidence=proposal.evidence,
            why_it_matters=proposal.why_it_matters,
            relation=proposal.relation,
            effect=proposal.effect,
            unlock=proposal.unlock,
            urgency_evidence=proposal.urgency_evidence,
            freshness_assumptions=proposal.freshness_assumptions,
            disposition=disposition,
            target=target,
            coordinator_reason=coordinator_reason,
        )

    @override
    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


def _mentions_field(message: str, field: str) -> bool:
    return f"`{field}`" in message or f"$.{field}" in message


def _proposal_validation_error(error: msgspec.ValidationError) -> ProposalError:
    message = str(error)
    if _mentions_field(message, "relation"):
        return ProposalError("PROPOSAL_RELATION_INVALID", message)
    if message.startswith("Expected `object`"):
        return ProposalError("PROPOSAL_INVALID", "JSON root must be an object.")
    if any(_mentions_field(message, field) for field in REQUIRED_TEXT_FIELDS):
        return ProposalError("PROPOSAL_FIELD_REQUIRED", message)
    return ProposalError("PROPOSAL_FIELD_INVALID", message)


def parse_proposal(data: bytes | str) -> Proposal:
    try:
        return msgspec.json.decode(data, type=Proposal, strict=True)
    except msgspec.ValidationError as error:
        raise _proposal_validation_error(error) from error
    except msgspec.DecodeError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot parse JSON: {error}") from error


def read_proposal(path: Path) -> Proposal:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot read JSON at '{path}': {error}") from error
    return parse_proposal(data)


def create_proposal(work_root: Path, project_root: Path, data: bytes | str) -> Path:
    if not (work_root / "coordinator.json").is_file():
        raise ProposalError("COORDINATOR_NOT_REGISTERED", "Register an exact coordinator before submitting intake.")
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ProposalError("WORK_STATE_INVALID", report.render())
    proposal = parse_proposal(data)
    path = work_root / "inbox" / f"{proposal.proposal_id}.json"
    try:
        atomic_create(path, proposal.render())
    except FileExistsError as error:
        raise ProposalError("PROPOSAL_ALREADY_EXISTS", f"Proposal '{proposal.proposal_id}' already exists.") from error
    return path
