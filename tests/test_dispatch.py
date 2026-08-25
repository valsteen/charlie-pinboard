import contextlib
import hashlib
import io
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msgspec

from charlie_pinboard.adapters.files.artifacts import ArtifactRepository, NewArtifact, write_revision
from charlie_pinboard.adapters.files.file_io import DurableRoots, resolve_durable_roots
from charlie_pinboard.adapters.sqlite.database import initialize_database
from charlie_pinboard.adapters.sqlite.store import SQLiteWorkStore
from charlie_pinboard.application.actions import discover_actions
from charlie_pinboard.application.dispatch import (
    DispatchEnvironment,
    DispatchError,
    DispatchPermission,
    prepare_dispatch,
)
from charlie_pinboard.application.stored_state import ArtifactKind
from charlie_pinboard.domain.decisions import Action, ActionKind, Role
from charlie_pinboard.domain.identifiers import LeaseId
from charlie_pinboard.interfaces.cli import main
from charlie_pinboard.interfaces.dispatch_brief import (
    _checkpoint_section,
    _parse_header_text,
    prepare_dispatch_from_artifact,
    read_dispatch_environment,
)
from tests.support import SQLITE_NOW, complete_sqlite_state

CHECKPOINT = "Sequence 2 — Complete the shared protocol cutover"
CONTRACT_INVARIANT = "Kotlin and Rust use protocol v13 together."
CONTRACT_TABLE = """\
#### Contract table

| Invariant | Authority / owner | Required consumer or production observation | Failure classification | Exact verification | Preflight / final revalidation |
| --- | --- | --- | --- | --- | --- |
| Kotlin and Rust use protocol v13 together. | Extension protocol | Rust connector | Unsupported version is explicit. | `pnpm rust:test` | Re-run after both consumers change. |
"""
AUTHORITY_COLUMNS = "| Authority ID | Selector | Reviewed SHA-256 | In-scope families |"
COVERAGE_COLUMNS = "| Authority / invariant family | Required distinction | Required consumer / production observation | Disposition | Brief owner | Cheapest counterexample |"
REVIEW_COLUMNS = "| Authority / invariant family | Brief owner | Verdict | Cheapest counterexample result |"


def _section_bytes(text: str, heading: str) -> bytes:
    lines = text.splitlines()
    heading_line = f"### {heading}"
    start = lines.index(heading_line)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### ")), len(lines))
    return ("\n".join(lines[start:end]) + "\n").encode()


def _table_lines(section: str, header: str) -> tuple[str, ...]:
    lines = section.splitlines()
    start = lines.index(header)
    rows = [header, lines[start + 1]]
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return tuple(rows)


def _cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip()[1:-1].split("|"))


def _reviewed_brief(project: Path) -> bytes:
    architecture = "# Architecture\r\n\r\n## Protocol semantics\r\n\r\nProtocol v13 is shared.\r\n\r\n# Other\r\n\r\nNot selected.\r\n"
    plan = b'{"consumer":"rust"}\n'
    (project / "architecture.md").write_bytes(architecture.encode())
    (project / "plan.json").write_bytes(plan)
    architecture_digest = hashlib.sha256(b"## Protocol semantics\n\nProtocol v13 is shared.\n\n").hexdigest()
    plan_digest = hashlib.sha256(plan).hexdigest()
    return f"""\
---
kind: work-attempt
schema: pinboard-work-brief/v1
attempt: work-a-1
item: work-a
state: active
branch: codex/work-a
base_revision: base-revision
owner_task_id: worker
updated: "2026-08-25"
---

# Attempt

### {CHECKPOINT}

Checkpoint boundary: cross-boundary
Checkpoint outcome: independently-buildable
Architecture impact: update-required — `architecture.md` — This checkpoint changes the shared protocol architecture.

#### Reviewed authorities

{AUTHORITY_COLUMNS}
| --- | --- | --- | --- |
| architecture | `architecture.md#Protocol semantics` | `{architecture_digest}` | protocol-contract |
| plan | `plan.json` | `{plan_digest}` | consumer-proof |

{CONTRACT_TABLE}

#### Authoritative coverage

{COVERAGE_COLUMNS}
| --- | --- | --- | --- | --- | --- |
| `authority:architecture#protocol-contract` | The protocol version remains shared. | Kotlin and Rust protocol consumers | contract | `contract:{CONTRACT_INVARIANT}` | Keep Kotlin on v12 while Rust moves to v13. |
| `authority:plan#consumer-proof` | The Rust consumer is verified directly. | Rust connector | acceptance | `criterion:1` | Delete the Rust protocol test. |

Lifecycle partition: not-applicable — this protocol cutover changes no lifecycle operation.

#### Acceptance criteria

1. The production Rust connector accepts protocol v13.
""".encode()


def _review(brief: bytes) -> bytes:
    section = _section_bytes(brief.decode(), CHECKPOINT)
    authority_table = _table_lines(section.decode(), AUTHORITY_COLUMNS)
    coverage_rows = _table_lines(section.decode(), COVERAGE_COLUMNS)[2:]
    rows = "\n".join(
        f"| {cells[0]} | {cells[4]} | covered | Counterexample rejected. |"
        for cells in (_cells(row) for row in coverage_rows)
    )
    return f"""\
---
kind: work-brief-review
schema: pinboard-work-brief-review/v1
attempt: work-a-1
checkpoint: "{CHECKPOINT}"
checkpoint_sha256: "{hashlib.sha256(section).hexdigest()}"
reviewed_authority_set_sha256: "{hashlib.sha256(("\n".join(authority_table) + "\n").encode()).hexdigest()}"
reviewer_task_id: "brief-reviewer-task"
status: complete
verdict: ready
---

# Brief review

{REVIEW_COLUMNS}
| --- | --- | --- | --- |
{rows}
""".encode()


class DispatchTest(unittest.TestCase):
    def _prepare_cross_boundary(
        self,
        project: Path,
        brief: bytes,
        *,
        review: bytes | None = None,
    ) -> str:
        path = project / "candidate-brief.md"
        path.write_bytes(brief)
        environment = DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )
        candidate = _review(brief) if review is None else review
        return prepare_dispatch_from_artifact(
            path,
            "work-a-1",
            "codex/work-a",
            project,
            CHECKPOINT,
            environment,
            brief_review=candidate,
            review_id="review-id",
            review_publisher=lambda _digest, value, _review_id: (value or b"", "review"),
        )

    def _initialized(
        self, brief_bytes: bytes, project: Path | None = None
    ) -> tuple[Path, DurableRoots, SQLiteWorkStore, Callable[[], Action], DispatchEnvironment]:
        project = Path(tempfile.mkdtemp()).resolve() if project is None else project
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        published = write_revision(roots, NewArtifact(ArtifactKind.BRIEF, "work-a", 1, ".md", brief_bytes))
        state = complete_sqlite_state()
        now = datetime.now(UTC)
        assert state.authority.coordination is not None
        coordination = replace(state.authority.coordination, expires_at=now + timedelta(minutes=5))
        attempt_leases = tuple(
            replace(value, expires_at=now + timedelta(minutes=5)) for value in state.authority.attempt_leases
        )
        brief = replace(
            state.artifacts.references[0],
            key=published.key,
            revision=published.revision,
            selector=published.selector,
            content_sha256=published.content_sha256,
            size_bytes=published.size_bytes,
        )
        state = replace(
            state,
            artifacts=replace(state.artifacts, references=(brief, *state.artifacts.references[1:])),
            authority=replace(state.authority, coordination=coordination, attempt_leases=attempt_leases),
        )
        store = SQLiteWorkStore(roots.database_path)
        store.initialize_state(state)

        def action() -> Action:
            return next(
                value
                for value in discover_actions(
                    store,
                    Role.COORDINATOR,
                    lease_id=coordination.lease_id,
                    generation=coordination.generation,
                )
                if value.kind == ActionKind.DISPATCH
            )

        environment = DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )
        return project, roots, store, action, environment

    def test_current_frontmatter_scalars_and_checkpoint_ambiguity_are_explicit(self) -> None:
        header = _parse_header_text(
            """---
double: "value"
single: 'other'
null_value: null
tilde_value: ~
true_value: true
false_value: false
malformed_quote: "unterminated
---
"""
        )
        self.assertEqual("value", header["double"])
        self.assertEqual("other", header["single"])
        self.assertIsNone(header["null_value"])
        self.assertIsNone(header["tilde_value"])
        self.assertIs(header["true_value"], True)
        self.assertIs(header["false_value"], False)
        self.assertEqual('"unterminated', header["malformed_quote"])
        with self.assertRaisesRegex(ValueError, "HEADER_MISSING"):
            _parse_header_text("kind: value\n")
        with self.assertRaisesRegex(ValueError, "HEADER_UNTERMINATED"):
            _parse_header_text("---\nkind: value\n")
        with self.assertRaisesRegex(ValueError, "HEADER_FIELD_INVALID.*line 2"):
            _parse_header_text("---\nnot-a-field\n---\n")
        with self.assertRaisesRegex(ValueError, "HEADER_FIELD_INVALID.*line 2"):
            _parse_header_text("---\n: value\n---\n")
        with self.assertRaisesRegex(ValueError, "HEADER_FIELD_DUPLICATE.*owner_task_id.*line 3"):
            _parse_header_text("---\nowner_task_id: first\nowner_task_id: second\n---\n")

        path = Path(tempfile.mkdtemp()) / "brief.md"
        path.write_text("# Attempt\n\n## Same\n\nOne.\n\n## Same\n\nTwo.\n", encoding="utf-8")
        with self.assertRaises(DispatchError) as ambiguous:
            _checkpoint_section(path, "Same")
        self.assertEqual("DISPATCH_CHECKPOINT_AMBIGUOUS", ambiguous.exception.code)

    def test_sqlite_dispatch_reads_accepted_brief_and_rejects_stale_authority(self) -> None:
        brief = """---
kind: work-attempt
schema: pinboard-work-brief/v1
attempt: work-a-1
item: work-a
state: active
branch: codex/work-a
base_revision: base-revision
owner_task_id: worker
updated: "2026-08-25"
---

# Attempt

## Local implementation

Checkpoint boundary: local
Checkpoint outcome: independently-buildable
Architecture impact: none — This checkpoint changes no ownership or dependency direction.
""".encode()
        project, roots, store, action, environment = self._initialized(brief)
        prompt = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action(),
            "Local implementation",
            environment,
        )
        self.assertIn("Canonical brief:", prompt)

        current = action()
        for changed, code in (
            (replace(current, expected_revision="stale"), "STALE_ACTION"),
            (replace(current, label="changed"), "DISPATCH_ACTION_INVALID"),
            (replace(current, kind=ActionKind.INSPECT), "DISPATCH_ACTION_UNAVAILABLE"),
            (replace(current, lease_id=LeaseId("wrong")), "COORDINATION_LEASE_REQUIRED"),
        ):
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                prepare_dispatch(
                    store,
                    ArtifactRepository(roots),
                    prepare_dispatch_from_artifact,
                    project,
                    changed,
                    "Local implementation",
                    environment,
                )
            self.assertEqual(code, rejected.exception.code)

        mutating = msgspec.structs.replace(
            environment,
            permissions=(DispatchPermission.REPOSITORY_READ, DispatchPermission.REPOSITORY_WRITE),
        )
        mutating_prompt = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action(),
            "Local implementation",
            mutating,
        )
        self.assertIn("Canonical brief:", mutating_prompt)

    def test_current_brief_parser_rejects_neighboring_invalid_launches(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief_path = project / "brief.md"
        local = """---
kind: work-attempt
schema: pinboard-work-brief/v1
attempt: work-a-1
item: work-a
state: active
branch: codex/work-a
base_revision: base-revision
owner_task_id: worker
updated: "2026-08-25"
---

# Attempt

## Local implementation

Checkpoint boundary: local
Checkpoint outcome: independently-buildable
Architecture impact: none — This checkpoint changes no ownership or dependency direction.
"""
        brief_path.write_text(local, encoding="utf-8")
        environment = DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )

        prompt = prepare_dispatch_from_artifact(
            brief_path,
            "work-a-1",
            "codex/work-a",
            project,
            "Local implementation",
            environment,
        )
        self.assertIn("Attempt: work-a-1", prompt)

        for declaration in (
            "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
            "Architecture impact: read-only — `ARCHITECTURE.md` — This checkpoint conforms to the current architecture.",
            "Architecture impact: update-required — `ARCHITECTURE.md` — This candidate updates the architecture authority.",
        ):
            brief_path.write_text(
                local.replace(
                    "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                    declaration,
                ),
                encoding="utf-8",
            )
            with self.subTest(declaration=declaration):
                self.assertIn(
                    "Attempt: work-a-1",
                    prepare_dispatch_from_artifact(
                        brief_path,
                        "work-a-1",
                        "codex/work-a",
                        project,
                        "Local implementation",
                        environment,
                    ),
                )

        invalid_architecture_impacts = (
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.\n", ""
            ),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: none — First reason.\nArchitecture impact: none — Second reason.",
            ),
            local.replace("Architecture impact: none", "Architecture impact: invented"),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: none — `ARCHITECTURE.md` — Reason.",
            ),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: read-only — Reason without a selector.",
            ),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: read-only — `../ARCHITECTURE.md` — Reason.",
            ),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: update-required — `/tmp/ARCHITECTURE.md` — Reason.",
            ),
            local.replace(
                "Architecture impact: none — This checkpoint changes no ownership or dependency direction.",
                "Architecture impact: update-required — `ARCHITECTURE.md` —",
            ),
        )
        for text in invalid_architecture_impacts:
            brief_path.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                with self.assertRaises(DispatchError) as rejected:
                    prepare_dispatch_from_artifact(
                        brief_path,
                        "work-a-1",
                        "codex/work-a",
                        project,
                        "Local implementation",
                        environment,
                    )
                self.assertEqual("DISPATCH_ARCHITECTURE_IMPACT_INVALID", rejected.exception.code)

        cases = (
            (
                environment,
                local.replace("kind: work-attempt\n", ""),
                "Local implementation",
                None,
                None,
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                environment,
                local.replace("kind: work-attempt", "kind: other"),
                "Local implementation",
                None,
                None,
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                environment,
                local.replace("schema: pinboard-work-brief/v1", "schema: " + "repo" + "-work/v2"),
                "Local implementation",
                None,
                None,
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                msgspec.structs.replace(environment, branch="codex/other"),
                local,
                "Local implementation",
                None,
                None,
                "DISPATCH_BRANCH_MISMATCH",
            ),
            (
                msgspec.structs.replace(environment, checkout=str(project / "missing")),
                local,
                "Local implementation",
                None,
                None,
                "DISPATCH_CHECKOUT_MISSING",
            ),
            (environment, local, "Missing", None, None, "DISPATCH_CHECKPOINT_MISSING"),
            (
                environment,
                local.replace("Checkpoint boundary: local", "Checkpoint boundary: invented"),
                "Local implementation",
                None,
                None,
                "DISPATCH_BOUNDARY_INVALID",
            ),
            (
                environment,
                local.replace("Checkpoint boundary: local\n", ""),
                "Local implementation",
                None,
                None,
                "DISPATCH_BOUNDARY_MISSING",
            ),
            (
                environment,
                local,
                "Local implementation",
                b"review",
                "review-id",
                "DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID",
            ),
        )
        for candidate_environment, text, checkpoint, review, review_id, code in cases:
            brief_path.write_text(text, encoding="utf-8")
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                prepare_dispatch_from_artifact(
                    brief_path,
                    "work-a-1",
                    "codex/work-a",
                    project,
                    checkpoint,
                    candidate_environment,
                    brief_review=review,
                    review_id=review_id,
                )
            self.assertEqual(code, rejected.exception.code)

        brief_path.write_text(local, encoding="utf-8")
        with self.assertRaises(DispatchError) as noncanonical:
            prepare_dispatch_from_artifact(
                brief_path,
                "work-a-1",
                "codex/work-a",
                project,
                "Local implementation",
                environment,
                supplied_prompt=b"changed",
            )
        self.assertEqual("DISPATCH_PROMPT_NOT_CANONICAL", noncanonical.exception.code)

    def test_cross_boundary_parser_rejects_incomplete_contract_and_review_neighbors(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        brief_path = project / "brief.md"
        environment = DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )

        variants = (
            (
                brief.replace(b"Checkpoint outcome: independently-buildable", b"Checkpoint outcome: partial"),
                "DISPATCH_CHECKPOINT_NOT_BUILDABLE",
            ),
            (brief.replace(CONTRACT_TABLE.encode(), b""), "DISPATCH_CONTRACT_MISSING"),
            (
                brief.replace(COVERAGE_COLUMNS.encode(), b"| Removed coverage header |"),
                "DISPATCH_AUTHORITY_COVERAGE_MISSING",
            ),
            (
                brief.replace(b"Lifecycle partition: not-applicable", b"Lifecycle partition: invented"),
                "DISPATCH_LIFECYCLE_PARTITION_INVALID",
            ),
        )
        for value, code in variants:
            brief_path.write_bytes(value)
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                prepare_dispatch_from_artifact(
                    brief_path,
                    "work-a-1",
                    "codex/work-a",
                    project,
                    CHECKPOINT,
                    environment,
                    review_publisher=lambda _digest, _candidate, _review_id, value=value: (_review(value), "review"),
                )
            self.assertEqual(code, rejected.exception.code)

    def test_invalid_architecture_impact_rejects_before_review_publication(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project).replace(
            b"Architecture impact: update-required \xe2\x80\x94 `architecture.md` "
            b"\xe2\x80\x94 This checkpoint changes the shared protocol architecture.\n",
            b"",
        )
        brief_path = project / "brief.md"
        brief_path.write_bytes(brief)
        environment = DispatchEnvironment(
            "pinboard-dispatch/v1",
            str(project),
            "codex/work-a",
            "base-revision",
            (DispatchPermission.REPOSITORY_READ,),
        )
        publication_attempts: list[str] = []

        with self.assertRaises(DispatchError) as rejected:
            prepare_dispatch_from_artifact(
                brief_path,
                "work-a-1",
                "codex/work-a",
                project,
                CHECKPOINT,
                environment,
                brief_review=_review(brief),
                review_id="review-id",
                review_publisher=lambda _digest, value, _review_id: (
                    publication_attempts.append("published") or value or b"",
                    "review",
                ),
            )
        self.assertEqual("DISPATCH_ARCHITECTURE_IMPACT_INVALID", rejected.exception.code)
        self.assertEqual([], publication_attempts)

    def test_cross_boundary_contract_and_coverage_counterexamples_are_rejected(self) -> None:
        cases: tuple[tuple[Callable[[bytes], bytes], str], ...] = (
            (
                lambda value: value.replace(b"Rust connector | Unsupported", "— | Unsupported".encode(), 1),
                "DISPATCH_CONTRACT_INCOMPLETE",
            ),
            (
                lambda value: value.replace(
                    b"| --- | --- | --- | --- | --- | --- |",
                    b"| not | a | markdown | separator | row | here |",
                    1,
                ),
                "DISPATCH_CONTRACT_INVALID",
            ),
            (
                lambda value: value.replace(
                    b"| `authority:plan#consumer-proof` | The Rust consumer is verified directly. | Rust connector | acceptance | `criterion:1` | Delete the Rust protocol test. |\n",
                    b"",
                ),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                lambda value: value.replace(
                    f"`contract:{CONTRACT_INVARIANT}`".encode(),
                    b"`contract:Missing invariant`",
                ),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                lambda value: value.replace(b"authority:plan#consumer-proof", b"authority:plan#unknown-family"),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
        )
        for mutate, code in cases:
            project = Path(tempfile.mkdtemp()).resolve()
            brief = mutate(_reviewed_brief(project))
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                self._prepare_cross_boundary(project, brief)
            self.assertEqual(code, rejected.exception.code)

    def test_reviewed_authority_digest_tracks_only_the_selected_source(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        self.assertIn(CHECKPOINT, self._prepare_cross_boundary(project, brief))

        architecture = project / "architecture.md"
        architecture.write_bytes(architecture.read_bytes().replace(b"Not selected.", b"Changed but not selected."))
        self.assertIn(CHECKPOINT, self._prepare_cross_boundary(project, brief))
        architecture.write_bytes(
            architecture.read_bytes().replace(b"Protocol v13 is shared.", b"Protocol v14 is shared.")
        )
        with self.assertRaises(DispatchError) as stale:
            self._prepare_cross_boundary(project, brief)
        self.assertEqual("DISPATCH_AUTHORITY_STALE", stale.exception.code)

    def test_heading_selectors_support_each_markdown_heading_level(self) -> None:
        old_digest = hashlib.sha256(b"## Protocol semantics\n\nProtocol v13 is shared.\n\n").hexdigest()
        for level in range(1, 7):
            project = Path(tempfile.mkdtemp()).resolve()
            brief = _reviewed_brief(project)
            heading = f"{'#' * level} Protocol # semantics"
            (project / "architecture.md").write_text(
                f"{heading}\r\n\r\nProtocol v13 is shared.\r\n\r\n# Other\r\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(f"{heading}\n\nProtocol v13 is shared.\n\n".encode()).hexdigest()
            brief = brief.replace(b"architecture.md#Protocol semantics", b"architecture.md#Protocol # semantics")
            brief = brief.replace(old_digest.encode(), digest.encode())
            with self.subTest(level=level):
                self.assertIn(CHECKPOINT, self._prepare_cross_boundary(project, brief))

    def test_review_metadata_and_row_failure_matrix_is_complete(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        review = _review(brief)
        section = _section_bytes(brief.decode(), CHECKPOINT)
        checkpoint_digest = hashlib.sha256(section).hexdigest().encode()
        authority_table = _table_lines(section.decode(), AUTHORITY_COLUMNS)
        authority_digest = hashlib.sha256(("\n".join(authority_table) + "\n").encode()).hexdigest().encode()
        cases = (
            (review.replace(checkpoint_digest, b"f" * 64, 1), "DISPATCH_BRIEF_REVIEW_STALE"),
            (review.replace(authority_digest, b"e" * 64, 1), "DISPATCH_BRIEF_REVIEW_STALE"),
            (review.replace(b"status: complete", b"status: incomplete"), "DISPATCH_BRIEF_REVIEW_NOT_READY"),
            (review.replace(b"verdict: ready", b"verdict: rejected"), "DISPATCH_BRIEF_REVIEW_NOT_READY"),
            (
                review.replace(b'reviewer_task_id: "brief-reviewer-task"', b'reviewer_task_id: "worker"'),
                "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT",
            ),
            (
                review.replace(b"| `authority:plan#consumer-proof`", b"| `authority:plan#other`"),
                "DISPATCH_BRIEF_REVIEW_INCOMPLETE",
            ),
            (review.replace(b"| covered |", b"| missing |", 1), "DISPATCH_BRIEF_REVIEW_INCOMPLETE"),
        )
        for candidate, code in cases:
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                self._prepare_cross_boundary(project, brief, review=candidate)
            self.assertEqual(code, rejected.exception.code)

    def test_required_lifecycle_partition_validates_shape_before_review_truth(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        lifecycle = b"""Lifecycle partition: required

#### Lifecycle partition

| Operation | Allowed source state | Required authority | Required observation / evidence | State and fencing effects | Nearest illegal sibling / stable rejection |
| --- | --- | --- | --- | --- | --- |
| direct-preserve | active | human authorization | exact observation | revoke and fence | quarantined / stable rejection |
"""
        brief = brief.replace(
            b"Lifecycle partition: not-applicable \xe2\x80\x94 this protocol cutover changes no lifecycle operation.",
            lifecycle,
        )
        self.assertIn(CHECKPOINT, self._prepare_cross_boundary(project, brief))

        invalid = brief.replace(
            b"| direct-preserve | active | human authorization | exact observation | revoke and fence | quarantined / stable rejection |",
            "| direct-preserve | active | — | exact observation | revoke and fence | quarantined / stable rejection |".encode(),
        )
        with self.assertRaises(DispatchError) as rejected:
            self._prepare_cross_boundary(project, invalid)
        self.assertEqual("DISPATCH_LIFECYCLE_PARTITION_INVALID", rejected.exception.code)

    def test_reviewed_authority_selector_and_identity_failure_matrix(self) -> None:
        cases: tuple[tuple[Callable[[bytes], bytes], str], ...] = (
            (
                lambda value: value.replace(b"architecture.md#Protocol semantics", b"/absolute.md"),
                "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            ),
            (
                lambda value: value.replace(b"architecture.md#Protocol semantics", b"../architecture.md"),
                "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            ),
            (
                lambda value: value.replace(b"architecture.md#Protocol semantics", b"architecture.md#"),
                "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            ),
            (
                lambda value: value.replace(b"| plan | `plan.json`", b"| architecture | `plan.json`"),
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
            ),
            (
                lambda value: value.replace(
                    b"| architecture | `architecture.md#Protocol semantics` | `",
                    b"| architecture | `architecture.md#Protocol semantics` | `x",
                    1,
                ),
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
            ),
            (
                lambda value: value.replace(b"| protocol-contract |", b"| protocol-contract,protocol-contract |"),
                "DISPATCH_REVIEWED_AUTHORITIES_INVALID",
            ),
            (
                lambda value: value.replace(b"architecture.md#Protocol semantics", b"missing.md"),
                "DISPATCH_AUTHORITY_UNREADABLE",
            ),
            (
                lambda value: value.replace(b"architecture.md#Protocol semantics", b"architecture.md#Missing"),
                "DISPATCH_AUTHORITY_SELECTOR_INVALID",
            ),
        )
        for mutate, code in cases:
            project = Path(tempfile.mkdtemp()).resolve()
            brief = mutate(_reviewed_brief(project))
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                self._prepare_cross_boundary(project, brief)
            self.assertEqual(code, rejected.exception.code)

        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        (project / "architecture.md").write_bytes(b"\xff\xfe")
        with self.assertRaises(DispatchError) as non_utf8:
            self._prepare_cross_boundary(project, brief)
        self.assertEqual("DISPATCH_AUTHORITY_SELECTOR_INVALID", non_utf8.exception.code)

    def test_review_frontmatter_and_coverage_owner_failure_matrix(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        review = _review(brief)
        review_cases = (
            (b"not frontmatter\n", "DISPATCH_BRIEF_REVIEW_INVALID"),
            (review.replace(b"kind: work-brief-review", b"kind: other"), "DISPATCH_BRIEF_REVIEW_INVALID"),
            (
                review.replace(b"schema: pinboard-work-brief-review/v1", ("schema: " + "repo" + "-work/v2").encode()),
                "DISPATCH_BRIEF_REVIEW_INVALID",
            ),
            (review.replace(b"attempt: work-a-1", b"attempt: other"), "DISPATCH_BRIEF_REVIEW_INVALID"),
            (
                review.replace(
                    b'reviewer_task_id: "brief-reviewer-task"', b'reviewer_task_id: " brief-reviewer-task "'
                ),
                "DISPATCH_BRIEF_REVIEW_INVALID",
            ),
            (
                review.replace(b'reviewer_task_id: "brief-reviewer-task"', b"reviewer_task_id: null"),
                "DISPATCH_BRIEF_REVIEW_INVALID",
            ),
            (review.replace(b"Counterexample rejected.", "—".encode(), 1), "DISPATCH_BRIEF_REVIEW_INCOMPLETE"),
        )
        for candidate, code in review_cases:
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                self._prepare_cross_boundary(project, brief, review=candidate)
            self.assertEqual(code, rejected.exception.code)

        brief_cases = (
            (
                brief.replace(b"kind: work-attempt\n", b""),
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                brief.replace(b"kind: work-attempt", b"kind: other"),
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                brief.replace(b"schema: pinboard-work-brief/v1", ("schema: " + "repo" + "-work/v2").encode()),
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                brief.replace(b"#### Acceptance criteria", b"#### Missing criteria"),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                brief.replace(b"1. The production Rust connector accepts protocol v13.", b""),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                brief.replace(b"`criterion:1`", b"`criterion:2`"),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                brief.replace(b"| acceptance | `criterion:1` |", b"| deferred | `deferral:missing` |"),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                brief.replace(b"| acceptance | `criterion:1` |", b"| invented | `criterion:1` |"),
                "DISPATCH_AUTHORITY_COVERAGE_INVALID",
            ),
            (
                brief.replace(b"owner_task_id: worker", b"owner_task_id: worker\nowner_task_id: brief-reviewer-task"),
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                brief.replace(b"owner_task_id: worker", b": worker"),
                "DISPATCH_BRIEF_INVALID",
            ),
            (
                brief.replace(b"owner_task_id: worker", b"owner_task_id: worker\nmalformed-header-field"),
                "DISPATCH_BRIEF_INVALID",
            ),
        )
        for candidate, code in brief_cases:
            with self.subTest(code=code), self.assertRaises(DispatchError) as rejected:
                self._prepare_cross_boundary(project, candidate)
            self.assertEqual(code, rejected.exception.code)

    def test_dispatch_environment_rejects_predecessor_schema(self) -> None:
        path = Path(tempfile.mkdtemp()) / "environment.json"
        path.write_text(
            msgspec.json.encode(
                {
                    "schema": "repo" + "-work-dispatch/v1",
                    "checkout": "/project",
                    "branch": "codex/work-a",
                    "starting_revision": "base-revision",
                    "permissions": ["repository-read"],
                }
            ).decode(),
            encoding="utf-8",
        )
        with self.assertRaises(DispatchError) as rejected:
            read_dispatch_environment(path)
        self.assertEqual("DISPATCH_ENVIRONMENT_INVALID", rejected.exception.code)

    def test_sqlite_cross_boundary_review_is_immutable_and_collision_preserving(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        candidate = _review(brief)
        project, roots, store, action, environment = self._initialized(brief, project)

        with self.assertRaises(DispatchError) as missing:
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                action(),
                CHECKPOINT,
                environment,
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_MISSING", missing.exception.code)

        prompt = prepare_dispatch(
            store,
            ArtifactRepository(roots),
            prepare_dispatch_from_artifact,
            project,
            action(),
            CHECKPOINT,
            environment,
            brief_review=candidate,
            review_id="sqlite-review",
        )
        self.assertIn(CHECKPOINT, prompt)
        current = action()
        self.assertEqual(
            prompt,
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                current,
                CHECKPOINT,
                environment,
            ),
        )
        with self.assertRaises(DispatchError) as collision:
            prepare_dispatch(
                store,
                ArtifactRepository(roots),
                prepare_dispatch_from_artifact,
                project,
                current,
                CHECKPOINT,
                environment,
                brief_review=candidate + b"\nAdditional reviewer note.\n",
                review_id="later-review",
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", collision.exception.code)
        self.assertTrue(any("rejected-later-review" in value.key for value in store.snapshot().artifacts.references))

    def test_cli_dispatch_publishes_review_and_verifies_the_rendered_prompt(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        brief = _reviewed_brief(project)
        candidate = _review(brief)
        project, roots, _store, action, environment = self._initialized(brief, project)
        environment_path = project / "environment.json"
        environment_path.write_bytes(msgspec.json.encode(environment))
        review_path = project / "review.md"
        review_path.write_bytes(candidate)

        def arguments(current: Action) -> list[str]:
            values = [
                "--project-root",
                str(project),
                "--work-root",
                str(roots.work_root),
                "dispatch",
                "--action-id",
                str(current.action_id),
                "--expected-revision",
                current.expected_revision,
                "--generation",
                str(current.coordinator_generation),
                "--checkpoint",
                CHECKPOINT,
                "--environment",
                str(environment_path),
            ]
            if current.lease_id is not None:
                values.extend(("--lease-id", str(current.lease_id)))
            return values

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main((*arguments(action()), "--brief-review", str(review_path), "--review-id", "cli-review"))
        self.assertEqual(0, result, stderr.getvalue())
        prompt = stdout.getvalue()
        self.assertIn("sole semantic execution contract", prompt)

        prompt_path = project / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            verified = main((*arguments(action()), "--prompt", str(prompt_path)))
        self.assertEqual(0, verified, stderr.getvalue())
        self.assertIn("OK DISPATCH_READY", stdout.getvalue())

        for option, code in (
            ("--prompt", "DISPATCH_PROMPT_UNREADABLE"),
            ("--brief-review", "DISPATCH_BRIEF_REVIEW_INVALID"),
        ):
            stderr = io.StringIO()
            with self.subTest(option=option), contextlib.redirect_stderr(stderr):
                rejected = main((*arguments(action()), option, str(project / "missing.md")))
            self.assertEqual(14, rejected)
            self.assertIn(code, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
