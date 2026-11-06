"""Publish same-depth daily revision records and snapshots immutably."""

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path

from chronicle.daily_revision_archive import read_month_revision
from chronicle.daily_revision_archive_migration import copy_v2_chain_to_month
from chronicle.daily_revision_archive_storage import (
    RevisionFiles,
    RevisionIdentity,
    legacy_paths,
    month_paths,
    parent_id,
    publish_immutable,
    record_bytes,
    revision_identity,
)
from chronicle.errors import ChronicleError
from chronicle.daily_revisions import DailyRevision


@dataclass(frozen=True, slots=True)
class DailyRevisionInput:
    """Typed input for one changed latest daily view."""

    daily_path: Path
    day: date
    markdown_body: str
    previous_revision_id: str | None


def _prepare_parent(
    daily_path: Path,
    day: date,
    revision_id: str | None,
) -> Path | None:
    if revision_id is None:
        return None
    month_record = month_paths(daily_path, day, revision_id)[0]
    if month_record.is_symlink():
        raise ChronicleError("daily_conflict", "previous daily record is unsafe")
    if month_record.exists():
        _ = read_month_revision(daily_path, day, revision_id)
        return month_record
    legacy_record = legacy_paths(daily_path, day, revision_id)[0]
    if legacy_record.is_symlink():
        raise ChronicleError("daily_conflict", "previous v2 daily record is unsafe")
    if not legacy_record.exists():
        raise ChronicleError("daily_conflict", "previous daily revision record is missing")
    copy_v2_chain_to_month(daily_path, day, revision_id)
    _ = read_month_revision(daily_path, day, revision_id)
    return month_record


def archive_legacy_daily(
    daily_path: Path,
    day: date,
    previous_markdown: bytes,
) -> DailyRevision:
    """Preserve an unversioned daily note in the same-depth month archive."""
    revision_id = revision_identity(
        RevisionIdentity(day, None, "legacy", previous_markdown, 2)
    )
    record_path, snapshot_path = month_paths(daily_path, day, revision_id)
    publish_immutable(snapshot_path, previous_markdown, "legacy snapshot")
    files = RevisionFiles(
        day,
        revision_id,
        None,
        record_path,
        snapshot_path,
        None,
        "legacy",
        previous_markdown,
    )
    publish_immutable(record_path, record_bytes(files), "legacy revision record")
    return DailyRevision(revision_id, None, record_path, snapshot_path, "legacy")


def create_daily_revision(request: DailyRevisionInput) -> tuple[DailyRevision, bytes]:
    """Publish a same-depth snapshot and record before moving the latest pointer."""
    parent_id_value = parent_id(request.previous_revision_id)
    parent_record_path = _prepare_parent(
        request.daily_path, request.day, parent_id_value
    )
    body_bytes = request.markdown_body.encode("utf-8")
    revision_id = revision_identity(
        RevisionIdentity(request.day, parent_id_value, "daily", body_bytes, 2)
    )
    record_path, snapshot_path = month_paths(request.daily_path, request.day, revision_id)
    record_relative = Path(
        os.path.relpath(record_path, start=request.daily_path.parent)
    ).as_posix()
    snapshot_relative = Path(
        os.path.relpath(snapshot_path, start=request.daily_path.parent)
    ).as_posix()
    parent_marker = parent_id_value or "none"
    header = (
        f"<!-- chronicle:daily-revision revision_id={revision_id} "
        f"previous_revision_id={parent_marker} record={record_relative} "
        f"snapshot={snapshot_relative} -->\n"
    )
    snapshot = f"{header}{request.markdown_body}".encode("utf-8")
    publish_immutable(snapshot_path, snapshot, "revision snapshot")
    files = RevisionFiles(
        request.day,
        revision_id,
        parent_id_value,
        record_path,
        snapshot_path,
        parent_record_path,
        "daily",
        snapshot,
    )
    publish_immutable(record_path, record_bytes(files), "revision record")
    return (
        DailyRevision(revision_id, parent_id_value, record_path, snapshot_path, "daily"),
        snapshot,
    )
