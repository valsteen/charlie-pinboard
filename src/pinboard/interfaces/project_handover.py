"""Read-only composition for the complete portable project handover."""

import base64
from pathlib import PurePosixPath

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import handover
from pinboard.interfaces import cli_commands
from pinboard.interfaces.cli_output import write_json

_MEDIA_TYPES = {
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def _content(reference_id: int, value: bytes) -> handover.HandoverArtifactContent:
    try:
        content = value.decode("utf-8")
    except UnicodeDecodeError:
        return handover.HandoverArtifactContent(
            reference_id,
            handover.ContentEncoding.BASE64,
            base64.b64encode(value).decode("ascii"),
        )
    return handover.HandoverArtifactContent(reference_id, handover.ContentEncoding.UTF8, content)


def export(roots: cli_commands.ResolvedRoots, _command: cli_commands.HandoverCommand) -> int:
    state = SQLiteWorkStore(roots.work / "state.sqlite3").snapshot()
    artifacts = ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work))
    references: list[handover.HandoverArtifactReference] = []
    contents: list[handover.HandoverArtifactContent] = []
    for reference in state.artifact_references:
        media_type = _MEDIA_TYPES.get(PurePosixPath(reference.selector).suffix.lower(), "application/octet-stream")
        references.append(handover.artifact_reference(reference, media_type=media_type))
        contents.append(_content(int(reference.artifact_ref_id), artifacts.read(reference)))
    write_json(handover.project_handover(state, tuple(references), tuple(contents)))
    return 0
