"""Publish one canonical work brief and present its accepted reference.

The command function reads the selected candidate, opens the concrete artifact
repository and SQLite store, accepts the immutable artifact, and rebuilds
generated views. It returns advertised decision failures and lets filesystem,
storage, and malformed boundary data remain exact exceptions.
"""

import sys
from datetime import UTC, datetime

import msgspec

from pinboard.adapters.files import artifacts as artifact_files
from pinboard.adapters.files import file_io
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import artifact_publication, artifacts, stored_state
from pinboard.domain.errors import DecisionFailure
from pinboard.interfaces import cli_commands, work_briefs, work_views
from pinboard.interfaces.cli_output import write_json
from pinboard.interfaces.errors import CommandFailure, CommandResult, WorkBriefError, WorkBriefErrorCode


class BriefPublicationView(msgspec.Struct, frozen=True):
    artifact_ref_id: int
    kind: str
    key: str
    revision: int
    selector: str
    content_sha256: str
    size_bytes: int
    accepted_revision: int


def publish_brief(
    roots: cli_commands.ResolvedRoots,
    command: cli_commands.BriefPublishCommand,
) -> CommandResult[int]:
    try:
        candidate = command.file.read_bytes()
    except OSError as error:
        raise WorkBriefError(
            WorkBriefErrorCode.BRIEF_INVALID,
            f"Cannot read work brief candidate '{command.file}': {error}",
        ) from error
    brief = work_briefs.decode_work_brief(candidate)
    store = SQLiteWorkStore(roots.work / "state.sqlite3")
    accepted = artifact_publication.publish_accepted_artifact(
        store,
        artifact_files.ArtifactRepository(file_io.resolve_durable_roots(roots.shared_repository, roots.work)),
        artifacts.NewArtifact(
            stored_state.ArtifactKind.BRIEF,
            brief.attempt_id,
            brief.artifact_revision,
            ".json",
            work_briefs.canonical_work_brief_bytes(brief),
        ),
        datetime.now(UTC),
    )
    if isinstance(accepted, DecisionFailure):
        return CommandFailure(accepted.code, accepted.message)
    view_result = work_views.rebuild(roots, store)
    if view_result.warning is not None:
        print(view_result.warning.message, file=sys.stderr)
    view = BriefPublicationView(
        int(accepted.artifact_ref_id),
        accepted.kind.value,
        accepted.key,
        accepted.revision,
        accepted.selector,
        accepted.content_sha256,
        accepted.size_bytes,
        accepted.accepted_revision,
    )
    if command.json:
        write_json(view)
    else:
        print(
            f"OK BRIEF_PUBLISHED artifact_ref_id={view.artifact_ref_id} selector={view.selector} "
            f"accepted_revision={view.accepted_revision}"
        )
    return 0
