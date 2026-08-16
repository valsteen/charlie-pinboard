from __future__ import annotations

import json
import re
from pathlib import Path

from repo_work.atomic import atomic_create
from repo_work.model import SCHEMA_V1
from repo_work.validate import validate_work_state


IDENTITY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RELATIONS = frozenset({"independent", "prerequisite", "follow-up", "duplicate", "contradiction"})
REQUIRED_TEXT = (
    "proposal_id",
    "created_at",
    "source_task_id",
    "user_label",
    "trigger",
    "why_it_matters",
    "effect",
    "unlock",
    "urgency_evidence",
)


class ProposalError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def validate_proposal(value: dict[str, object]) -> None:
    if value.get("schema") != SCHEMA_V1:
        raise ProposalError("PROPOSAL_SCHEMA_INVALID", f"Proposal must use '{SCHEMA_V1}'.")
    for field in REQUIRED_TEXT:
        content = value.get(field)
        if not isinstance(content, str) or not content.strip():
            raise ProposalError("PROPOSAL_FIELD_REQUIRED", f"'{field}' must be a non-empty string.")
        if "\n" in content or "|" in content:
            raise ProposalError("PROPOSAL_FIELD_INVALID", f"'{field}' cannot contain a newline or pipe.")
    proposal_id = str(value["proposal_id"])
    if not IDENTITY_PATTERN.fullmatch(proposal_id):
        raise ProposalError("PROPOSAL_ID_INVALID", f"Invalid proposal identity '{proposal_id}'.")
    for field in ("evidence", "freshness_assumptions"):
        entries = value.get(field)
        if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
            raise ProposalError("PROPOSAL_FIELD_INVALID", f"'{field}' must be a list of non-empty strings.")
    relation = value.get("relation")
    if not isinstance(relation, dict) or relation.get("kind") not in RELATIONS:
        raise ProposalError("PROPOSAL_RELATION_INVALID", "relation.kind is not supported.")
    related_item = relation.get("item")
    if related_item is not None and (
        not isinstance(related_item, str) or not IDENTITY_PATTERN.fullmatch(related_item)
    ):
        raise ProposalError("PROPOSAL_RELATION_INVALID", "relation.item must be null or a work item identity.")


def read_proposal(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot parse '{path}': {error}") from error
    if not isinstance(value, dict):
        raise ProposalError("PROPOSAL_INVALID", "Proposal root must be an object.")
    validate_proposal(value)
    return value


def create_proposal(work_root: Path, project_root: Path, value: dict[str, object]) -> Path:
    if not (work_root / "coordinator.json").is_file():
        raise ProposalError("COORDINATOR_NOT_REGISTERED", "Register an exact coordinator before submitting intake.")
    report = validate_work_state(work_root, project_root)
    if not report.valid:
        raise ProposalError("WORK_STATE_INVALID", report.render())
    validate_proposal(value)
    proposal_id = str(value["proposal_id"])
    path = work_root / "inbox" / f"{proposal_id}.json"
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        atomic_create(path, data)
    except FileExistsError as error:
        raise ProposalError("PROPOSAL_ALREADY_EXISTS", f"Proposal '{proposal_id}' already exists.") from error
    return path
