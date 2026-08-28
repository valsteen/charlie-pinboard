from typing import Annotated, Literal

import msgspec

from charlie_pinboard.domain import work_models

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type ProposalSchema = Literal["pinboard-proposal/v1"]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type ProposalText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^|\n]*[^\s|][^|\n]*$")]


class ProposalRelation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: work_models.ProposalRelationKind
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
    position: Annotated[int, msgspec.Meta(ge=1)] | None = None
