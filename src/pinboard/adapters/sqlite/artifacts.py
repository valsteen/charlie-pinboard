"""Read and accept artifact records on a supplied connection.

Artifact acceptance verifies the supplied filesystem reference; no other
operation reads files. This module never commits, rolls back, closes the
connection, calls callbacks, or obtains time. Expected stale CAS writes return a
``DecisionFailure``; SQLite and persisted-invariant failures remain exceptional.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from pinboard.adapters.files.artifacts import verify_reference
from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import stored_state
from pinboard.application.artifacts import (
    ArtifactRef,
    ArtifactRelationship,
    EvidenceArtifactRef,
    ResultArtifactRef,
)
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId


def read_artifacts(connection: sqlite3.Connection) -> tuple[stored_state.ArtifactReference, ...]:
    return tuple(
        decode_row(row, stored_state.ArtifactReference)
        for row in connection.execute(
            """
            SELECT artifact_ref_id, artifact_key AS key, artifact_revision AS revision, kind,
                   relative_path AS selector, content_sha256, size_bytes, accepted_revision, created_at
            FROM artifact_refs
            ORDER BY artifact_ref_id
            """
        ).fetchall()
    )


def accept_checkpoint_artifact(
    connection: sqlite3.Connection,
    state: stored_state.StoredWorkState,
    published: ResultArtifactRef | EvidenceArtifactRef,
    expected_id: ArtifactRefId,
    revision: int,
    now: datetime,
) -> ArtifactRefId:
    existing = next(
        (
            value
            for value in state.artifact_references
            if (value.kind, value.key, value.revision) == (published.kind, published.key, published.revision)
        ),
        None,
    )
    if existing is not None:
        if existing.artifact_ref_id != expected_id or (
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        ) != (published.selector, published.content_sha256, published.size_bytes):
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "An accepted checkpoint artifact identity names different bytes.",
            )
        return existing.artifact_ref_id
    connection.execute(
        """
        INSERT INTO artifact_refs (
            artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
            content_sha256, size_bytes, accepted_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            expected_id,
            published.key,
            published.revision,
            published.kind.value,
            published.selector,
            published.content_sha256,
            published.size_bytes,
            revision,
            now.isoformat(),
        ),
    )
    return expected_id


def accept_artifact_reference(
    connection: sqlite3.Connection,
    before: stored_state.StoredWorkState,
    work_root: Path,
    published: ArtifactRef,
    accepted_at: datetime,
    *,
    relationship: ArtifactRelationship | None = None,
) -> DecisionResult[stored_state.ArtifactReference]:
    """Accept one verified reference; the caller owns transaction and readback."""

    verify_reference(work_root, published)
    existing = next(
        (
            value
            for value in before.artifact_references
            if (value.kind, value.key, value.revision) == (published.kind, published.key, published.revision)
        ),
        None,
    )
    if existing is not None:
        if (
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        ) != (published.selector, published.content_sha256, published.size_bytes):
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "An accepted artifact identity already names different bytes.",
            )
        reference = existing
    else:
        reference = stored_state.ArtifactReference(
            ArtifactRefId(1 + max((int(value.artifact_ref_id) for value in before.artifact_references), default=0)),
            published.key,
            published.revision,
            published.kind,
            published.selector,
            published.content_sha256,
            published.size_bytes,
            before.lifecycle.project.revision + 1,
            accepted_at,
        )
    relationship_exists = relationship is not None and any(
        value.item_id == relationship.item_id
        and value.artifact_ref_id == reference.artifact_ref_id
        and value.role == relationship.role
        for value in before.lifecycle.item_artifacts
    )
    if existing is not None and (relationship is None or relationship_exists):
        return existing
    revision = before.lifecycle.project.revision + 1
    if existing is None:
        connection.execute(
            """
            INSERT INTO artifact_refs (
                artifact_ref_id, artifact_key, artifact_revision, kind, relative_path,
                content_sha256, size_bytes, accepted_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference.artifact_ref_id,
                reference.key,
                reference.revision,
                reference.kind.value,
                reference.selector,
                reference.content_sha256,
                reference.size_bytes,
                reference.accepted_revision,
                reference.created_at.isoformat(),
            ),
        )
    if relationship is not None:
        item = next(
            (value for value in before.lifecycle.work_items if value.item_id == relationship.item_id),
            None,
        )
        if item is None or published.kind.value != relationship.role.value:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                "Artifact relationship does not match a current item and compatible role.",
            )
        position = sum(
            1
            for value in before.lifecycle.item_artifacts
            if value.item_id == relationship.item_id and value.role == relationship.role
        )
        if (
            failure := require_one_changed_row(
                connection.execute(
                    """
                    UPDATE work_items
                    SET subject_revision = ?, updated_at = ?
                    WHERE item_id = ? AND subject_revision = ?
                    """,
                    (revision, accepted_at.isoformat(), relationship.item_id, item.subject_revision),
                ),
                "The artifact relationship item changed before persistence.",
            )
        ) is not None:
            return failure
        connection.execute(
            """
            INSERT INTO item_artifacts (item_id, artifact_ref_id, role, position)
            VALUES (?, ?, ?, ?)
            """,
            (relationship.item_id, reference.artifact_ref_id, relationship.role.value, position),
        )
    if (
        failure := require_one_changed_row(
            connection.execute(
                """
                UPDATE project_meta
                SET revision = ?, updated_at = ?
                WHERE singleton = 1 AND revision = ?
                """,
                (revision, accepted_at.isoformat(), before.lifecycle.project.revision),
            ),
            "The project revision changed before artifact acceptance.",
        )
    ) is not None:
        return failure
    return reference
