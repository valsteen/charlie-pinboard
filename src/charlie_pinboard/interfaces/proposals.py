from enum import Enum
from pathlib import Path
from typing import Annotated

import msgspec

from charlie_pinboard.domain.model import SchemaV1
from charlie_pinboard.interfaces.errors import ProposalError

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type ProposalText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^|\n]*[^\s|][^|\n]*$")]


class RelationKind(Enum):
    INDEPENDENT = "independent"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow-up"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"


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
