import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Final, Literal

import msgspec

from repo_work.actions import Action, actions_for, coordinator_generation
from repo_work.atomic import PlatformNotSupportedError, transition_lock
from repo_work.markdown import parse_attempt

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"^[^\n]+$")]
type DispatchSchema = Literal["repo-work-dispatch/v1"]

HEADING: Final = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
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


class DispatchError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class DispatchPermission(Enum):
    REPOSITORY_READ = "repository-read"
    REPOSITORY_WRITE = "repository-write"
    NETWORK = "network"
    EXTERNAL_WRITE = "external-write"
    LIVE_APPLICATION = "live-application"


class CheckpointBoundary(Enum):
    LOCAL = "local"
    CROSS_BOUNDARY = "cross-boundary"


class DispatchEnvironment(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: DispatchSchema
    checkout: NonEmptyLine
    branch: NonEmptyLine
    starting_revision: NonEmptyLine
    permissions: tuple[DispatchPermission, ...]


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


def _contract_rows(section: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    start = next((index for index, line in enumerate(section) if _table_cells(line) == CONTRACT_COLUMNS), None)
    if start is None:
        raise DispatchError(
            "DISPATCH_CONTRACT_MISSING",
            "A cross-boundary checkpoint needs the six-column Contract table in its canonical brief.",
        )
    separator = _table_cells(section[start + 1]) if start + 1 < len(section) else ()
    if len(separator) != len(CONTRACT_COLUMNS) or any(TABLE_DELIMITER.fullmatch(cell) is None for cell in separator):
        raise DispatchError("DISPATCH_CONTRACT_INVALID", "The Contract table separator is missing.")
    rows: list[tuple[str, ...]] = []
    for line in section[start + 2 :]:
        cells = _table_cells(line)
        if not cells:
            break
        if len(cells) != len(CONTRACT_COLUMNS):
            raise DispatchError(
                "DISPATCH_CONTRACT_INVALID",
                f"A Contract table row has {len(cells)} cells; expected {len(CONTRACT_COLUMNS)}.",
            )
        rows.append(cells)
    if not rows:
        raise DispatchError("DISPATCH_CONTRACT_MISSING", "The Contract table has no invariant rows.")
    return tuple(rows)


def _validate_cross_boundary_checkpoint(section: tuple[str, ...]) -> None:
    if "Checkpoint outcome: independently-buildable" not in section:
        raise DispatchError(
            "DISPATCH_CHECKPOINT_NOT_BUILDABLE",
            "A cross-boundary checkpoint must record 'Checkpoint outcome: independently-buildable'.",
        )
    for row_number, row in enumerate(_contract_rows(section), start=1):
        for column, cell in zip(CONTRACT_COLUMNS, row, strict=True):
            if cell.casefold() in EMPTY_CONTRACT_CELLS:
                raise DispatchError(
                    "DISPATCH_CONTRACT_INCOMPLETE",
                    f"Contract row {row_number} has no concrete value for '{column}'.",
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
    if supplied.kind != "dispatch":
        raise DispatchError("DISPATCH_ACTION_INVALID", "The supplied action is not a dispatch action.")
    if coordinator_generation(work_root) != supplied.coordinator_generation:
        raise DispatchError("COORDINATOR_REPLACED", "A different coordinator generation now owns dispatch.")
    current = next(
        (
            action
            for action in actions_for(work_root, project_root, "coordinator")
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
        "Use $bounded-implementer for this repository attempt.\n\n"
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


def prepare_dispatch(
    work_root: Path,
    project_root: Path,
    action: Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None = None,
) -> str:
    try:
        with transition_lock(work_root):
            return _prepare_dispatch_locked(
                work_root,
                project_root,
                action,
                checkpoint,
                environment,
                supplied_prompt,
            )
    except PlatformNotSupportedError as error:
        message = str(error).partition(": ")[2] or str(error)
        raise DispatchError("PLATFORM_NOT_SUPPORTED", message) from error


def _prepare_dispatch_locked(
    work_root: Path,
    project_root: Path,
    action: Action,
    checkpoint: str,
    environment: DispatchEnvironment,
    supplied_prompt: bytes | None,
) -> str:
    _require_current_dispatch_action(work_root, project_root, action)
    attempt_path = work_root / "attempts" / action.subject / "attempt.md"
    attempt = parse_attempt(attempt_path)
    if attempt.state != "active":
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
    if _checkpoint_boundary(section) == CheckpointBoundary.CROSS_BOUNDARY:
        _validate_cross_boundary_checkpoint(section)
    prompt = _canonical_prompt(attempt.path, attempt.attempt, checkpoint, environment)
    if supplied_prompt is not None and supplied_prompt != prompt.encode():
        raise DispatchError(
            "DISPATCH_PROMPT_NOT_CANONICAL",
            "The launch adds or changes instructions outside the canonical attempt brief; render and use the exact prompt.",
        )
    return prompt
