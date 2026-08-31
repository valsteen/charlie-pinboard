import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

import msgspec

from pinboard.domain import work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
)
from pinboard.domain.identifiers import ItemId

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]


def _encoded_record(value: msgspec.Struct) -> bytes:
    return msgspec.json.encode(value, order="sorted") + b"\n"


class WorkItemDefinitionRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    acceptance_criteria: tuple[NonEmptyString, ...]
    dependencies: tuple[NonEmptyString, ...]
    effect: NonEmptyString
    evidence: tuple[NonEmptyString, ...]
    hypothesis: NonEmptyString
    non_scope: tuple[NonEmptyString, ...]
    objective: NonEmptyString
    schema: Literal["pinboard-work-item-definition/v1"]
    scope: tuple[NonEmptyString, ...]
    title: NonEmptyString
    unlock: NonEmptyString


class TransitionReceiptOutcome(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    evidence: str | None
    outcome: str | None
    candidate: str | None = None
    checkpoint: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    outcome_schema: str
    payload: bytes


def encode_transition_receipt_outcome(
    *,
    evidence: str | None,
    outcome: str | None,
    candidate: str | None = None,
    checkpoint: str | None = None,
) -> bytes:
    return msgspec.json.encode(
        TransitionReceiptOutcome(evidence, outcome, candidate, checkpoint),
        order="sorted",
    )


def _ordered_unique(values: tuple[str, ...], field: str, *, required: bool = False) -> DecisionFailure | None:
    if required and not values:
        return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_INVALID, f"{field} must be nonempty.")
    if any(not value or value.strip() != value or "\n" in value or "\r" in value for value in values):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"{field} entries must be nonempty canonical single-line values.",
        )
    if len(values) != len(set(values)):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"{field} entries must be ordered and unique.",
        )
    return None


def work_item_definition_record(
    definition: work_models.WorkItemDefinition,
) -> DecisionResult[WorkItemDefinitionRecord]:
    for field, value in (
        ("title", definition.title),
        ("objective", definition.objective),
        ("hypothesis", definition.hypothesis),
        ("effect", definition.effect),
        ("unlock", definition.unlock),
    ):
        if not value or value.strip() != value or "\n" in value or "\r" in value:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_INVALID,
                f"{field} must be a nonempty canonical single-line value.",
            )
    for field, values, required in (
        ("evidence", definition.evidence, False),
        ("scope", definition.scope, True),
        ("non_scope", definition.non_scope, False),
        ("acceptance_criteria", definition.acceptance_criteria, True),
        ("dependencies", tuple(definition.dependencies), False),
    ):
        if (failure := _ordered_unique(values, field, required=required)) is not None:
            return failure
    return WorkItemDefinitionRecord(
        definition.acceptance_criteria,
        tuple(definition.dependencies),
        definition.effect,
        definition.evidence,
        definition.hypothesis,
        definition.non_scope,
        definition.objective,
        "pinboard-work-item-definition/v1",
        definition.scope,
        definition.title,
        definition.unlock,
    )


def work_item_definition_bytes(definition: work_models.WorkItemDefinition) -> DecisionResult[bytes]:
    record = work_item_definition_record(definition)
    if isinstance(record, DecisionFailure):
        return record
    return _encoded_record(record)


def work_item_definition_digest(definition: work_models.WorkItemDefinition) -> DecisionResult[str]:
    payload = work_item_definition_bytes(definition)
    if isinstance(payload, DecisionFailure):
        return payload
    return hashlib.sha256(payload).hexdigest()


def decode_work_item_definition(payload: bytes) -> DecisionResult[work_models.WorkItemDefinition]:
    try:
        record = msgspec.json.decode(payload, type=WorkItemDefinitionRecord, strict=True)
    except msgspec.DecodeError as error:
        return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Definition JSON is invalid: {error}")
    if _encoded_record(record) != payload:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "Definition JSON must use the canonical encoding.",
        )
    definition = work_models.WorkItemDefinition(
        record.title,
        record.objective,
        record.hypothesis,
        record.evidence,
        record.scope,
        record.non_scope,
        record.acceptance_criteria,
        tuple(ItemId(value) for value in record.dependencies),
        record.effect,
        record.unlock,
    )
    validated = work_item_definition_record(definition)
    if isinstance(validated, DecisionFailure):
        return validated
    return definition
