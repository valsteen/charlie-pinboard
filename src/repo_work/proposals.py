import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from repo_work.atomic import atomic_create
from repo_work.json_values import JsonObjectError, read_json_object, render_json_object
from repo_work.model import SCHEMA_V1
from repo_work.validate import validate_work_state

IDENTITY_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RELATIONS: Final = frozenset({"independent", "prerequisite", "follow-up", "duplicate", "contradiction"})


class ProposalError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ProposalRelation:
    kind: str
    item: str | None


@dataclass(frozen=True, slots=True)
class Proposal:
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

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = asdict(self)
        result["evidence"] = list(self.evidence)
        result["freshness_assumptions"] = list(self.freshness_assumptions)
        return result

    def render(self) -> bytes:
        return render_json_object(self.as_json())


def _text(value: dict[str, object], field: str) -> str:
    content = value.get(field)
    if not isinstance(content, str) or not content.strip():
        raise ProposalError("PROPOSAL_FIELD_REQUIRED", f"'{field}' must be a non-empty string.")
    if "\n" in content or "|" in content:
        raise ProposalError("PROPOSAL_FIELD_INVALID", f"'{field}' cannot contain a newline or pipe.")
    return content


def _strings(value: dict[str, object], field: str) -> tuple[str, ...]:
    entries = value.get(field)
    if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
        raise ProposalError("PROPOSAL_FIELD_INVALID", f"'{field}' must be a list of non-empty strings.")
    return tuple(entries)


def _relation(value: object) -> ProposalRelation:
    if not isinstance(value, dict):
        raise ProposalError("PROPOSAL_RELATION_INVALID", "relation must be an object.")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in RELATIONS:
        raise ProposalError("PROPOSAL_RELATION_INVALID", "relation.kind is not supported.")
    item = value.get("item")
    if item is not None and (not isinstance(item, str) or not IDENTITY_PATTERN.fullmatch(item)):
        raise ProposalError("PROPOSAL_RELATION_INVALID", "relation.item must be null or a work item identity.")
    return ProposalRelation(kind=kind, item=item)


def parse_proposal(value: dict[str, object]) -> Proposal:
    schema = _text(value, "schema")
    if schema != SCHEMA_V1:
        raise ProposalError("PROPOSAL_SCHEMA_INVALID", f"Proposal must use '{SCHEMA_V1}'.")
    proposal_id = _text(value, "proposal_id")
    if not IDENTITY_PATTERN.fullmatch(proposal_id):
        raise ProposalError("PROPOSAL_ID_INVALID", f"Invalid proposal identity '{proposal_id}'.")
    return Proposal(
        schema=schema,
        proposal_id=proposal_id,
        created_at=_text(value, "created_at"),
        source_task_id=_text(value, "source_task_id"),
        user_label=_text(value, "user_label"),
        trigger=_text(value, "trigger"),
        evidence=_strings(value, "evidence"),
        why_it_matters=_text(value, "why_it_matters"),
        relation=_relation(value.get("relation")),
        effect=_text(value, "effect"),
        unlock=_text(value, "unlock"),
        urgency_evidence=_text(value, "urgency_evidence"),
        freshness_assumptions=_strings(value, "freshness_assumptions"),
    )


def read_proposal(path: Path) -> Proposal:
    try:
        return parse_proposal(read_json_object(path, code="PROPOSAL_INVALID", subject="proposal"))
    except JsonObjectError as error:
        raise ProposalError(error.code, str(error).partition(": ")[2]) from error


def create_proposal(work_root: Path, project_root: Path, value: dict[str, object]) -> Path:
    if not (work_root / "coordinator.json").is_file():
        raise ProposalError("COORDINATOR_NOT_REGISTERED", "Register an exact coordinator before submitting intake.")
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ProposalError("WORK_STATE_INVALID", report.render())
    proposal = parse_proposal(value)
    path = work_root / "inbox" / f"{proposal.proposal_id}.json"
    try:
        atomic_create(path, proposal.render())
    except FileExistsError as error:
        raise ProposalError("PROPOSAL_ALREADY_EXISTS", f"Proposal '{proposal.proposal_id}' already exists.") from error
    return path
