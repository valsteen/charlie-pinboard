from typing import Annotated, Literal

import msgspec

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type ProposalSchema = Literal["pinboard-proposal/v1"]
type ProposalIdentity = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type ProposalText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^|\n]*[^\s|][^|\n]*$")]


class IndependentProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="independent", tag_field="kind"
):
    item: None


class PrerequisiteProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="prerequisite", tag_field="kind"
):
    item: ProposalIdentity


class FollowUpProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="follow-up", tag_field="kind"
):
    item: ProposalIdentity


class DuplicateProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="duplicate", tag_field="kind"
):
    item: ProposalIdentity


class ContradictionProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="contradiction", tag_field="kind"
):
    item: ProposalIdentity


class ClarificationProposalRelation(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="clarification", tag_field="kind"
):
    item: None


type ProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
)


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
