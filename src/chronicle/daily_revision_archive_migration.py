"""Copy verified v2 daily ancestry into link-compatible monthly companions."""

from datetime import date
from pathlib import Path

from chronicle.atomic import ensure_private_dir
from chronicle.daily_revisions import verify_daily_revision_chain as verify_v2_chain
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter
from chronicle.schema import optional_text
from chronicle.daily_revision_archive_storage import (
    legacy_paths,
    month_paths,
    parent_id,
    publish_immutable,
    read_regular_file,
)


def copy_v2_chain_to_month(daily_path: Path, day: date, revision_id: str) -> None:
    """Copy a verified v2 ancestry byte-for-byte without changing old files."""
    verified = verify_v2_chain(daily_path, day, revision_id)
    if verified.revision_id != revision_id:
        raise ChronicleError("daily_conflict", "v2 daily revision identity changed")
    legacy_record, _legacy_snapshot = legacy_paths(daily_path, day, revision_id)
    legacy_directory = legacy_record.parent
    month_directory = month_paths(daily_path, day, revision_id)[0].parent
    if legacy_directory.is_symlink() or not legacy_directory.is_dir():
        raise ChronicleError("daily_conflict", "v2 daily revision archive is unsafe")
    ensure_private_dir(month_directory)

    visited: set[str] = set()
    current_id: str | None = revision_id
    while current_id is not None:
        if current_id in visited:
            raise ChronicleError("daily_conflict", "v2 daily revision ancestry contains a cycle")
        visited.add(current_id)
        source_record = legacy_directory / f"{current_id}.md"
        source_snapshot = legacy_directory / f"{current_id}.snapshot.md"
        record_bytes = read_regular_file(source_record, "v2 revision record")
        snapshot_bytes = read_regular_file(source_snapshot, "v2 revision snapshot")
        publish_immutable(month_directory / source_snapshot.name, snapshot_bytes, "v2 snapshot alias")
        publish_immutable(month_directory / source_record.name, record_bytes, "v2 record alias")
        metadata, _body, _normalized = parse_frontmatter(record_bytes)
        current_id = parent_id(optional_text(metadata, "previous_revision_id"))
