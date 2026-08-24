import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, cast

import msgspec

from charlie_pinboard import __version__
from charlie_pinboard.adapters.files.artifacts import ArtifactRepository
from charlie_pinboard.adapters.files.file_io import DurableRoots, atomic_replace
from charlie_pinboard.adapters.files.views import rebuild as rebuild_views
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.artifacts import NewArtifact
from charlie_pinboard.application.stored_state import (
    ArtifactKind,
    ArtifactRecords,
    ArtifactReference,
    AttemptLeaseCounter,
    AttemptLeaseGeneration,
    AttemptLeaseState,
    AuthorityRecords,
    CoordinationLeaseState,
    HistoryRecords,
    ItemArtifactLink,
    ItemDependency,
    ItemScopeRevision,
    LifecycleRecords,
    OriginKind,
    PlanningRecords,
    ProjectRecord,
    ProposalDisposition,
    ProposalEvidence,
    ProposalFreshness,
    ProposalRecords,
    ProposalRelation,
    ResourceRecords,
    StoredAttempt,
    StoredAttemptLease,
    StoredCoordinationLease,
    StoredFocus,
    StoredProposal,
    StoredTransitionReceipt,
    StoredWorkItem,
    StoredWorkItemState,
    StoredWorkState,
    TransitionHistoryActionKind,
    TransitionHistoryAuthorizationKind,
)
from charlie_pinboard.domain.errors import DecisionFailure
from charlie_pinboard.domain.history import item_scope_digest
from charlie_pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from charlie_pinboard.domain.model import (
    ArtifactRole,
    CanonicalJson,
    ItemScope,
    ScopeArtifact,
    ScopeDependency,
    Timing,
    WorkState,
)
from charlie_pinboard.legacy.authority import AuthorityVersion, resolve_authority
from charlie_pinboard.legacy.leases import read_attempt_lease, read_coordination_lease
from charlie_pinboard.legacy.markdown import (
    QueueItem,
    WorkItemRecord,
    parse_attempt,
    parse_current,
    parse_header,
    parse_item,
    parse_queue,
)
from charlie_pinboard.legacy.proposals import (
    Proposal,
    ProposalDispositionKind,
    ProposalHistory,
    read_proposal,
)
from charlie_pinboard.legacy.proposals import (
    ProposalRelation as LegacyProposalRelation,
)
from charlie_pinboard.legacy.validate import validate_work_state

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]


class _FlatProposalHistory(msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True):
    schema: Literal["repo-work/v1"]
    proposal_id: NonEmptyString
    created_at: NonEmptyString
    source_task_id: NonEmptyString
    user_label: NonEmptyString
    trigger: NonEmptyString
    evidence: tuple[NonEmptyString, ...]
    why_it_matters: NonEmptyString
    relation: LegacyProposalRelation
    effect: NonEmptyString
    unlock: NonEmptyString
    urgency_evidence: NonEmptyString
    freshness_assumptions: tuple[NonEmptyString, ...]
    disposition: ProposalDispositionKind
    target: str | None
    coordinator_reason: str | None = None


class _ImportCountRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    items: int
    attempts: int
    proposals: int
    artifacts: int
    resources: int
    item_resources: int
    claims: int


class _ImportOutcomeRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    cutover_id: str
    source_revision: str
    source_schema: Literal["repo-work/v2"]
    importer_version: str
    destination_revision: int
    manifest_selector: str
    manifest_sha256: str
    counts: _ImportCountRecord


class _ManifestEntryRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source_selector: str
    source_sha256: str
    source_size: int
    classification: str
    transformation: str
    target_kind: str | None
    target_key: str | None
    target_revision: int | None
    target_selector: str | None
    target_sha256: str | None
    target_size: int | None
    owning_item: str | None
    owning_attempt: str | None
    role: str | None
    position: int | None


class _ManifestRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["repo-work-mapping-manifest/v1"]
    cutover_id: str
    source_schema: Literal["repo-work/v2"]
    source_revision: str
    entries: tuple[_ManifestEntryRecord, ...]


class _SourceSnapshotEntryRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    selector: str
    classification: str
    sha256: str
    size: int


class _SourceSnapshotRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["repo-work-source-snapshot/v1"]
    source_authority_schema: Literal["repo-work-authority/v1"]
    source_revision: str
    entries: tuple[_SourceSnapshotEntryRecord, ...]


_SOURCE_SNAPSHOT_SCHEMA = "repo-work-source-snapshot/v1"
_MANIFEST_SCHEMA = "repo-work-mapping-manifest/v1"
_TRANSFORMATION_VERSION = "legacy-markdown-to-sqlite-v1"
CUTOVER_TOMBSTONE = (
    b'{\n  "schema": "repo-work-authority/v2",\n  "current": "sqlite-v1",\n  "database": "state.sqlite3"\n}\n'
)
_INACTIVE_ROOTS = frozenset(
    {
        "queue.md",
        "current.md",
        "coordinator.json",
        "items",
        "attempts",
        "history",
        "inbox",
        "accept-parallel.json",
        "activate-parallel.json",
        "pause-receipts.json",
    }
)


class LegacyImportError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ImportCounts:
    items: int
    attempts: int
    proposals: int
    artifacts: int
    resources: int = 0
    item_resources: int = 0
    claims: int = 0


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    cutover_id: str
    source_revision: str
    destination_revision: int
    manifest_selector: str
    manifest_sha256: str
    source_snapshot: bytes
    counts: ImportCounts


@dataclass(frozen=True, slots=True)
class _SourceEntry:
    selector: str
    classification: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class _PendingArtifact:
    source_selector: str
    kind: ArtifactKind
    key: str
    suffix: str
    content: bytes
    item_id: ItemId | None
    attempt_id: AttemptId | None
    role: ArtifactRole | None

    @property
    def selector(self) -> str:
        directory = {
            ArtifactKind.REQUIREMENTS: "requirements",
            ArtifactKind.PLAN: "plans",
            ArtifactKind.DESIGN: "designs",
            ArtifactKind.BRIEF: "briefs",
            ArtifactKind.RESULT: "results",
            ArtifactKind.BLOCKER: "blockers",
            ArtifactKind.EVIDENCE: "evidence",
        }[self.kind]
        return PurePosixPath("artifacts", directory, self.key, f"1{self.suffix}").as_posix()


@dataclass(frozen=True, slots=True)
class _ItemSource:
    path: Path
    record: WorkItemRecord
    queue: QueueItem
    header: dict[str, str | bool | None]
    body: bytes


@dataclass(frozen=True, slots=True)
class _PreparedImport:
    receipt: ImportReceipt
    state: StoredWorkState
    artifacts: tuple[_PendingArtifact, ...]
    manifest: bytes


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _timestamp(value: str, *, selector: str) -> datetime:
    if len(value) == 10:
        try:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
        except ValueError as error:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} has invalid timestamp {value!r}.") from error
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as error:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} has invalid timestamp {value!r}.") from error
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
    if parsed.tzinfo is None:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} has a naive timestamp {value!r}.")
    return parsed.astimezone(UTC)


def _required_string(header: dict[str, str | bool | None], field: str, selector: str) -> str:
    value = header.get(field)
    if not isinstance(value, str) or not value or value == "—":
        raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} requires nonempty {field}.")
    return value


def _optional_string(header: dict[str, str | bool | None], field: str) -> str | None:
    value = header.get(field)
    return value if isinstance(value, str) and value and value != "—" else None


def _list_value(header: dict[str, str | bool | None], field: str) -> tuple[str, ...]:
    value = _optional_string(header, field)
    return () if value is None else tuple(part.strip() for part in value.split(",") if part.strip())


def _body(data: bytes, selector: str) -> bytes:
    lines = data.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} has no frontmatter.")
    closing: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip(b"\r\n") == b"---":
            closing = index
            break
    if closing is None:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{selector} has unterminated frontmatter.")
    remainder = b"".join(lines[closing + 1 :])
    if remainder.startswith(b"\r\n"):
        return remainder[2:]
    if remainder.startswith(b"\n"):
        return remainder[1:]
    return remainder


def _file_entry(base: Path, path: Path, classification: str) -> _SourceEntry:
    data = path.read_bytes()
    return _SourceEntry(path.relative_to(base).as_posix(), classification, hashlib.sha256(data).hexdigest(), len(data))


def _source_path_key(path: Path, base: Path) -> bytes:
    return path.relative_to(base).as_posix().encode()


def _selected_classification(inside: Path, selector: str) -> str:
    if inside in {Path("queue.md"), Path("migration-complete.md")}:
        return "validation-only-generated"
    if inside == Path("current.md") or inside == Path("leases/coordination.md"):
        return "consumed-authority"
    parts = inside.parts
    match parts:
        case ("items", filename) if filename.endswith(".md"):
            return "consumed-record"
        case ("inbox", filename) if filename.endswith(".json"):
            return "consumed-record"
        case ("history", "items", filename) if filename.endswith(".md"):
            return "consumed-record"
        case ("history", "proposals", filename) if filename.endswith(".json"):
            return "consumed-record"
        case ("attempts", _, "attempt.md"):
            return "consumed-record"
        case ("attempts", _, *_) if len(parts) >= 3:
            return "consumed-artifact"
        case ("resources", *_):
            return "legacy-resource-state"
        case ("leases", "resources", *_):
            return "legacy-resource-state"
        case _:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Unclassified selected-v2 selector: {selector}.")


def _classify_files(base: Path, selected: Path) -> tuple[_SourceEntry, ...]:
    entries: list[_SourceEntry] = []
    selected_relative = selected.relative_to(base)
    paths = (candidate for candidate in base.rglob("*") if candidate.is_file())

    def source_path_key(value: Path) -> bytes:
        return _source_path_key(value, base)

    for path in sorted(paths, key=source_path_key):
        relative = path.relative_to(base)
        selector = relative.as_posix()
        if path.name == ".DS_Store":
            classification = "ignored-platform-metadata"
        elif relative == Path("authority.json"):
            classification = "validation-only-authority"
        elif relative.is_relative_to(selected_relative):
            inside = relative.relative_to(selected_relative)
            classification = _selected_classification(inside, selector)
        elif relative.parts and relative.parts[0] in _INACTIVE_ROOTS:
            classification = "superseded-by-selected-v2"
        else:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Unclassified legacy selector: {selector}.")
        entries.append(_file_entry(base, path, classification))
    return tuple(entries)


def _source_snapshot(entries: tuple[_SourceEntry, ...], source_revision: str) -> bytes:
    value = _SourceSnapshotRecord(
        _SOURCE_SNAPSHOT_SCHEMA,
        "repo-work-authority/v1",
        source_revision,
        tuple(
            _SourceSnapshotEntryRecord(entry.selector, entry.classification, entry.sha256, entry.size)
            for entry in entries
        ),
    )
    return msgspec.json.encode(value, order="sorted") + b"\n"


def _selected_source(project_root: Path, base_work_root: Path) -> Path:
    authority = resolve_authority(base_work_root)
    if authority.version != AuthorityVersion.V2:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "The source must select one schema-v2 authority.")
    selected = authority.work_root
    if selected.parent != base_work_root.resolve() or selected.name != "v2":
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "The source authority must select the canonical v2 tree.")
    try:
        selected.relative_to(project_root.resolve())
    except ValueError as error:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "The selected source is outside its project root.") from error
    return selected


def _queue_for_terminal(path: Path) -> QueueItem:
    header = parse_header(path)
    state_value = _required_string(header, "state", str(path))
    try:
        state = StoredWorkItemState(state_value)
    except ValueError as error:
        raise LegacyImportError(
            "LEGACY_SOURCE_INVALID", f"{path} has unsupported terminal state {state_value!r}."
        ) from error
    if state not in {StoredWorkItemState.DONE, StoredWorkItemState.SUPERSEDED, StoredWorkItemState.DROPPED}:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{path} is not a terminal history item.")
    return QueueItem(
        item=_required_string(header, "item", str(path)),
        state=cast(WorkState, state),
        timing=_optional_string(header, "timing"),
        depends_on=_list_value(header, "depends_on"),
        attempt=_optional_string(header, "attempt"),
        source=_optional_string(header, "source") or "",
        next_action=_optional_string(header, "next_action"),
        notes=_optional_string(header, "notes") or "",
        outcome_evidence=_required_string(header, "evidence", str(path)),
    )


def _item_sources(selected: Path) -> tuple[_ItemSource, ...]:
    queue = parse_queue(selected / "queue.md")
    live_rows = queue.by_id()
    sources: list[_ItemSource] = []
    for path in sorted((selected / "items").glob("*.md")):
        record = parse_item(path)
        row = live_rows.get(record.item)
        if row is None or record.queue_item != row:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{path} contradicts queue.md.")
        sources.append(_ItemSource(path, record, row, parse_header(path), _body(path.read_bytes(), str(path))))
    if set(live_rows) != {source.record.item for source in sources}:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "queue.md and selected live item files differ.")
    for path in sorted((selected / "history" / "items").glob("*.md")):
        header = parse_header(path)
        if header.get("kind") != "work-history" or header.get("schema") != "repo-work/v2":
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{path} is not a schema-v2 history item.")
        record = WorkItemRecord(
            path,
            _required_string(header, "item", str(path)),
            _required_string(header, "user_label", str(path)),
        )
        row = _queue_for_terminal(path)
        if record.item != row.item:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"{path} has mismatched item identity.")
        sources.append(_ItemSource(path, record, row, header, _body(path.read_bytes(), str(path))))
    identities = [source.record.item for source in sources]
    if len(identities) != len(set(identities)):
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "Live and terminal item identities overlap.")
    return tuple(sources)


def _proposal_values(selected: Path, now: datetime) -> tuple[ProposalRecords, dict[str, Proposal]]:
    proposals: list[StoredProposal] = []
    evidence: list[ProposalEvidence] = []
    freshness: list[ProposalFreshness] = []
    source_by_id: dict[str, Proposal] = {}
    paths = [*(selected / "inbox").glob("*.json"), *(selected / "history" / "proposals").glob("*.json")]
    for path in sorted(paths):
        history: ProposalHistory | None = None
        flat_history: _FlatProposalHistory | None = None
        try:
            if path.is_relative_to(selected / "history" / "proposals"):
                data = path.read_bytes()
                try:
                    history = msgspec.json.decode(data, type=ProposalHistory)
                except msgspec.DecodeError:
                    flat_history = msgspec.json.decode(data, type=_FlatProposalHistory)
                    proposal = Proposal(
                        flat_history.schema,
                        flat_history.proposal_id,
                        flat_history.created_at,
                        flat_history.source_task_id,
                        flat_history.user_label,
                        flat_history.trigger,
                        flat_history.evidence,
                        flat_history.why_it_matters,
                        flat_history.relation,
                        flat_history.effect,
                        flat_history.unlock,
                        flat_history.urgency_evidence,
                        flat_history.freshness_assumptions,
                    )
                else:
                    proposal = history.proposal
            else:
                proposal = read_proposal(path)
        except (msgspec.DecodeError, ValueError) as error:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Cannot decode proposal {path}.") from error
        if proposal.proposal_id in source_by_id:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Duplicate proposal {proposal.proposal_id}.")
        source_by_id[proposal.proposal_id] = proposal
        legacy_disposition = (
            history.disposition if history is not None else None if flat_history is None else flat_history.disposition
        )
        disposition = None if legacy_disposition is None else ProposalDisposition(legacy_disposition.value)
        disposition_target = (
            history.target if history is not None else None if flat_history is None else flat_history.target
        )
        disposition_reason = (
            history.coordinator_reason
            if history is not None
            else None
            if flat_history is None
            else flat_history.coordinator_reason
        )
        proposals.append(
            StoredProposal(
                ProposalId(proposal.proposal_id),
                OriginKind.LEGACY_IMPORT,
                _timestamp(proposal.created_at, selector=str(path)),
                now,
                TaskId(proposal.source_task_id),
                proposal.user_label,
                proposal.trigger,
                proposal.why_it_matters,
                ProposalRelation(proposal.relation.kind.value),
                None if proposal.relation.item is None else ItemId(proposal.relation.item),
                proposal.effect,
                proposal.unlock,
                proposal.urgency_evidence,
                disposition,
                None if disposition_target is None else ItemId(disposition_target),
                disposition_reason,
                1,
                None,
                None if legacy_disposition is None else now,
            )
        )
        evidence.extend(
            ProposalEvidence(ProposalId(proposal.proposal_id), index, value)
            for index, value in enumerate(proposal.evidence)
        )
        freshness.extend(
            ProposalFreshness(ProposalId(proposal.proposal_id), index, value)
            for index, value in enumerate(proposal.freshness_assumptions)
        )
    return (
        ProposalRecords(
            tuple(sorted(proposals, key=_proposal_key)),
            tuple(sorted(evidence, key=_proposal_evidence_key)),
            tuple(sorted(freshness, key=_proposal_freshness_key)),
        ),
        source_by_id,
    )


def _proposal_key(value: StoredProposal) -> str:
    return str(value.proposal_id)


def _proposal_evidence_key(value: ProposalEvidence) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _proposal_freshness_key(value: ProposalFreshness) -> tuple[str, int]:
    return str(value.proposal_id), value.position


def _pending_artifacts(selected: Path, items: tuple[_ItemSource, ...]) -> tuple[_PendingArtifact, ...]:
    artifacts = [
        _PendingArtifact(
            source.path.relative_to(selected.parent).as_posix(),
            ArtifactKind.REQUIREMENTS,
            source.record.item,
            ".md",
            source.body,
            ItemId(source.record.item),
            None,
            ArtifactRole.REQUIREMENTS,
        )
        for source in items
        if source.body
    ]
    item_ids = {source.record.item: ItemId(source.record.item) for source in items}
    for attempt_path in sorted((selected / "attempts").glob("*/attempt.md")):
        attempt = parse_attempt(attempt_path)
        item_id = item_ids.get(attempt.item)
        if item_id is None:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Attempt {attempt.attempt} owns an unknown item.")
        attempt_id = AttemptId(attempt.attempt)
        artifacts.append(
            _PendingArtifact(
                attempt_path.relative_to(selected.parent).as_posix(),
                ArtifactKind.BRIEF,
                attempt.attempt,
                ".md",
                _body(attempt_path.read_bytes(), str(attempt_path)),
                item_id,
                attempt_id,
                None,
            )
        )
        for path in sorted(
            candidate
            for candidate in attempt_path.parent.rglob("*")
            if candidate.is_file() and candidate != attempt_path
        ):
            relative = path.relative_to(attempt_path.parent)
            if relative == Path("result.md"):
                kind, key, role = ArtifactKind.RESULT, attempt.attempt, None
            elif relative == Path("blocker.md"):
                kind, key, role = ArtifactKind.BLOCKER, attempt.attempt, None
            elif relative == Path("review.md"):
                kind, key, role = ArtifactKind.EVIDENCE, f"{attempt.attempt}-review", ArtifactRole.EVIDENCE
            else:
                name = "-".join((*relative.parts[:-1], relative.stem))
                kind, key, role = ArtifactKind.EVIDENCE, f"{attempt.attempt}-{name}", ArtifactRole.EVIDENCE
            suffix = path.suffix or ".bin"
            artifacts.append(
                _PendingArtifact(
                    path.relative_to(selected.parent).as_posix(),
                    kind,
                    key,
                    suffix,
                    path.read_bytes(),
                    item_id,
                    attempt_id,
                    role,
                )
            )
    selectors = [artifact.selector for artifact in artifacts]
    identities = [(artifact.kind, artifact.key) for artifact in artifacts]
    if len(selectors) != len(set(selectors)) or len(identities) != len(set(identities)):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Legacy artifacts map to a duplicate target.")
    return tuple(sorted(artifacts, key=_artifact_key))


def _artifact_key(artifact: _PendingArtifact) -> bytes:
    return artifact.selector.encode()


def _stored_item_key(value: StoredWorkItem) -> str:
    return str(value.item_id)


def _scope_revision_key(value: ItemScopeRevision) -> str:
    return str(value.item_id)


def _dependency_key(value: ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _item_artifact_key(value: ItemArtifactLink) -> tuple[str, str, int]:
    return str(value.item_id), value.role.value, value.position


def _attempt_key(value: StoredAttempt) -> str:
    return str(value.attempt_id)


def _artifact_references(artifacts: tuple[_PendingArtifact, ...], now: datetime) -> tuple[ArtifactReference, ...]:
    return tuple(
        ArtifactReference(
            ArtifactRefId(index),
            artifact.key,
            1,
            artifact.kind,
            artifact.selector,
            hashlib.sha256(artifact.content).hexdigest(),
            len(artifact.content),
            1,
            now,
        )
        for index, artifact in enumerate(artifacts, start=1)
    )


def _manifest_bytes(
    cutover_id: str,
    source_revision: str,
    entries: tuple[_SourceEntry, ...],
    artifacts: tuple[_PendingArtifact, ...],
) -> bytes:
    artifact_by_source = {artifact.source_selector: artifact for artifact in artifacts}
    position_by_selector: dict[str, int] = {}
    groups: dict[tuple[ItemId, ArtifactRole], list[_PendingArtifact]] = {}
    for artifact in artifacts:
        if artifact.item_id is not None and artifact.role is not None:
            groups.setdefault((artifact.item_id, artifact.role), []).append(artifact)
    for grouped in groups.values():
        for position, artifact in enumerate(sorted(grouped, key=_artifact_key)):
            position_by_selector[artifact.selector] = position
    rows: list[JsonValue] = []
    for entry in entries:
        artifact = artifact_by_source.get(entry.selector)
        rows.append(
            {
                "source_selector": entry.selector,
                "source_sha256": entry.sha256,
                "source_size": entry.size,
                "classification": entry.classification,
                "transformation": _TRANSFORMATION_VERSION if artifact is not None else "none",
                "target_kind": None if artifact is None else artifact.kind.value,
                "target_key": None if artifact is None else artifact.key,
                "target_revision": None if artifact is None else 1,
                "target_selector": None if artifact is None else artifact.selector,
                "target_sha256": None if artifact is None else hashlib.sha256(artifact.content).hexdigest(),
                "target_size": None if artifact is None else len(artifact.content),
                "owning_item": None if artifact is None or artifact.item_id is None else str(artifact.item_id),
                "owning_attempt": None if artifact is None or artifact.attempt_id is None else str(artifact.attempt_id),
                "role": None if artifact is None or artifact.role is None else artifact.role.value,
                "position": None if artifact is None else position_by_selector.get(artifact.selector),
            }
        )
    return _canonical_json(
        {
            "schema": _MANIFEST_SCHEMA,
            "cutover_id": cutover_id,
            "source_schema": "repo-work/v2",
            "source_revision": source_revision,
            "entries": rows,
        }
    )


def inactive_roots_from_manifest(manifest_bytes: bytes) -> tuple[str, ...]:
    try:
        manifest = msgspec.json.decode(manifest_bytes, type=_ManifestRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The import manifest is invalid JSON.") from error
    roots = {
        entry.source_selector.split("/", 1)[0]
        for entry in manifest.entries
        if entry.classification == "superseded-by-selected-v2"
    }
    if not roots.issubset(_INACTIVE_ROOTS):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The import manifest names an unsupported root.")
    return tuple(sorted(roots))


def _attempt_state(
    selected: Path,
    item_digests: dict[ItemId, str],
    reference_by_selector: dict[str, ArtifactReference],
    now: datetime,
) -> tuple[tuple[StoredAttempt, ...], AuthorityRecords]:
    attempts: list[StoredAttempt] = []
    counters: list[AttemptLeaseCounter] = []
    generations: list[AttemptLeaseGeneration] = []
    leases: list[StoredAttemptLease] = []
    for path in sorted((selected / "attempts").glob("*/attempt.md")):
        parsed = parse_attempt(path)
        header = parse_header(path)
        item_id = ItemId(parsed.item)
        digest = item_digests.get(item_id)
        if digest is None:
            raise LegacyImportError("LEGACY_SOURCE_INVALID", f"Attempt {parsed.attempt} owns an unknown item.")
        attempt_id = AttemptId(parsed.attempt)
        brief = reference_by_selector[f"artifacts/briefs/{parsed.attempt}/1.md"]
        result = reference_by_selector.get(f"artifacts/results/{parsed.attempt}/1.md")
        blocker = reference_by_selector.get(f"artifacts/blockers/{parsed.attempt}/1.md")
        updated = _timestamp(_required_string(header, "updated", str(path)), selector=str(path))
        attempts.append(
            StoredAttempt(
                attempt_id,
                item_id,
                OriginKind.LEGACY_IMPORT,
                parsed.state,
                parsed.branch,
                parsed.base_revision,
                parsed.provenance,
                brief.artifact_ref_id,
                None if result is None else result.artifact_ref_id,
                None if blocker is None else blocker.artifact_ref_id,
                None,
                None,
                1,
                digest,
                1,
                None,
                updated,
                now,
                now,
            )
        )
        lease = read_attempt_lease(selected, parsed.attempt)
        counters.append(AttemptLeaseCounter(attempt_id, lease.generation))
        if lease.generation > 0:
            generations.append(
                AttemptLeaseGeneration(
                    attempt_id,
                    lease.generation,
                    LeaseId(lease.lease_id),
                    TaskId(lease.task_id),
                    HostId(lease.host_id),
                )
            )
            leases.append(
                StoredAttemptLease(
                    attempt_id,
                    lease.generation,
                    lease.acquired_at,
                    lease.expires_at,
                    AttemptLeaseState(lease.status.value),
                )
            )
    coordination_record = read_coordination_lease(selected)
    coordination = (
        None
        if coordination_record is None
        else StoredCoordinationLease(
            LeaseId(coordination_record.lease_id),
            TaskId(coordination_record.task_id),
            HostId(coordination_record.host_id),
            coordination_record.generation,
            coordination_record.acquired_at,
            coordination_record.expires_at,
            CoordinationLeaseState(coordination_record.status.value),
        )
    )
    return tuple(attempts), AuthorityRecords(coordination, tuple(counters), tuple(generations), tuple(leases))


def _prepare(  # noqa: PLR0915 - exhaustive temporary cross-boundary mapping remains visible in one owner
    project_root: Path, base_work_root: Path, now: datetime
) -> _PreparedImport:
    if now.tzinfo is None:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "Import time must be timezone-aware.")
    now = now.astimezone(UTC)
    selected = _selected_source(project_root, base_work_root)
    entries = _classify_files(base_work_root.resolve(), selected)
    resource_entries = tuple(entry for entry in entries if entry.classification == "legacy-resource-state")
    if resource_entries:
        raise LegacyImportError(
            "LEGACY_RESOURCE_STATE_UNSUPPORTED",
            "The legacy ledger contains resource, item-resource, or claim state.",
        )
    migration_header = parse_header(selected / "migration-complete.md")
    if migration_header.get("kind") != "migration-complete" or migration_header.get("schema") != "repo-work/v2":
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "migration-complete.md is not the exact schema-v2 marker.")
    queue = parse_queue(selected / "queue.md")
    source_snapshot = _source_snapshot(entries, queue.revision)
    cutover_id = hashlib.sha256(source_snapshot).hexdigest()
    item_sources = _item_sources(selected)
    if any(source.record.resources for source in item_sources):
        raise LegacyImportError(
            "LEGACY_RESOURCE_STATE_UNSUPPORTED",
            "The legacy ledger contains resource, item-resource, or claim state.",
        )
    validation = validate_work_state(base_work_root, project_root)
    if not validation.valid:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", validation.render())
    proposal_records, proposal_by_id = _proposal_values(selected, now)
    source_artifacts = _pending_artifacts(selected, item_sources)
    manifest_key = f"legacy-import-{cutover_id}-manifest"
    manifest_selector = f"artifacts/evidence/{manifest_key}/1.json"
    provisional_manifest = _manifest_bytes(cutover_id, queue.revision, entries, source_artifacts)
    manifest_artifact = _PendingArtifact(
        "",
        ArtifactKind.EVIDENCE,
        manifest_key,
        ".json",
        provisional_manifest,
        None,
        None,
        None,
    )
    artifacts = tuple(sorted((*source_artifacts, manifest_artifact), key=_artifact_key))
    references = _artifact_references(artifacts, now)
    reference_by_selector = {reference.selector: reference for reference in references}
    source_by_item = {ItemId(source.record.item): source for source in item_sources}
    item_digests: dict[ItemId, str] = {}
    work_items: list[StoredWorkItem] = []
    scope_revisions: list[ItemScopeRevision] = []
    dependencies: list[ItemDependency] = []
    item_artifacts: list[ItemArtifactLink] = []
    for item_id, source in source_by_item.items():
        row = source.queue
        requirement = reference_by_selector.get(f"artifacts/requirements/{item_id}/1.md")
        proposal = (
            proposal_by_id.get(row.source.removeprefix("proposal:")) if row.source.startswith("proposal:") else None
        )
        scope = ItemScope(
            item_id,
            source.record.user_label,
            None if proposal is None else proposal.trigger,
            None if proposal is None else proposal.why_it_matters,
            None if proposal is None else proposal.effect,
            None if proposal is None else proposal.unlock,
            tuple(ScopeDependency(index, ItemId(value)) for index, value in enumerate(row.depends_on)),
            (),
            ()
            if requirement is None
            else (
                ScopeArtifact(
                    ArtifactRole.REQUIREMENTS,
                    0,
                    "requirements",
                    requirement.key,
                    requirement.revision,
                    requirement.selector,
                    requirement.content_sha256,
                ),
            ),
        )
        digest_result = item_scope_digest(scope)
        if isinstance(digest_result, DecisionFailure):
            raise LegacyImportError("LEGACY_SOURCE_INVALID", digest_result.message)
        item_digests[item_id] = digest_result
        updated_value = _optional_string(source.header, "updated")
        updated = now if updated_value is None else _timestamp(updated_value, selector=str(source.path))
        try:
            state = StoredWorkItemState(row.state.value)
            timing = None if row.timing is None else Timing(row.timing)
        except ValueError as error:
            raise LegacyImportError(
                "LEGACY_SOURCE_INVALID", f"Unsupported item vocabulary in {source.path}."
            ) from error
        work_items.append(
            StoredWorkItem(
                item_id,
                OriginKind.LEGACY_IMPORT,
                source.record.user_label,
                state,
                timing,
                row.source or None,
                None if proposal is None else proposal.trigger,
                None if proposal is None else proposal.why_it_matters,
                None if proposal is None else proposal.effect,
                None if proposal is None else proposal.unlock,
                row.outcome_evidence,
                row.next_action,
                row.notes or None,
                1,
                digest_result,
                1,
                None,
                updated,
                now,
                now,
            )
        )
        scope_revisions.append(ItemScopeRevision(item_id, 1, digest_result, 1, now))
        dependencies.extend(ItemDependency(item_id, ItemId(value), index) for index, value in enumerate(row.depends_on))
        linked = [artifact for artifact in artifacts if artifact.item_id == item_id and artifact.role is not None]
        for role in ArtifactRole:
            role_artifacts = sorted((artifact for artifact in linked if artifact.role == role), key=_artifact_key)
            item_artifacts.extend(
                ItemArtifactLink(item_id, reference_by_selector[artifact.selector].artifact_ref_id, role, position)
                for position, artifact in enumerate(role_artifacts)
            )
    missing_dependencies = {value.dependency_id for value in dependencies} - set(source_by_item)
    if missing_dependencies:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "An imported dependency has no item record.")
    attempts, authority_records = _attempt_state(selected, item_digests, reference_by_selector, now)
    focus_source = parse_current(selected / "current.md")
    focus = StoredFocus(
        None if focus_source.focus_item is None else ItemId(focus_source.focus_item),
        None if focus_source.focus_attempt is None else AttemptId(focus_source.focus_attempt),
        focus_source.next_action,
        1,
    )
    manifest_reference = reference_by_selector[manifest_selector]
    outcome: JsonValue = {
        "cutover_id": cutover_id,
        "source_revision": queue.revision,
        "source_schema": "repo-work/v2",
        "importer_version": __version__,
        "destination_revision": 1,
        "manifest_selector": manifest_selector,
        "manifest_sha256": manifest_reference.content_sha256,
        "counts": {
            "items": len(work_items),
            "attempts": len(attempts),
            "proposals": len(proposal_records.proposals),
            "artifacts": len(references),
            "resources": 0,
            "item_resources": 0,
            "claims": 0,
        },
    }
    history = StoredTransitionReceipt(
        HistoryId(1),
        1,
        ActionId(f"legacy-import:{cutover_id}"),
        TransitionHistoryActionKind.LEGACY_IMPORT,
        HistorySubjectId("ledger"),
        manifest_reference.artifact_ref_id,
        TransitionHistoryAuthorizationKind.MIGRATION,
        TaskId("legacy-importer"),
        None,
        "repo-work-legacy-import-input/v1",
        CanonicalJson(_canonical_json({"cutover_id": cutover_id}).removesuffix(b"\n")),
        "repo-work-legacy-import-outcome/v1",
        CanonicalJson(_canonical_json(outcome).removesuffix(b"\n")),
        now,
    )
    state = StoredWorkState(
        LifecycleRecords(
            ProjectRecord("charlie-pinboard", 1, 1, 1, now, now),
            tuple(sorted(work_items, key=_stored_item_key)),
            tuple(sorted(scope_revisions, key=_scope_revision_key)),
            tuple(sorted(dependencies, key=_dependency_key)),
            tuple(sorted(item_artifacts, key=_item_artifact_key)),
            tuple(sorted(attempts, key=_attempt_key)),
        ),
        proposal_records,
        PlanningRecords(),
        ArtifactRecords(references),
        authority_records,
        ResourceRecords(),
        HistoryRecords((history,)),
        focus,
    )
    counts = ImportCounts(len(work_items), len(attempts), len(proposal_records.proposals), len(references))
    receipt = ImportReceipt(
        cutover_id,
        queue.revision,
        1,
        manifest_selector,
        hashlib.sha256(provisional_manifest).hexdigest(),
        source_snapshot,
        counts,
    )
    return _PreparedImport(receipt, state, artifacts, provisional_manifest)


def dry_run_ledger(project_root: Path, base_work_root: Path, now: datetime) -> ImportReceipt:
    return _prepare_checked(project_root, base_work_root, now).receipt


def _prepare_checked(project_root: Path, base_work_root: Path, now: datetime) -> _PreparedImport:
    try:
        return _prepare(project_root.resolve(), base_work_root.resolve(), now)
    except LegacyImportError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise LegacyImportError("LEGACY_SOURCE_INVALID", str(error)) from error


def import_ledger(
    project_root: Path,
    base_work_root: Path,
    destination: Path,
    now: datetime,
) -> ImportReceipt:
    destination = destination.absolute()
    if destination.name != "state.sqlite3" or destination.exists():
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Import destination must be an absent state.sqlite3.")
    prepared = _prepare_checked(project_root, base_work_root, now)
    roots = DurableRoots(destination.parent.resolve(strict=True), ())
    repository = ArtifactRepository(roots)
    for artifact in prepared.artifacts:
        repository.publish(NewArtifact(artifact.kind, artifact.key, 1, artifact.suffix, artifact.content))
    initialize_database(roots, now.astimezone(UTC))
    store = SQLiteWorkStore(destination)
    store.initialize_state(prepared.state)
    if store.snapshot() != prepared.state:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Imported state did not reload exactly.")
    for reference in prepared.state.artifacts.references:
        repository.verify(reference)
    if not _source_still_matches(base_work_root.resolve(), prepared.receipt.source_snapshot):
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "The source changed during import.")
    view_result = rebuild_views(store, destination.parent)
    if view_result.warning is not None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", view_result.warning.message)
    return prepared.receipt


def _inactive_roots(source_snapshot: bytes) -> tuple[str, ...]:
    try:
        value = msgspec.json.decode(source_snapshot, type=_SourceSnapshotRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The source snapshot is not valid JSON.") from error
    roots: set[str] = set()
    for entry in value.entries:
        if entry.classification != "superseded-by-selected-v2":
            continue
        if not entry.selector:
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The source snapshot has an invalid selector.")
        roots.add(entry.selector.split("/", 1)[0])
    if not roots.issubset(_INACTIVE_ROOTS):
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", "The source snapshot names an unsupported inactive root."
        )
    return tuple(sorted(roots))


def _source_still_matches(base_work_root: Path, source_snapshot: bytes) -> bool:
    try:
        value = msgspec.json.decode(source_snapshot, type=_SourceSnapshotRecord)
    except msgspec.DecodeError:
        return False
    expected = {entry.selector: (entry.sha256, entry.size) for entry in value.entries}
    observed: set[str] = set()
    for path in (candidate for candidate in base_work_root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(base_work_root)
        if (relative.parts and relative.parts[0] in {"artifacts", "views"}) or relative.name.startswith(
            "state.sqlite3"
        ):
            continue
        selector = relative.as_posix()
        expected_value = expected.get(selector)
        if expected_value is None:
            return False
        data = path.read_bytes()
        if (hashlib.sha256(data).hexdigest(), len(data)) != expected_value:
            return False
        observed.add(selector)
    return observed == set(expected)


def _archival_source_still_matches(base_work_root: Path, source_snapshot: bytes) -> bool:
    try:
        value = msgspec.json.decode(source_snapshot, type=_SourceSnapshotRecord)
    except msgspec.DecodeError:
        return False
    expected = {
        entry.selector: (entry.sha256, entry.size) for entry in value.entries if entry.selector != "authority.json"
    }
    observed: set[str] = set()
    for path in (candidate for candidate in base_work_root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(base_work_root)
        if (relative.parts and relative.parts[0] in {"artifacts", "views"}) or relative.name.startswith(
            "state.sqlite3"
        ):
            continue
        selector = relative.as_posix()
        if selector == "authority.json":
            if path.read_bytes() != CUTOVER_TOMBSTONE:
                return False
            continue
        if relative.parts and relative.parts[0] == "legacy-v2":
            canonical = PurePosixPath("v2", *relative.parts[1:]).as_posix()
        elif relative.parts and relative.parts[0] == "legacy-v1":
            canonical = PurePosixPath(*relative.parts[1:]).as_posix()
        else:
            canonical = selector
        expected_value = expected.get(canonical)
        if expected_value is None or canonical in observed:
            return False
        data = path.read_bytes()
        if (hashlib.sha256(data).hexdigest(), len(data)) != expected_value:
            return False
        observed.add(canonical)
    return observed == set(expected)


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise LegacyImportError(
            "STORAGE_INVARIANT_VIOLATION", f"Directory could not be synchronized: {path}."
        ) from error


def archive_legacy(base_work_root: Path, receipt: ImportReceipt) -> None:
    base = base_work_root.resolve()
    if (base / "authority.json").read_bytes() != CUTOVER_TOMBSTONE:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The exact SQLite cutover tombstone is not active.")
    selected = base / "v2"
    archived = base / "legacy-v2"
    if selected.exists() and archived.exists():
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "Both current and archived v2 trees exist.")
    if not _archival_source_still_matches(base, receipt.source_snapshot):
        raise LegacyImportError("LEGACY_SOURCE_INVALID", "The frozen source changed before archival completed.")
    if selected.exists():
        selected.replace(archived)
        _sync_directory(base)
    elif not archived.is_dir():
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The selected v2 tree is missing.")
    inactive = _inactive_roots(receipt.source_snapshot)
    archive_v1 = base / "legacy-v1"
    if inactive:
        archive_v1.mkdir(exist_ok=True)
        _sync_directory(base)
    for selector in inactive:
        source = base / selector
        target = archive_v1 / selector
        if source.exists() and target.exists():
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Both inactive selectors exist for {selector}.")
        if source.exists():
            source.replace(target)
            _sync_directory(base)
            _sync_directory(archive_v1)
        elif not target.exists():
            raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", f"Inactive selector {selector} is missing.")


def _read_import_receipt(base_work_root: Path) -> ImportReceipt:
    state = SQLiteWorkStore(base_work_root / "state.sqlite3").snapshot()
    imports = [
        value for value in state.history.receipts if value.action_kind == TransitionHistoryActionKind.LEGACY_IMPORT
    ]
    if len(imports) != 1 or imports[0].artifact_ref_id is None:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The staged database has no unique import receipt.")
    try:
        outcome = msgspec.json.decode(bytes(imports[0].outcome_payload), type=_ImportOutcomeRecord)
    except msgspec.DecodeError as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The staged import outcome is invalid.") from error
    reference = next(
        (value for value in state.artifacts.references if value.artifact_ref_id == imports[0].artifact_ref_id),
        None,
    )
    if (
        reference is None
        or reference.kind != ArtifactKind.EVIDENCE
        or reference.selector != outcome.manifest_selector
        or reference.content_sha256 != outcome.manifest_sha256
    ):
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The staged import manifest identity is invalid.")
    ArtifactRepository(DurableRoots(base_work_root, ())).verify(reference)
    try:
        manifest = msgspec.json.decode((base_work_root / reference.selector).read_bytes(), type=_ManifestRecord)
    except (OSError, msgspec.DecodeError) as error:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The staged import manifest is invalid.") from error
    entries = tuple(
        _SourceEntry(value.source_selector, value.classification, value.source_sha256, value.source_size)
        for value in manifest.entries
    )
    source_snapshot = _source_snapshot(entries, outcome.source_revision)
    if hashlib.sha256(source_snapshot).hexdigest() != outcome.cutover_id or manifest.cutover_id != outcome.cutover_id:
        raise LegacyImportError("STORAGE_INVARIANT_VIOLATION", "The staged source identity is invalid.")
    counts = outcome.counts
    return ImportReceipt(
        outcome.cutover_id,
        outcome.source_revision,
        outcome.destination_revision,
        outcome.manifest_selector,
        outcome.manifest_sha256,
        source_snapshot,
        ImportCounts(
            counts.items,
            counts.attempts,
            counts.proposals,
            counts.artifacts,
            counts.resources,
            counts.item_resources,
            counts.claims,
        ),
    )


def cutover_ledger(project_root: Path, base_work_root: Path, now: datetime) -> ImportReceipt:
    base = base_work_root.resolve()
    database = base / "state.sqlite3"
    receipt = _read_import_receipt(base) if database.is_file() else import_ledger(project_root, base, database, now)
    marker = base / "authority.json"
    marker_bytes = marker.read_bytes() if marker.is_file() else None
    if marker_bytes != CUTOVER_TOMBSTONE:
        if marker_bytes is None or not _source_still_matches(base, receipt.source_snapshot):
            raise LegacyImportError("LEGACY_SOURCE_INVALID", "The source changed before authority replacement.")
        atomic_replace(marker, CUTOVER_TOMBSTONE)
    archive_legacy(base, receipt)
    return receipt
