from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.models import AffectedViews, ViewRefreshResult, ViewWarning
from pinboard.adapters.files.views import rebuild as rebuild_file_views
from pinboard.adapters.files.views import refresh as refresh_file_views
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces import cli_commands
from pinboard.interfaces.errors import WorkBriefError
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
    try:
        briefs = attempt_brief_views(roots, store)
    except WorkBriefError as error:
        return ViewRefreshResult(
            store.snapshot().lifecycle.project.revision,
            ViewWarning(
                f"The SQLite transition succeeded, but generated views need repair: {error} "
                "Run 'pinboard views rebuild'.",
                "Run 'pinboard views rebuild'.",
            ),
        )
    return refresh_file_views(store, roots.work, affected, briefs)


def rebuild(roots: cli_commands.ResolvedRoots, store: SQLiteWorkStore) -> ViewRefreshResult:
    try:
        briefs = attempt_brief_views(roots, store)
    except WorkBriefError as error:
        return ViewRefreshResult(
            store.snapshot().lifecycle.project.revision,
            ViewWarning(
                f"Generated views could not be rebuilt: {error} "
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
                "Resolve the accepted work-brief problem and run 'pinboard views rebuild' again.",
            ),
        )
    return rebuild_file_views(store, roots.work, briefs)
