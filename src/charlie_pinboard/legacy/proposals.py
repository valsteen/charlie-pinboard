from enum import Enum
from pathlib import Path
from typing import Annotated

import msgspec

from charlie_pinboard.application import service as _store_service
from charlie_pinboard.domain.model import SchemaV1
from charlie_pinboard.legacy.atomic import atomic_create
from charlie_pinboard.legacy.authority import AuthorityVersion, authority_transaction
from charlie_pinboard.legacy.storage_layout import PathIdentityError, identity_child
from charlie_pinboard.legacy.validate import validate_work_state

create_store_proposal = _store_service.create_proposal

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type ProposalText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^|\n]*[^\s|][^|\n]*$")]


class ProposalError(ValueError):
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


class ProposalRelation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: RelationKind
    item: ProposalIdentity | None


class Proposal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: SchemaV1
    proposal_id: ProposalIdentity
    created_at: ProposalText
    source_task_id: ProposalText
    user_label: ProposalText
    trigger: ProposalText
    evidence: tuple[NonEmptyString, ...]
    why_it_matters: ProposalText
    relation: ProposalRelation
    effect: ProposalText
    unlock: ProposalText
    urgency_evidence: ProposalText
    freshness_assumptions: tuple[NonEmptyString, ...]

    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


class ProposalHistory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    proposal: Proposal
    disposition: ProposalDispositionKind
    target: str | None
    coordinator_reason: str | None = None

    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


def parse_proposal(data: bytes | str) -> Proposal:
    try:
        return msgspec.json.decode(data, type=Proposal)
    except msgspec.DecodeError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot decode proposal JSON: {error}") from error


def read_proposal(path: Path) -> Proposal:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot read JSON at '{path}': {error}") from error
    return parse_proposal(data)


def create_proposal(work_root: Path, project_root: Path, data: bytes | str) -> Path:
    proposal = parse_proposal(data)
    with authority_transaction(work_root) as authority:
        current = authority.work_root
        if authority.version == AuthorityVersion.V1 and not (current / "coordinator.json").is_file():
            raise ProposalError("COORDINATOR_NOT_REGISTERED", "Register an exact coordinator before submitting intake.")
        report = validate_work_state(work_root, project_root)
        if not report.valid:
            raise ProposalError("WORK_STATE_INVALID", report.render())
        try:
            path = identity_child(current, current / "inbox", f"{proposal.proposal_id}.json")
        except PathIdentityError as error:
            raise ProposalError(
                "PROPOSAL_IDENTITY_INVALID",
                f"Proposal '{proposal.proposal_id}' must stay inside the authoritative inbox.",
            ) from error
        try:
            atomic_create(path, proposal.render())
        except FileExistsError as error:
            raise ProposalError(
                "PROPOSAL_ALREADY_EXISTS", f"Proposal '{proposal.proposal_id}' already exists."
            ) from error
        return path
