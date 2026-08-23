import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, NewType, assert_never

import msgspec

from charlie_pinboard.application.dispatch import (
    BriefReviewPublisher,
    DispatchEnvironment,
    DispatchError,
)
from charlie_pinboard.application.dispatch import DispatchPermission as DispatchPermission
from charlie_pinboard.domain.identifiers import TaskId
from charlie_pinboard.domain.model import AttemptState
from charlie_pinboard.legacy.actions import Action, ActionKind, actions_for, coordinator_generation
from charlie_pinboard.legacy.atomic import PlatformNotSupportedError, atomic_create
from charlie_pinboard.legacy.authority import AuthorityVersion, authority_transaction, resolve_authority
from charlie_pinboard.legacy.leases import LeaseError, require_coordination
from charlie_pinboard.legacy.markdown import Header, ParseError, parse_attempt, parse_header, parse_header_text

HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CONTRACT_COLUMNS: Final = (
    "Invariant",
    "Authority / owner",
    "Required consumer or production observation",
    "Failure classification",
    "Exact verification",
    "Preflight / final revalidation",
)
EMPTY_CONTRACT_CELLS: Final = frozenset({"", "—", "-", "none", "null", "n/a", "tbd", "todo"})
TABLE_DELIMITER: Final = re.compile(r"^:?-{3,}:?$")
IDENTIFIER: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
AUTHORITY_REFERENCE: Final = re.compile(
    r"^authority:(?P<authority>[a-z0-9]+(?:-[a-z0-9]+)*)#(?P<family>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
CRITERION_OWNER: Final = re.compile(r"^criterion:(?P<number>[1-9][0-9]*)$")
PROHIBITION: Final = re.compile(r"\b(?:must not|do not|cannot|never|prohibition|prohibited)\b", re.IGNORECASE)
DEFERRAL: Final = re.compile(
    r"^Deferral:\s+(?P<label>[a-z0-9]+(?:-[a-z0-9]+)*)\s+—\s+(?P<reason>.+?)\s+Reopen when:\s+(?P<reopen>.+)$"
)
ACCEPTANCE_CRITERION: Final = re.compile(r"^(?P<number>[1-9][0-9]*)\.\s+\S")
REVIEWED_AUTHORITY_COLUMNS: Final = ("Authority ID", "Selector", "Reviewed SHA-256", "In-scope families")
REVIEW_ID: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COVERAGE_COLUMNS: Final = (
    "Authority / invariant family",
    "Required distinction",
    "Required consumer / production observation",
    "Disposition",
    "Brief owner",
    "Cheapest counterexample",
)
LIFECYCLE_COLUMNS: Final = (
    "Operation",
    "Allowed source state",
    "Required authority",
    "Required observation / evidence",
    "State and fencing effects",
    "Nearest illegal sibling / stable rejection",
)
BRIEF_REVIEW_COLUMNS: Final = (
    "Authority / invariant family",
    "Brief owner",
    "Verdict",
    "Cheapest counterexample result",
)

AuthorityId = NewType("AuthorityId", str)
AuthorityFamily = NewType("AuthorityFamily", str)


class CoverageDisposition(Enum):
    CONTRACT = "contract"
    ACCEPTANCE = "acceptance"
    DEFERRED = "deferred"
    NOT_APPLICABLE = "not-applicable"


class BriefOwnerKind(Enum):
    CONTRACT = "contract"
    CRITERION = "criterion"
    DEFERRAL = "deferral"
    REASON = "reason"


@dataclass(frozen=True, slots=True)
class MarkdownTable:
    rows: tuple[tuple[str, ...], ...]
    serialized: bytes


@dataclass(frozen=True, slots=True)
class AuthoritySelector:
    relative_path: Path
    heading: str | None


@dataclass(frozen=True, slots=True)
class ReviewedAuthority:
    authority_id: AuthorityId
    selector: AuthoritySelector
    reviewed_sha256: str
    families: tuple[AuthorityFamily, ...]


@dataclass(frozen=True, slots=True)
class ContractRecord:
    invariant: str
    authority: str
    consumer: str
    failure: str
    verification: str
    revalidation: str


@dataclass(frozen=True, slots=True)
class BriefOwner:
    kind: BriefOwnerKind
    value: str


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    authority_id: AuthorityId
    family: AuthorityFamily
    distinction: str
    consumer: str
    disposition: CoverageDisposition
    owner: BriefOwner
    counterexample: str

    @property
    def reference(self) -> str:
        return f"authority:{self.authority_id}#{self.family}"


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    operation: str
    source_state: str
    authority: str
    evidence: str
    effects: str
    illegal_sibling: str


@dataclass(frozen=True, slots=True)
class BriefReviewMetadata:
    attempt: str
    checkpoint: str
    checkpoint_sha256: str
    reviewed_authority_set_sha256: str
    reviewer_task_id: str
    status: str
    verdict: str


class CheckpointBoundary(Enum):
    LOCAL = "local"
    CROSS_BOUNDARY = "cross-boundary"


def read_dispatch_environment(path: Path) -> DispatchEnvironment:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise DispatchError("DISPATCH_ENVIRONMENT_UNREADABLE", f"Cannot read '{path}': {error}") from error
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment)
    except msgspec.DecodeError as error:
        raise DispatchError("DISPATCH_ENVIRONMENT_INVALID", f"Cannot decode dispatch environment: {error}") from error


def _checkpoint_section(path: Path, checkpoint: str) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = HEADING.fullmatch(line)
        if match is not None and match.group(2) == checkpoint:
            matches.append((index, len(match.group(1))))
    if not matches:
        raise DispatchError("DISPATCH_CHECKPOINT_MISSING", f"Checkpoint '{checkpoint}' is not in '{path}'.")
    if len(matches) != 1:
        raise DispatchError("DISPATCH_CHECKPOINT_AMBIGUOUS", f"Checkpoint '{checkpoint}' appears more than once.")
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = HEADING.fullmatch(lines[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    return tuple(lines[start:end])


def _table_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def _code_value(cell: str) -> str:
    return cell[1:-1] if len(cell) >= 2 and cell.startswith("`") and cell.endswith("`") else cell


def _require_value(value: str, error_code: str, description: str) -> str:
    if value.casefold() in EMPTY_CONTRACT_CELLS:
        raise DispatchError(error_code, f"{description} must have a concrete value.")
    return value


def _markdown_table(
    lines: tuple[str, ...],
    columns: tuple[str, ...],
    *,
    label: str,
    missing_code: str,
    invalid_code: str,
) -> MarkdownTable:
    starts = tuple(index for index, line in enumerate(lines) if _table_cells(line) == columns)
    if not starts:
        raise DispatchError(missing_code, f"The {label} table is missing.")
    if len(starts) != 1:
        raise DispatchError(invalid_code, f"The {label} table must appear exactly once.")
    start = starts[0]
    separator = _table_cells(lines[start + 1]) if start + 1 < len(lines) else ()
    if len(separator) != len(columns) or any(TABLE_DELIMITER.fullmatch(cell) is None for cell in separator):
        raise DispatchError(invalid_code, f"The {label} table separator is missing.")
    rows: list[tuple[str, ...]] = []
    serialized_lines = [lines[start], lines[start + 1]]
    for line in lines[start + 2 :]:
        cells = _table_cells(line)
        if not cells:
            break
        if len(cells) != len(columns):
            raise DispatchError(invalid_code, f"A {label} row has {len(cells)} cells; expected {len(columns)}.")
        rows.append(cells)
        serialized_lines.append(line)
    if not rows:
        raise DispatchError(missing_code, f"The {label} table has no rows.")
    return MarkdownTable(tuple(rows), ("\n".join(serialized_lines) + "\n").encode())


def _normalized_section_bytes(section: tuple[str, ...]) -> bytes:
    return ("\n".join(section) + "\n").encode()


def _selected_section(lines: tuple[str, ...], heading: str, path: Path) -> tuple[str, ...]:
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = HEADING.fullmatch(line)
        if match is not None and match.group(2) == heading:
            matches.append((index, len(match.group(1))))
    if not matches:
        raise DispatchError("DISPATCH_AUTHORITY_SELECTOR_INVALID", f"Heading '{heading}' is not in '{path}'.")
    if len(matches) != 1:
        raise DispatchError("DISPATCH_AUTHORITY_SELECTOR_INVALID", f"Heading '{heading}' is not unique in '{path}'.")
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = HEADING.fullmatch(lines[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    return lines[start:end]


def _contract_records(section: tuple[str, ...]) -> tuple[ContractRecord, ...]:
    table = _markdown_table(
        section,
        CONTRACT_COLUMNS,
        label="Contract",
        missing_code="DISPATCH_CONTRACT_MISSING",
        invalid_code="DISPATCH_CONTRACT_INVALID",
    )
    records: list[ContractRecord] = []
    for row_number, row in enumerate(table.rows, start=1):
        for column, cell in zip(CONTRACT_COLUMNS, row, strict=True):
            if cell.casefold() in EMPTY_CONTRACT_CELLS:
                raise DispatchError(
                    "DISPATCH_CONTRACT_INCOMPLETE",
                    f"Contract row {row_number} has no concrete value for '{column}'.",
                )
        records.append(ContractRecord(*row))
    return tuple(records)


def _validate_cross_boundary_checkpoint(section: tuple[str, ...]) -> tuple[ContractRecord, ...]:
    if "Checkpoint outcome: independently-buildable" not in section:
        raise DispatchError(
            "DISPATCH_CHECKPOINT_NOT_BUILDABLE",
            "A cross-boundary checkpoint must record 'Checkpoint outcome: independently-buildable'.",
        )
    return _contract_records(section)


def _authority_selector(cell: str) -> AuthoritySelector:
    value = _code_value(cell)
    relative, separator, heading = value.partition("#")
    relative_path = Path(relative)
    if not relative or relative_path.is_absolute() or ".." in relative_path.parts or (separator and not heading):
        raise DispatchError(
            "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            f"Authority selector '{value}' must name one project-relative file and optional literal heading.",
        )
    return AuthoritySelector(relative_path, heading if separator else None)


def _reviewed_authorities(section: tuple[str, ...]) -> tuple[tuple[ReviewedAuthority, ...], bytes]:
    table = _markdown_table(
        section,
        REVIEWED_AUTHORITY_COLUMNS,
        label="Reviewed authorities",
        missing_code="DISPATCH_REVIEWED_AUTHORITIES_MISSING",
        invalid_code="DISPATCH_REVIEWED_AUTHORITIES_INVALID",
    )
    records: list[ReviewedAuthority] = []
    seen_ids: set[AuthorityId] = set()
    for row in table.rows:
        identifier_value, selector_value, digest_value, families_value = row
        identifier = AuthorityId(_code_value(identifier_value))
        digest = _code_value(digest_value)
        family_cells = tuple(part.strip() for part in _code_value(families_value).split(","))
        families = tuple(AuthorityFamily(value) for value in family_cells)
        if IDENTIFIER.fullmatch(identifier) is None or identifier in seen_ids:
            raise DispatchError(
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
                f"Authority ID '{identifier}' must be unique kebab-case.",
            )
        if SHA256.fullmatch(digest) is None:
            raise DispatchError(
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
                f"Authority '{identifier}' must carry a lowercase SHA-256 digest.",
            )
        if (
            not families
            or any(IDENTIFIER.fullmatch(value) is None for value in families)
            or len(set(families)) != len(families)
        ):
            raise DispatchError(
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
                f"Authority '{identifier}' must name one or more unique kebab-case families.",
            )
        seen_ids.add(identifier)
        records.append(ReviewedAuthority(identifier, _authority_selector(selector_value), digest, families))
    return tuple(records), table.serialized


def _authority_bytes(project_root: Path, authority: ReviewedAuthority) -> bytes:
    path = project_root / authority.selector.relative_path
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise DispatchError(
            "DISPATCH_AUTHORITY_UNREADABLE",
            f"Cannot read authority '{authority.authority_id}' at '{path}': {error}",
        ) from error
    if authority.selector.heading is None:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DispatchError(
            "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            f"Heading-selected authority '{path}' is not UTF-8.",
        ) from error
    selected = _selected_section(tuple(text.splitlines()), authority.selector.heading, path)
    return _normalized_section_bytes(selected)


def _validate_authority_digests(project_root: Path, authorities: tuple[ReviewedAuthority, ...]) -> None:
    for authority in authorities:
        observed = hashlib.sha256(_authority_bytes(project_root, authority)).hexdigest()
        if observed != authority.reviewed_sha256:
            raise DispatchError(
                "DISPATCH_AUTHORITY_STALE",
                f"Authority '{authority.authority_id}' changed after the brief review.",
            )


def _acceptance_criteria(section: tuple[str, ...]) -> frozenset[int]:
    headings = tuple(
        index
        for index, line in enumerate(section)
        if (match := HEADING.fullmatch(line)) is not None and match.group(2) == "Acceptance criteria"
    )
    if len(headings) != 1:
        raise DispatchError(
            "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            "A cross-boundary checkpoint must have one Acceptance criteria section.",
        )
    start = headings[0]
    level_match = HEADING.fullmatch(section[start])
    if level_match is None:
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", "Acceptance criteria heading is invalid.")
    level = len(level_match.group(1))
    end = len(section)
    for index in range(start + 1, len(section)):
        match = HEADING.fullmatch(section[index])
        if match is not None and len(match.group(1)) <= level:
            end = index
            break
    numbers = tuple(
        int(match.group("number"))
        for line in section[start + 1 : end]
        if (match := ACCEPTANCE_CRITERION.match(line)) is not None
    )
    if not numbers or len(set(numbers)) != len(numbers):
        raise DispatchError(
            "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            "Acceptance criteria must use unique positive numbered rows.",
        )
    return frozenset(numbers)


def _deferrals(section: tuple[str, ...]) -> frozenset[str]:
    labels: list[str] = []
    for line in section:
        match = DEFERRAL.fullmatch(line)
        if match is None:
            continue
        _require_value(match.group("reason"), "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Deferral reason")
        _require_value(match.group("reopen"), "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Deferral reopen condition")
        labels.append(match.group("label"))
    if len(set(labels)) != len(labels):
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", "Deferral labels must be unique.")
    return frozenset(labels)


def _brief_owner(
    cell: str,
    disposition: CoverageDisposition,
    contracts: frozenset[str],
    criteria: frozenset[int],
    deferrals: frozenset[str],
) -> BriefOwner:
    value = _code_value(cell)
    prefix, separator, owner_value = value.partition(":")
    if not separator or not owner_value:
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", f"Brief owner '{value}' is unresolved.")
    try:
        kind = BriefOwnerKind(prefix)
    except ValueError as error:
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", f"Brief owner '{value}' is unresolved.") from error
    expected_kind = {
        CoverageDisposition.CONTRACT: BriefOwnerKind.CONTRACT,
        CoverageDisposition.ACCEPTANCE: BriefOwnerKind.CRITERION,
        CoverageDisposition.DEFERRED: BriefOwnerKind.DEFERRAL,
        CoverageDisposition.NOT_APPLICABLE: BriefOwnerKind.REASON,
    }[disposition]
    if kind != expected_kind:
        raise DispatchError(
            "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            f"Disposition '{disposition.value}' cannot use owner '{value}'.",
        )
    if kind == BriefOwnerKind.CONTRACT and owner_value not in contracts:
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", f"Contract owner '{owner_value}' does not exist.")
    if kind == BriefOwnerKind.CRITERION:
        criterion = CRITERION_OWNER.fullmatch(value)
        if criterion is None or int(criterion.group("number")) not in criteria:
            raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", f"Criterion owner '{value}' does not exist.")
    if kind == BriefOwnerKind.DEFERRAL and owner_value not in deferrals:
        raise DispatchError("DISPATCH_AUTHORITY_COVERAGE_INVALID", f"Deferral owner '{owner_value}' does not exist.")
    if kind == BriefOwnerKind.REASON:
        _require_value(owner_value, "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Not-applicable reason")
    return BriefOwner(kind, owner_value)


def _coverage_records(
    section: tuple[str, ...],
    authorities: tuple[ReviewedAuthority, ...],
    contracts: tuple[ContractRecord, ...],
) -> tuple[CoverageRecord, ...]:
    table = _markdown_table(
        section,
        COVERAGE_COLUMNS,
        label="Authoritative coverage",
        missing_code="DISPATCH_AUTHORITY_COVERAGE_MISSING",
        invalid_code="DISPATCH_AUTHORITY_COVERAGE_INVALID",
    )
    contract_invariants = frozenset(record.invariant for record in contracts)
    criteria = _acceptance_criteria(section)
    deferrals = _deferrals(section)
    expected = frozenset((authority.authority_id, family) for authority in authorities for family in authority.families)
    records: list[CoverageRecord] = []
    seen: set[tuple[AuthorityId, AuthorityFamily]] = set()
    for row in table.rows:
        reference_cell, distinction, consumer, disposition_cell, owner_cell, counterexample = row
        reference = _code_value(reference_cell)
        match = AUTHORITY_REFERENCE.fullmatch(reference)
        if match is None:
            raise DispatchError(
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
                f"Coverage reference '{reference}' must be 'authority:<id>#<family>'.",
            )
        key = (AuthorityId(match.group("authority")), AuthorityFamily(match.group("family")))
        if key not in expected or key in seen:
            raise DispatchError(
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
                f"Coverage reference '{reference}' is unknown or duplicated.",
            )
        try:
            disposition = CoverageDisposition(_code_value(disposition_cell))
        except ValueError as error:
            raise DispatchError(
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
                f"Coverage disposition '{disposition_cell}' is not allowed.",
            ) from error
        _require_value(distinction, "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Required distinction")
        _require_value(consumer, "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Required consumer")
        _require_value(counterexample, "DISPATCH_AUTHORITY_COVERAGE_INVALID", "Cheapest counterexample")
        if disposition in {CoverageDisposition.DEFERRED, CoverageDisposition.NOT_APPLICABLE} and PROHIBITION.search(
            distinction
        ):
            raise DispatchError(
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
                f"In-scope prohibition '{reference}' cannot be deferred or marked not-applicable.",
            )
        owner = _brief_owner(owner_cell, disposition, contract_invariants, criteria, deferrals)
        records.append(CoverageRecord(*key, distinction, consumer, disposition, owner, counterexample))
        seen.add(key)
    if seen != expected:
        missing = ", ".join(f"authority:{authority}#{family}" for authority, family in sorted(expected - seen))
        raise DispatchError(
            "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            f"Authoritative coverage is incomplete; missing: {missing}.",
        )
    return tuple(records)


def _validate_lifecycle_partition(section: tuple[str, ...]) -> tuple[LifecycleRecord, ...]:
    declarations = tuple(line for line in section if line.startswith("Lifecycle partition:"))
    if len(declarations) != 1:
        raise DispatchError(
            "DISPATCH_LIFECYCLE_PARTITION_INVALID",
            "A cross-boundary checkpoint must declare exactly one lifecycle partition disposition.",
        )
    declaration = declarations[0]
    if declaration.startswith("Lifecycle partition: not-applicable —"):
        reason = declaration.removeprefix("Lifecycle partition: not-applicable —").strip()
        _require_value(reason, "DISPATCH_LIFECYCLE_PARTITION_INVALID", "Lifecycle not-applicable reason")
        return ()
    if declaration != "Lifecycle partition: required":
        raise DispatchError(
            "DISPATCH_LIFECYCLE_PARTITION_INVALID",
            "Lifecycle partition must be 'required' or reasoned 'not-applicable'.",
        )
    table = _markdown_table(
        section,
        LIFECYCLE_COLUMNS,
        label="Lifecycle partition",
        missing_code="DISPATCH_LIFECYCLE_PARTITION_INVALID",
        invalid_code="DISPATCH_LIFECYCLE_PARTITION_INVALID",
    )
    records: list[LifecycleRecord] = []
    operations: set[str] = set()
    for row in table.rows:
        for column, cell in zip(LIFECYCLE_COLUMNS, row, strict=True):
            _require_value(cell, "DISPATCH_LIFECYCLE_PARTITION_INVALID", f"Lifecycle '{column}'")
        operation = _code_value(row[0])
        if IDENTIFIER.fullmatch(operation) is None or operation in operations:
            raise DispatchError(
                "DISPATCH_LIFECYCLE_PARTITION_INVALID",
                f"Lifecycle operation '{operation}' must be unique kebab-case.",
            )
        records.append(LifecycleRecord(operation, *row[1:]))
        operations.add(operation)
    return tuple(records)


def _header_string(header: Header, field: str, source: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", f"Brief review '{source}' needs string field '{field}'.")
    return value


def _review_metadata_bytes(data: bytes, source: str) -> BriefReviewMetadata:
    try:
        text = data.decode("utf-8")
        header = parse_header_text(text)
    except (UnicodeError, ValueError) as error:
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_INVALID", f"Cannot parse brief review '{source}': {error}"
        ) from error
    if header.get("kind") != "work-brief-review" or header.get("schema") != "repo-work/v2":
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_INVALID",
            "Brief review kind and schema must be 'work-brief-review' and 'repo-work/v2'.",
        )
    return BriefReviewMetadata(
        _header_string(header, "attempt", source),
        _header_string(header, "checkpoint", source),
        _header_string(header, "checkpoint_sha256", source),
        _header_string(header, "reviewed_authority_set_sha256", source),
        _header_string(header, "reviewer_task_id", source),
        _header_string(header, "status", source),
        _header_string(header, "verdict", source),
    )


def _validate_review_rows(data: bytes, source: str, coverage: tuple[CoverageRecord, ...]) -> None:
    try:
        review_lines = tuple(data.decode("utf-8").splitlines())
    except UnicodeError as error:
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", f"Cannot read brief review '{source}': {error}") from error
    table = _markdown_table(
        review_lines,
        BRIEF_REVIEW_COLUMNS,
        label="brief-review coverage",
        missing_code="DISPATCH_BRIEF_REVIEW_INCOMPLETE",
        invalid_code="DISPATCH_BRIEF_REVIEW_INCOMPLETE",
    )
    expected = {(record.reference, f"{record.owner.kind.value}:{record.owner.value}") for record in coverage}
    observed: set[tuple[str, str]] = set()
    for reference_cell, owner_cell, verdict, counterexample_result in table.rows:
        key = (_code_value(reference_cell), _code_value(owner_cell))
        if key not in expected or key in observed or verdict != "covered":
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_INCOMPLETE",
                f"Brief review row '{key[0]}' is missing, duplicated, unresolved, or not covered.",
            )
        _require_value(
            counterexample_result,
            "DISPATCH_BRIEF_REVIEW_INCOMPLETE",
            "Cheapest counterexample result",
        )
        observed.add(key)
    if observed != expected:
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_INCOMPLETE",
            "Brief review must contain exactly one covered row for every authoritative coverage row.",
        )


def _validate_brief_review_bytes(
    data: bytes,
    source: str,
    attempt_path: Path,
    attempt_id: str,
    checkpoint: str,
    section: tuple[str, ...],
    authority_table: bytes,
    coverage: tuple[CoverageRecord, ...],
) -> None:
    checkpoint_sha256 = hashlib.sha256(_normalized_section_bytes(section)).hexdigest()
    authority_set_sha256 = hashlib.sha256(authority_table).hexdigest()
    metadata = _review_metadata_bytes(data, source)
    if metadata.attempt != attempt_id or metadata.checkpoint != checkpoint:
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", "Brief review names a different attempt or checkpoint.")
    if (
        SHA256.fullmatch(metadata.checkpoint_sha256) is None
        or SHA256.fullmatch(metadata.reviewed_authority_set_sha256) is None
    ):
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", "Brief review digest fields must be lowercase SHA-256.")
    if (
        metadata.checkpoint_sha256 != checkpoint_sha256
        or metadata.reviewed_authority_set_sha256 != authority_set_sha256
    ):
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_STALE", "Brief review is not bound to the current checkpoint and sources."
        )
    try:
        attempt_header = parse_header(attempt_path)
    except (OSError, ParseError) as error:
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", f"Cannot read attempt owner: {error}") from error
    owner = attempt_header.get("owner_task_id", attempt_header.get("owner"))
    if not isinstance(owner, str) or not owner.strip():
        raise DispatchError("DISPATCH_BRIEF_REVIEW_INVALID", "The attempt has no concrete owner task identity.")
    reviewer_task_id = TaskId(metadata.reviewer_task_id.strip())
    owner_task_id = TaskId(owner.strip())
    if reviewer_task_id == owner_task_id:
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT",
            "The brief reviewer must be a different task from the attempt owner.",
        )
    if metadata.reviewer_task_id != reviewer_task_id or owner != owner_task_id:
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_INVALID",
            "Brief reviewer and attempt owner task identities must not contain surrounding whitespace.",
        )
    if metadata.status != "complete" or metadata.verdict != "ready":
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_NOT_READY",
            "Brief review must be complete with a ready verdict before dispatch.",
        )
    _validate_review_rows(data, source, coverage)


def _publish_or_read_brief_review(
    attempt_path: Path,
    checkpoint_sha256: str,
    candidate: bytes | None,
    review_id: str | None,
) -> tuple[bytes, str]:
    review_path = attempt_path.parent / "brief-reviews" / f"{checkpoint_sha256}.md"
    if candidate is None:
        if review_id is not None:
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
                "--review-id requires --brief-review.",
            )
        try:
            return review_path.read_bytes(), str(review_path)
        except OSError as error:
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_MISSING",
                f"Ready brief review '{review_path}' does not exist for the exact checkpoint bytes.",
            ) from error
    if review_id is None or REVIEW_ID.fullmatch(review_id) is None:
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
            "--brief-review requires one kebab-case --review-id.",
        )
    try:
        atomic_create(review_path, candidate)
        return candidate, str(review_path)
    except FileExistsError:
        try:
            existing = review_path.read_bytes()
        except OSError as error:
            raise DispatchError(
                "DISPATCH_BRIEF_REVIEW_INVALID", f"Cannot read existing ready review '{review_path}': {error}"
            ) from error
        if existing == candidate:
            return existing, str(review_path)
        rejected_path = review_path.parent / "rejected" / f"{checkpoint_sha256}-{review_id}.md"
        try:
            atomic_create(rejected_path, candidate)
        except FileExistsError:
            try:
                rejected = rejected_path.read_bytes()
            except OSError as error:
                raise DispatchError(
                    "DISPATCH_BRIEF_REVIEW_INVALID",
                    f"Cannot read existing rejected review '{rejected_path}': {error}",
                ) from error
            if rejected != candidate:
                raise DispatchError(
                    "DISPATCH_BRIEF_REVIEW_COLLISION",
                    f"Rejected review identity '{review_id}' already names different evidence.",
                ) from None
        raise DispatchError(
            "DISPATCH_BRIEF_REVIEW_COLLISION",
            f"Ready review '{review_path}' already exists with different evidence; later evidence is at '{rejected_path}'.",
        ) from None


def _validate_semantic_preservation(
    attempt_path: Path,
    attempt_id: str,
    checkpoint: str,
    section: tuple[str, ...],
    project_root: Path,
    contracts: tuple[ContractRecord, ...],
    brief_review: bytes | None,
    review_id: str | None,
    review_publisher: BriefReviewPublisher | None = None,
) -> None:
    authorities, authority_table = _reviewed_authorities(section)
    coverage = _coverage_records(section, authorities, contracts)
    _validate_lifecycle_partition(section)
    _validate_authority_digests(project_root, authorities)
    checkpoint_sha256 = hashlib.sha256(_normalized_section_bytes(section)).hexdigest()
    if brief_review is not None:
        _validate_brief_review_bytes(
            brief_review,
            "--brief-review",
            attempt_path,
            attempt_id,
            checkpoint,
            section,
            authority_table,
            coverage,
        )
    review_bytes, source = (
        review_publisher(checkpoint_sha256, brief_review, review_id)
        if review_publisher is not None
        else _publish_or_read_brief_review(attempt_path, checkpoint_sha256, brief_review, review_id)
    )
    _validate_brief_review_bytes(
        review_bytes,
        source,
        attempt_path,
        attempt_id,
        checkpoint,
        section,
        authority_table,
        coverage,
    )


def _checkpoint_boundary(section: tuple[str, ...]) -> CheckpointBoundary:
    values = tuple(
        line.removeprefix("Checkpoint boundary:").strip() for line in section if line.startswith("Checkpoint boundary:")
    )
    if len(values) != 1:
        raise DispatchError(
            "DISPATCH_BOUNDARY_MISSING",
            "The checkpoint must declare exactly one 'Checkpoint boundary: local' or 'cross-boundary'.",
        )
    try:
        return CheckpointBoundary(values[0])
    except ValueError as error:
        raise DispatchError(
            "DISPATCH_BOUNDARY_INVALID",
            f"Checkpoint boundary '{values[0]}' is not 'local' or 'cross-boundary'.",
        ) from error


def _require_current_dispatch_action(
    work_root: Path,
    project_root: Path,
    supplied: Action,
) -> None:
    if supplied.kind != ActionKind.DISPATCH:
        raise DispatchError("DISPATCH_ACTION_INVALID", "The supplied action is not a dispatch action.")
    authority = resolve_authority(work_root)
    match authority.version:
        case AuthorityVersion.V1:
            if coordinator_generation(work_root) != supplied.coordinator_generation:
                raise DispatchError("COORDINATOR_REPLACED", "A different coordinator generation now owns dispatch.")
        case AuthorityVersion.V2:
            try:
                require_coordination(authority.work_root, supplied.lease_id or "", supplied.coordinator_generation)
            except LeaseError as error:
                raise DispatchError(error.code, str(error).partition(": ")[2]) from error
        case _ as unreachable:
            assert_never(unreachable)
    current = next(
        (
            action
            for action in actions_for(
                work_root,
                project_root,
                "coordinator",
                lease_id=supplied.lease_id,
                generation=supplied.coordinator_generation,
            )
            if action.action_id == supplied.action_id
        ),
        None,
    )
    if current is None:
        raise DispatchError("DISPATCH_ACTION_UNAVAILABLE", f"Action '{supplied.action_id}' is not currently available.")
    if current.expected_revision != supplied.expected_revision:
        raise DispatchError("STALE_ACTION", "The work ledger changed after this dispatch action was selected.")


def _canonical_prompt(attempt_path: Path, attempt_id: str, checkpoint: str, environment: DispatchEnvironment) -> str:
    permissions = ", ".join(sorted(permission.value for permission in environment.permissions)) or "none"
    return (
        "Use $deliver for this repository attempt.\n\n"
        f"Attempt: {attempt_id}\n"
        f"Checkpoint: {checkpoint}\n"
        f"Canonical brief: {attempt_path}\n\n"
        "Read and follow that canonical attempt brief. It is the sole semantic execution contract. "
        "Do not restate, narrow, defer, or add acceptance semantics in this launch.\n\n"
        "Execution environment:\n"
        f"- Checkout: {environment.checkout}\n"
        f"- Branch: {environment.branch}\n"
        f"- Starting revision: {environment.starting_revision}\n"
        f"- Permissions: {permissions}\n"
    )


def prepare_dispatch_from_artifact(
    attempt_path: Path,
    attempt_id: str,
    attempt_branch: str,
    project_root: Path,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
    brief_review: bytes | None = None,
    review_id: str | None = None,
    review_publisher: BriefReviewPublisher | None = None,
) -> str:
    """Preserve the accepted brief contract after current authority was selected from SQLite."""

    if environment.branch != attempt_branch:
        raise DispatchError(
            "DISPATCH_BRANCH_MISMATCH",
            f"Environment branch '{environment.branch}' does not match attempt branch '{attempt_branch}'.",
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        raise DispatchError("DISPATCH_CHECKOUT_MISSING", f"Checkout '{checkout}' is not a directory.")
    section = _checkpoint_section(attempt_path, checkpoint)
    boundary = _checkpoint_boundary(section)
    match boundary:
        case CheckpointBoundary.LOCAL:
            if brief_review is not None or review_id is not None:
                raise DispatchError(
                    "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                )
        case CheckpointBoundary.CROSS_BOUNDARY:
            contracts = _validate_cross_boundary_checkpoint(section)
            _validate_semantic_preservation(
                attempt_path,
                attempt_id,
                checkpoint,
                section,
                project_root,
                contracts,
                brief_review,
                review_id,
                review_publisher,
            )
        case _ as unreachable:
            assert_never(unreachable)
    prompt = _canonical_prompt(attempt_path, attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        raise DispatchError(
            "DISPATCH_PROMPT_NOT_CANONICAL",
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt


def prepare_dispatch(
    work_root: Path,
    project_root: Path,
    action: Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
    brief_review: bytes | None = None,
    review_id: str | None = None,
) -> str:
    try:
        with authority_transaction(work_root) as authority:
            return _prepare_dispatch_locked(
                authority.work_root,
                work_root,
                project_root,
                action,
                checkpoint,
                environment,
                supplied_prompt,
                brief_review,
                review_id,
            )
    except PlatformNotSupportedError as error:
        message = str(error).partition(": ")[2] or str(error)
        raise DispatchError("PLATFORM_NOT_SUPPORTED", message) from error


def _prepare_dispatch_locked(
    work_root: Path,
    base_work_root: Path,
    project_root: Path,
    action: Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None,
    brief_review: bytes | None,
    review_id: str | None,
) -> str:
    _require_current_dispatch_action(base_work_root, project_root, action)
    attempt_path = work_root / "attempts" / action.subject / "attempt.md"
    attempt = parse_attempt(attempt_path)
    if attempt.state != AttemptState.ACTIVE:
        raise DispatchError("DISPATCH_ATTEMPT_NOT_ACTIVE", f"Attempt '{attempt.attempt}' is not active.")
    if environment.branch != attempt.branch:
        raise DispatchError(
            "DISPATCH_BRANCH_MISMATCH",
            f"Environment branch '{environment.branch}' does not match attempt branch '{attempt.branch}'.",
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        raise DispatchError("DISPATCH_CHECKOUT_MISSING", f"Checkout '{checkout}' is not a directory.")
    section = _checkpoint_section(attempt.path, checkpoint)
    boundary = _checkpoint_boundary(section)
    match boundary:
        case CheckpointBoundary.LOCAL:
            if brief_review is not None or review_id is not None:
                raise DispatchError(
                    "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                )
        case CheckpointBoundary.CROSS_BOUNDARY:
            contracts = _validate_cross_boundary_checkpoint(section)
            _validate_semantic_preservation(
                attempt.path,
                attempt.attempt,
                checkpoint,
                section,
                project_root,
                contracts,
                brief_review,
                review_id,
            )
        case _ as unreachable:
            assert_never(unreachable)
    prompt = _canonical_prompt(attempt.path, attempt.attempt, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        raise DispatchError(
            "DISPATCH_PROMPT_NOT_CANONICAL",
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt
