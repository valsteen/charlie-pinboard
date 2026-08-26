import hashlib
import json
import re
from pathlib import Path
from typing import Final, assert_never

import msgspec

from charlie_pinboard.application.dispatch_models import (
    BriefReviewPublisher,
    DispatchEnvironment,
)
from charlie_pinboard.application.errors import DispatchError, DispatchErrorCode
from charlie_pinboard.domain.identifiers import TaskId
from charlie_pinboard.interfaces.brief_source_models import AuthoritySelector
from charlie_pinboard.interfaces.brief_sources import parse_authority_selector, select_brief_source
from charlie_pinboard.interfaces.dispatch_brief_models import (
    AcceptedScopeAuthorizationBasis,
    ArchitectureImpact,
    ArchitectureImpactKind,
    AuthorityAuthorizationBasis,
    AuthorityFamily,
    AuthorityId,
    BriefOwner,
    BriefOwnerKind,
    BriefReviewMetadata,
    CheckpointBoundary,
    ContractRecord,
    CoverageDisposition,
    CoverageRecord,
    ExistingConsumerAuthorizationBasis,
    LifecycleRecord,
    MarkdownTable,
    RepositoryPolicyAuthorizationBasis,
    ReviewedAuthority,
)
from charlie_pinboard.interfaces.errors import (
    BriefSourceError,
    BriefSourceErrorCode,
    HeaderError,
    HeaderErrorCode,
)

type HeaderValue = str | bool | None
type Header = dict[str, HeaderValue]

HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CONTRACT_COLUMNS: Final = (
    "Invariant",
    "Authority / owner",
    "Required consumer or production observation",
    "Failure classification",
    "Exact verification",
    "Preflight / final revalidation",
    "Authorization basis",
)
EMPTY_CONTRACT_CELLS: Final = frozenset({"", "—", "-", "none", "null", "n/a", "tbd", "todo"})
TABLE_DELIMITER: Final = re.compile(r"^:?-{3,}:?$")
IDENTIFIER: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
AUTHORITY_REFERENCE: Final = re.compile(
    r"^authority:(?P<authority>[a-z0-9]+(?:-[a-z0-9]+)*)#(?P<family>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
ACCEPTED_SCOPE_REFERENCE: Final = re.compile(
    r"^accepted-scope:(?P<item>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<revision>[1-9][0-9]*)$"
)
REPOSITORY_POLICY_REFERENCE: Final = re.compile(
    r"^repository-policy:(?P<authority>[a-z0-9]+(?:-[a-z0-9]+)*)#(?P<family>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
EXISTING_CONSUMER_REFERENCE: Final = re.compile(
    r"^existing-consumer:(?P<authority>[a-z0-9]+(?:-[a-z0-9]+)*)#(?P<family>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
CRITERION_OWNER: Final = re.compile(r"^criterion:(?P<number>[1-9][0-9]*)$")
PROHIBITION: Final = re.compile(r"\b(?:must not|do not|cannot|never|prohibition|prohibited)\b", re.IGNORECASE)
DEFERRAL: Final = re.compile(
    r"^Deferral:\s+(?P<label>[a-z0-9]+(?:-[a-z0-9]+)*)\s+—\s+(?P<reason>.+?)\s+Reopen when:\s+(?P<reopen>.+)$"
)
ACCEPTANCE_CRITERION: Final = re.compile(r"^(?P<number>[1-9][0-9]*)\.\s+\S")
REVIEWED_AUTHORITY_COLUMNS: Final = ("Authority ID", "Selector", "Reviewed SHA-256", "In-scope families")
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


def _scalar(raw: str) -> HeaderValue:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if value in {"null", "~"}:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _parse_header_text(text: str) -> Header:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise HeaderError(HeaderErrorCode.MISSING, "Cannot parse fields without an opening delimiter.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise HeaderError(HeaderErrorCode.UNTERMINATED, "Cannot parse fields without a closing delimiter.") from error
    result: Header = {}
    for line_number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise HeaderError(HeaderErrorCode.FIELD_INVALID, f"Expected 'name: value' at line {line_number}.")
        key, raw = line.split(":", 1)
        key = key.strip()
        if not key:
            raise HeaderError(HeaderErrorCode.FIELD_INVALID, f"Header key is empty at line {line_number}.")
        if key in result:
            raise HeaderError(HeaderErrorCode.FIELD_DUPLICATE, f"Duplicate header field '{key}' at line {line_number}.")
        result[key] = _scalar(raw)
    return result


def _parse_header(path: Path) -> Header:
    return _parse_header_text(path.read_text(encoding="utf-8"))


def _validate_work_brief_header(path: Path, attempt_id: str) -> None:
    try:
        header = _parse_header(path)
    except (OSError, UnicodeError, HeaderError) as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID, f"Cannot read canonical work brief: {error}"
        ) from error
    if header.get("kind") != "work-attempt" or header.get("schema") != "pinboard-work-brief/v1":
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_INVALID,
            "Canonical work brief kind and schema must be 'work-attempt' and 'pinboard-work-brief/v1'.",
        )
    if header.get("attempt") != attempt_id:
        raise DispatchError(DispatchErrorCode.DISPATCH_BRIEF_INVALID, "Canonical work brief names a different attempt.")


def read_dispatch_environment(path: Path) -> DispatchEnvironment:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_UNREADABLE, f"Cannot read '{path}': {error}"
        ) from error
    try:
        return msgspec.json.decode(data, type=DispatchEnvironment)
    except msgspec.DecodeError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ENVIRONMENT_INVALID, f"Cannot decode dispatch environment: {error}"
        ) from error


def _checkpoint_section(path: Path, checkpoint: str) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = HEADING.fullmatch(line)
        if match is not None and match.group(2) == checkpoint:
            matches.append((index, len(match.group(1))))
    if not matches:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CHECKPOINT_MISSING, f"Checkpoint '{checkpoint}' is not in '{path}'."
        )
    if len(matches) != 1:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CHECKPOINT_AMBIGUOUS, f"Checkpoint '{checkpoint}' appears more than once."
        )
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


def _require_value(value: str, error_code: DispatchErrorCode, description: str) -> str:
    if value.casefold() in EMPTY_CONTRACT_CELLS:
        raise DispatchError(error_code, f"{description} must have a concrete value.")
    return value


def _markdown_table(
    lines: tuple[str, ...],
    columns: tuple[str, ...],
    *,
    label: str,
    missing_code: DispatchErrorCode,
    invalid_code: DispatchErrorCode,
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


def _source_authorization_key(
    match: re.Match[str], authorities: tuple[ReviewedAuthority, ...]
) -> tuple[AuthorityId, AuthorityFamily]:
    key = (AuthorityId(match.group("authority")), AuthorityFamily(match.group("family")))
    known = frozenset((authority.authority_id, family) for authority in authorities for family in authority.families)
    if key not in known:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CONTRACT_INVALID,
            f"Authorization basis references unknown authority family 'authority:{key[0]}#{key[1]}'.",
        )
    return key


def _authorization_basis(
    cell: str,
    accepted_item_id: str,
    accepted_scope_revision: int,
    authorities: tuple[ReviewedAuthority, ...],
) -> (
    AcceptedScopeAuthorizationBasis
    | AuthorityAuthorizationBasis
    | RepositoryPolicyAuthorizationBasis
    | ExistingConsumerAuthorizationBasis
):
    value = _code_value(cell)
    if (match := ACCEPTED_SCOPE_REFERENCE.fullmatch(value)) is not None:
        item_id = match.group("item")
        scope_revision = int(match.group("revision"))
        if item_id != accepted_item_id or scope_revision != accepted_scope_revision:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_CONTRACT_INVALID,
                "Accepted-scope authorization does not match the current attempt item and scope revision.",
            )
        return AcceptedScopeAuthorizationBasis(item_id, scope_revision)
    if (match := AUTHORITY_REFERENCE.fullmatch(value)) is not None:
        authority_id, family = _source_authorization_key(match, authorities)
        return AuthorityAuthorizationBasis(authority_id, family)
    if (match := REPOSITORY_POLICY_REFERENCE.fullmatch(value)) is not None:
        authority_id, family = _source_authorization_key(match, authorities)
        return RepositoryPolicyAuthorizationBasis(authority_id, family)
    if (match := EXISTING_CONSUMER_REFERENCE.fullmatch(value)) is not None:
        authority_id, family = _source_authorization_key(match, authorities)
        return ExistingConsumerAuthorizationBasis(authority_id, family)
    raise DispatchError(
        DispatchErrorCode.DISPATCH_CONTRACT_INVALID,
        "Authorization basis must be accepted-scope, authority, repository-policy, or existing-consumer.",
    )


def _contract_records(
    section: tuple[str, ...],
    accepted_item_id: str,
    accepted_scope_revision: int,
    authorities: tuple[ReviewedAuthority, ...],
) -> tuple[ContractRecord, ...]:
    table = _markdown_table(
        section,
        CONTRACT_COLUMNS,
        label="Contract",
        missing_code=DispatchErrorCode.DISPATCH_CONTRACT_MISSING,
        invalid_code=DispatchErrorCode.DISPATCH_CONTRACT_INVALID,
    )
    records: list[ContractRecord] = []
    for row_number, row in enumerate(table.rows, start=1):
        for column, cell in zip(CONTRACT_COLUMNS, row, strict=True):
            if cell.casefold() in EMPTY_CONTRACT_CELLS:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_CONTRACT_INCOMPLETE,
                    f"Contract row {row_number} has no concrete value for '{column}'.",
                )
        invariant, authority, consumer, failure, verification, revalidation, authorization_cell = row
        authorization_basis = _authorization_basis(
            authorization_cell,
            accepted_item_id,
            accepted_scope_revision,
            authorities,
        )
        records.append(
            ContractRecord(
                invariant,
                authority,
                consumer,
                failure,
                verification,
                revalidation,
                authorization_basis,
            )
        )
    return tuple(records)


def _validate_cross_boundary_checkpoint(section: tuple[str, ...]) -> None:
    if "Checkpoint outcome: independently-buildable" not in section:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_CHECKPOINT_NOT_BUILDABLE,
            "A cross-boundary checkpoint must record 'Checkpoint outcome: independently-buildable'.",
        )


def _authority_selector(cell: str) -> AuthoritySelector:
    value = _code_value(cell)
    try:
        return parse_authority_selector(value)
    except BriefSourceError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_SELECTOR_INVALID,
            error.message,
        ) from error


def _architecture_impact(section: tuple[str, ...]) -> ArchitectureImpact:
    declarations = tuple(line for line in section if line.startswith("Architecture impact:"))
    if len(declarations) != 1:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
            "The checkpoint must declare exactly one architecture impact.",
        )
    kind_value, separator, details = declarations[0].removeprefix("Architecture impact:").strip().partition(" — ")
    try:
        kind = ArchitectureImpactKind(kind_value)
    except ValueError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
            f"Architecture impact '{kind_value}' is not 'none', 'read-only', or 'update-required'.",
        ) from error
    if not separator:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
            "The architecture impact must include a nonempty reason.",
        )
    if kind == ArchitectureImpactKind.NONE:
        if details.casefold() in EMPTY_CONTRACT_CELLS or " — " in details:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
                "Architecture impact 'none' must contain only a nonempty reason.",
            )
        return ArchitectureImpact(kind, None, details)
    selector_value, selector_separator, reason = details.partition(" — ")
    if not selector_separator or reason.casefold() in EMPTY_CONTRACT_CELLS:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
            f"Architecture impact '{kind.value}' must name a project-relative authority selector and nonempty reason.",
        )
    try:
        selector = _authority_selector(selector_value)
    except DispatchError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_ARCHITECTURE_IMPACT_INVALID,
            f"Architecture impact '{kind.value}' has an invalid authority selector.",
        ) from error
    return ArchitectureImpact(kind, selector, reason)


def _reviewed_authorities(section: tuple[str, ...]) -> tuple[tuple[ReviewedAuthority, ...], bytes]:
    table = _markdown_table(
        section,
        REVIEWED_AUTHORITY_COLUMNS,
        label="Reviewed authorities",
        missing_code=DispatchErrorCode.DISPATCH_REVIEWED_AUTHORITIES_MISSING,
        invalid_code=DispatchErrorCode.DISPATCH_REVIEWED_AUTHORITIES_INVALID,
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
                DispatchErrorCode.DISPATCH_REVIEWED_AUTHORITIES_INVALID,
                f"Authority ID '{identifier}' must be unique kebab-case.",
            )
        if SHA256.fullmatch(digest) is None:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_REVIEWED_AUTHORITIES_INVALID,
                f"Authority '{identifier}' must carry a lowercase SHA-256 digest.",
            )
        if (
            not families
            or any(IDENTIFIER.fullmatch(value) is None for value in families)
            or len(set(families)) != len(families)
        ):
            raise DispatchError(
                DispatchErrorCode.DISPATCH_REVIEWED_AUTHORITIES_INVALID,
                f"Authority '{identifier}' must name one or more unique kebab-case families.",
            )
        seen_ids.add(identifier)
        records.append(ReviewedAuthority(identifier, _authority_selector(selector_value), digest, families))
    return tuple(records), table.serialized


def _authority_bytes(project_root: Path, authority: ReviewedAuthority) -> bytes:
    try:
        return select_brief_source(project_root, authority.selector, require_utf8=False).content
    except BriefSourceError as error:
        code = (
            DispatchErrorCode.DISPATCH_AUTHORITY_UNREADABLE
            if error.code == BriefSourceErrorCode.SOURCE_UNREADABLE
            else DispatchErrorCode.DISPATCH_AUTHORITY_SELECTOR_INVALID
        )
        raise DispatchError(
            code,
            f"Cannot select authority '{authority.authority_id}': {error.message}",
        ) from error


def _validate_authority_digests(project_root: Path, authorities: tuple[ReviewedAuthority, ...]) -> None:
    for authority in authorities:
        observed = hashlib.sha256(_authority_bytes(project_root, authority)).hexdigest()
        if observed != authority.reviewed_sha256:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_AUTHORITY_STALE,
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
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
            "A cross-boundary checkpoint must have one Acceptance criteria section.",
        )
    start = headings[0]
    level_match = HEADING.fullmatch(section[start])
    if level_match is None:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Acceptance criteria heading is invalid."
        )
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
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
            "Acceptance criteria must use unique positive numbered rows.",
        )
    return frozenset(numbers)


def _deferrals(section: tuple[str, ...]) -> frozenset[str]:
    labels: list[str] = []
    for line in section:
        match = DEFERRAL.fullmatch(line)
        if match is None:
            continue
        _require_value(match.group("reason"), DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Deferral reason")
        _require_value(
            match.group("reopen"), DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Deferral reopen condition"
        )
        labels.append(match.group("label"))
    if len(set(labels)) != len(labels):
        raise DispatchError(DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Deferral labels must be unique.")
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
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, f"Brief owner '{value}' is unresolved."
        )
    try:
        kind = BriefOwnerKind(prefix)
    except ValueError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, f"Brief owner '{value}' is unresolved."
        ) from error
    expected_kind = {
        CoverageDisposition.CONTRACT: BriefOwnerKind.CONTRACT,
        CoverageDisposition.ACCEPTANCE: BriefOwnerKind.CRITERION,
        CoverageDisposition.DEFERRED: BriefOwnerKind.DEFERRAL,
        CoverageDisposition.NOT_APPLICABLE: BriefOwnerKind.REASON,
    }[disposition]
    if kind != expected_kind:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
            f"Disposition '{disposition.value}' cannot use owner '{value}'.",
        )
    if kind == BriefOwnerKind.CONTRACT and owner_value not in contracts:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, f"Contract owner '{owner_value}' does not exist."
        )
    if kind == BriefOwnerKind.CRITERION:
        criterion = CRITERION_OWNER.fullmatch(value)
        if criterion is None or int(criterion.group("number")) not in criteria:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, f"Criterion owner '{value}' does not exist."
            )
    if kind == BriefOwnerKind.DEFERRAL and owner_value not in deferrals:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, f"Deferral owner '{owner_value}' does not exist."
        )
    if kind == BriefOwnerKind.REASON:
        _require_value(owner_value, DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Not-applicable reason")
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
        missing_code=DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_MISSING,
        invalid_code=DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
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
                DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
                f"Coverage reference '{reference}' must be 'authority:<id>#<family>'.",
            )
        key = (AuthorityId(match.group("authority")), AuthorityFamily(match.group("family")))
        if key not in expected or key in seen:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
                f"Coverage reference '{reference}' is unknown or duplicated.",
            )
        try:
            disposition = CoverageDisposition(_code_value(disposition_cell))
        except ValueError as error:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
                f"Coverage disposition '{disposition_cell}' is not allowed.",
            ) from error
        _require_value(distinction, DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Required distinction")
        _require_value(consumer, DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Required consumer")
        _require_value(counterexample, DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID, "Cheapest counterexample")
        if disposition in {CoverageDisposition.DEFERRED, CoverageDisposition.NOT_APPLICABLE} and PROHIBITION.search(
            distinction
        ):
            raise DispatchError(
                DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
                f"In-scope prohibition '{reference}' cannot be deferred or marked not-applicable.",
            )
        owner = _brief_owner(owner_cell, disposition, contract_invariants, criteria, deferrals)
        records.append(CoverageRecord(*key, distinction, consumer, disposition, owner, counterexample))
        seen.add(key)
    if seen != expected:
        missing = ", ".join(f"authority:{authority}#{family}" for authority, family in sorted(expected - seen))
        raise DispatchError(
            DispatchErrorCode.DISPATCH_AUTHORITY_COVERAGE_INVALID,
            f"Authoritative coverage is incomplete; missing: {missing}.",
        )
    return tuple(records)


def _validate_lifecycle_partition(section: tuple[str, ...]) -> tuple[LifecycleRecord, ...]:
    declarations = tuple(line for line in section if line.startswith("Lifecycle partition:"))
    if len(declarations) != 1:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID,
            "A cross-boundary checkpoint must declare exactly one lifecycle partition disposition.",
        )
    declaration = declarations[0]
    if declaration.startswith("Lifecycle partition: not-applicable —"):
        reason = declaration.removeprefix("Lifecycle partition: not-applicable —").strip()
        _require_value(
            reason, DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID, "Lifecycle not-applicable reason"
        )
        return ()
    if declaration != "Lifecycle partition: required":
        raise DispatchError(
            DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID,
            "Lifecycle partition must be 'required' or reasoned 'not-applicable'.",
        )
    table = _markdown_table(
        section,
        LIFECYCLE_COLUMNS,
        label="Lifecycle partition",
        missing_code=DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID,
        invalid_code=DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID,
    )
    records: list[LifecycleRecord] = []
    operations: set[str] = set()
    for row in table.rows:
        for column, cell in zip(LIFECYCLE_COLUMNS, row, strict=True):
            _require_value(cell, DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID, f"Lifecycle '{column}'")
        operation = _code_value(row[0])
        if IDENTIFIER.fullmatch(operation) is None or operation in operations:
            raise DispatchError(
                DispatchErrorCode.DISPATCH_LIFECYCLE_PARTITION_INVALID,
                f"Lifecycle operation '{operation}' must be unique kebab-case.",
            )
        records.append(LifecycleRecord(operation, *row[1:]))
        operations.add(operation)
    return tuple(records)


def _header_string(header: Header, field: str, source: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, f"Brief review '{source}' needs string field '{field}'."
        )
    return value


def _review_metadata_bytes(data: bytes, source: str) -> BriefReviewMetadata:
    try:
        text = data.decode("utf-8")
        header = _parse_header_text(text)
    except (UnicodeError, HeaderError) as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, f"Cannot parse brief review '{source}': {error}"
        ) from error
    if header.get("kind") != "work-brief-review" or header.get("schema") != "pinboard-work-brief-review/v1":
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
            "Brief review kind and schema must be 'work-brief-review' and 'pinboard-work-brief-review/v1'.",
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
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, f"Cannot read brief review '{source}': {error}"
        ) from error
    table = _markdown_table(
        review_lines,
        BRIEF_REVIEW_COLUMNS,
        label="brief-review coverage",
        missing_code=DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INCOMPLETE,
        invalid_code=DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INCOMPLETE,
    )
    expected = {
        (
            f"authority:{record.authority_id}#{record.family}",
            f"{record.owner.kind.value}:{record.owner.value}",
        )
        for record in coverage
    }
    observed: set[tuple[str, str]] = set()
    for reference_cell, owner_cell, verdict, counterexample_result in table.rows:
        key = (_code_value(reference_cell), _code_value(owner_cell))
        if key not in expected or key in observed or verdict != "covered":
            raise DispatchError(
                DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INCOMPLETE,
                f"Brief review row '{key[0]}' is missing, duplicated, unresolved, or not covered.",
            )
        _require_value(
            counterexample_result,
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INCOMPLETE,
            "Cheapest counterexample result",
        )
        observed.add(key)
    if observed != expected:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INCOMPLETE,
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
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, "Brief review names a different attempt or checkpoint."
        )
    if (
        SHA256.fullmatch(metadata.checkpoint_sha256) is None
        or SHA256.fullmatch(metadata.reviewed_authority_set_sha256) is None
    ):
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, "Brief review digest fields must be lowercase SHA-256."
        )
    if (
        metadata.checkpoint_sha256 != checkpoint_sha256
        or metadata.reviewed_authority_set_sha256 != authority_set_sha256
    ):
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_STALE,
            "Brief review is not bound to the current checkpoint and sources.",
        )
    try:
        attempt_header = _parse_header(attempt_path)
    except (OSError, ValueError) as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, f"Cannot read attempt owner: {error}"
        ) from error
    owner = attempt_header.get("owner_task_id", attempt_header.get("owner"))
    if not isinstance(owner, str) or not owner.strip():
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID, "The attempt has no concrete owner task identity."
        )
    reviewer_task_id = TaskId(metadata.reviewer_task_id.strip())
    owner_task_id = TaskId(owner.strip())
    if reviewer_task_id == owner_task_id:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT,
            "The brief reviewer must be a different task from the attempt owner.",
        )
    if metadata.reviewer_task_id != reviewer_task_id or owner != owner_task_id:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_INVALID,
            "Brief reviewer and attempt owner task identities must not contain surrounding whitespace.",
        )
    if metadata.status != "complete" or metadata.verdict != "ready":
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_NOT_READY,
            "Brief review must be complete with a ready verdict before dispatch.",
        )
    _validate_review_rows(data, source, coverage)


def _validate_semantic_preservation(
    attempt_path: Path,
    attempt_id: str,
    checkpoint: str,
    section: tuple[str, ...],
    project_root: Path,
    accepted_item_id: str,
    accepted_scope_revision: int,
    brief_review: bytes | None,
    review_id: str | None,
    review_publisher: BriefReviewPublisher | None = None,
) -> None:
    authorities, authority_table = _reviewed_authorities(section)
    contracts = _contract_records(section, accepted_item_id, accepted_scope_revision, authorities)
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
    if review_publisher is None:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRIEF_REVIEW_MISSING,
            "Current SQLite dispatch requires application-owned review publication.",
        )
    review_bytes, source = review_publisher(checkpoint_sha256, brief_review, review_id)
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
            DispatchErrorCode.DISPATCH_BOUNDARY_MISSING,
            "The checkpoint must declare exactly one 'Checkpoint boundary: local' or 'cross-boundary'.",
        )
    try:
        return CheckpointBoundary(values[0])
    except ValueError as error:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BOUNDARY_INVALID,
            f"Checkpoint boundary '{values[0]}' is not 'local' or 'cross-boundary'.",
        ) from error


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
    *,
    accepted_item_id: str | None = None,
    accepted_scope_revision: int | None = None,
    supplied_prompt: bytes | None = None,
    brief_review: bytes | None = None,
    review_id: str | None = None,
    review_publisher: BriefReviewPublisher | None = None,
) -> str:
    """Preserve the accepted brief contract after current authority was selected from SQLite."""

    _validate_work_brief_header(attempt_path, attempt_id)
    if environment.branch != attempt_branch:
        raise DispatchError(
            DispatchErrorCode.DISPATCH_BRANCH_MISMATCH,
            f"Environment branch '{environment.branch}' does not match attempt branch '{attempt_branch}'.",
        )
    checkout = Path(environment.checkout)
    if not checkout.is_dir():
        raise DispatchError(DispatchErrorCode.DISPATCH_CHECKOUT_MISSING, f"Checkout '{checkout}' is not a directory.")
    section = _checkpoint_section(attempt_path, checkpoint)
    _architecture_impact(section)
    boundary = _checkpoint_boundary(section)
    match boundary:
        case CheckpointBoundary.LOCAL:
            if brief_review is not None or review_id is not None:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID,
                    "Local checkpoints do not publish cross-boundary brief reviews.",
                )
        case CheckpointBoundary.CROSS_BOUNDARY:
            _validate_cross_boundary_checkpoint(section)
            if accepted_item_id is None or accepted_scope_revision is None:
                raise DispatchError(
                    DispatchErrorCode.DISPATCH_CONTRACT_INVALID,
                    "Cross-boundary dispatch requires the current attempt item and accepted scope revision.",
                )
            _validate_semantic_preservation(
                attempt_path,
                attempt_id,
                checkpoint,
                section,
                project_root,
                accepted_item_id,
                accepted_scope_revision,
                brief_review,
                review_id,
                review_publisher,
            )
        case _ as unreachable:
            assert_never(unreachable)
    prompt = _canonical_prompt(attempt_path, attempt_id, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        raise DispatchError(
            DispatchErrorCode.DISPATCH_PROMPT_NOT_CANONICAL,
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt
