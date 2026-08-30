from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult
from pinboard.adapters.files.views import rebuild as rebuild_file_views
from pinboard.adapters.files.views import refresh as refresh_file_views
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces import cli_commands
from pinboard.interfaces.work_briefs import build_attempt_brief_views


def attempt_brief_views(roots: cli_commands.ResolvedRoots, store: SQLiteWorkStore) -> dict[AttemptId, bytes]:
    return build_attempt_brief_views(
        store.snapshot(),
        ArtifactRepository(resolve_durable_roots(roots.shared_repository, roots.work)),
    )


def refresh(
    roots: cli_commands.ResolvedRoots,
    store: SQLiteWorkStore,
    affected: AffectedViews,
) -> ViewRefreshResult:
    return refresh_file_views(store, roots.work, affected, attempt_brief_views(roots, store))


def rebuild(roots: cli_commands.ResolvedRoots, store: SQLiteWorkStore) -> ViewRefreshResult:
    return rebuild_file_views(store, roots.work, attempt_brief_views(roots, store))
