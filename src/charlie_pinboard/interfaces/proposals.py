from typing import Annotated, Literal

import msgspec

from charlie_pinboard.domain.model import ProposalRelationKind


class ProposalError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type ProposalSchema = Literal["pinboard-proposal/v1"]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type ProposalText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^|\n]*[^\s|][^|\n]*$")]


class ProposalRelation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: ProposalRelationKind
    item: ProposalIdentity | None


class Proposal(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ProposalSchema
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


def parse_proposal(data: bytes | str) -> Proposal:
    try:
        return msgspec.json.decode(data, type=Proposal)
    except msgspec.DecodeError as error:
        raise ProposalError("PROPOSAL_INVALID", f"Cannot decode proposal JSON: {error}") from error
