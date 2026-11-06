"""Read and verify same-depth daily revision archives with v2 compatibility."""

from datetime import date
import hashlib
import os
from pathlib import Path
from typing import Literal

from chronicle.daily_revisions import (
    DailyRevision,
    parse_latest_revision as parse_v2_latest_revision,
    verify_daily_revision_chain as verify_v2_chain,
)
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter
from chronicle.schema import integer_field, optional_text, text_field
from chronicle.daily_revision_archive_storage import (
    DIGEST_PATTERN,
    HEADER_PATTERN,
    HEADER_PREFIX,
    RevisionFiles,
    RevisionIdentity,
    canonical_id,
    legacy_paths,
    month_paths,
    parent_id,
    read_regular_file,
    record_bytes,
    revision_identity,
    verify_record_links,
)


def parse_latest_revision(
    markdown: str,
    daily_path: Path,
    day: date,
) -> tuple[DailyRevision | None, str]:
    """Parse current markers, falling back to the immutable v2 path contract."""
    if not markdown.startswith(HEADER_PREFIX):
        return parse_v2_latest_revision(markdown, daily_path, day)
    match = HEADER_PATTERN.match(markdown)
    if match is None:
        return parse_v2_latest_revision(markdown, daily_path, day)

    revision_id = canonical_id(match.group(1), "revision ID")
    previous_value = None if match.group(2) == "none" else match.group(2)
    previous_revision_id = parent_id(previous_value)
    record_path, snapshot_path = month_paths(daily_path, day, revision_id)
    marked_record = Path(os.path.normpath(daily_path.parent / match.group(3)))
    marked_snapshot = Path(os.path.normpath(daily_path.parent / match.group(4)))
    if marked_record != record_path or marked_snapshot != snapshot_path:
        return parse_v2_latest_revision(markdown, daily_path, day)
    return (
        DailyRevision(
            revision_id,
            previous_revision_id,
            record_path,
            snapshot_path,
            "daily",
        ),
        markdown[match.end() :],
    )


def read_month_revision(
    daily_path: Path,
    day: date,
    revision_id: str,
) -> DailyRevision:
    """Verify one current-layout record and its snapshot, links, and identity."""
    revision_id = canonical_id(revision_id, "revision ID")
    record_path, snapshot_path = month_paths(daily_path, day, revision_id)
    directory = record_path.parent
    if directory.is_symlink() or not directory.is_dir():
        raise ChronicleError("daily_conflict", "daily revision archive is missing or unsafe")
    record_data = read_regular_file(record_path, "revision record")
    metadata, record_body, _normalized = parse_frontmatter(record_data)
    schema_version = integer_field(metadata, "schema_version")
    match schema_version:  # noqa: MATCH_OK — reject unknown persisted schema versions
        case 1:
            identity_schema_version: Literal[1, 2] = 1
        case 2:
            identity_schema_version = 2
        case _:
            raise ChronicleError("daily_conflict", "daily revision schema is unsupported")
    if (
        text_field(metadata, "kind") != "daily_revision"
        or text_field(metadata, "daily_date") != day.isoformat()
        or text_field(metadata, "revision_id") != revision_id
        or text_field(metadata, "snapshot_file") != snapshot_path.name
    ):
        raise ChronicleError("daily_conflict", "daily revision record identity is inconsistent")
    previous_revision_id = parent_id(optional_text(metadata, "previous_revision_id"))
    snapshot_mode_value = text_field(metadata, "snapshot_mode")
    match snapshot_mode_value:  # noqa: MATCH_OK — reject unknown persisted snapshot modes
        case "daily":
            snapshot_mode: Literal["daily", "legacy"] = "daily"
        case "legacy":
            snapshot_mode = "legacy"
        case _:
            raise ChronicleError("daily_conflict", "daily snapshot mode is invalid")
    if snapshot_mode == "legacy" and previous_revision_id is not None:
        raise ChronicleError("daily_conflict", "legacy snapshot ancestry is invalid")
    expected_digest = text_field(metadata, "snapshot_sha256")
    if DIGEST_PATTERN.fullmatch(expected_digest) is None:
        raise ChronicleError("daily_conflict", "daily snapshot digest is malformed")
    snapshot = read_regular_file(snapshot_path, "revision snapshot")
    if hashlib.sha256(snapshot).hexdigest() != expected_digest:
        raise ChronicleError("daily_conflict", "daily revision snapshot digest does not match")

    previous_record_path = (
        month_paths(daily_path, day, previous_revision_id)[0]
        if previous_revision_id is not None
        else None
    )
    if previous_record_path is not None and not previous_record_path.is_file():
        raise ChronicleError("daily_conflict", "previous daily revision record is missing")
    revision = DailyRevision(
        revision_id,
        previous_revision_id,
        record_path,
        snapshot_path,
        snapshot_mode,
    )
    try:
        snapshot_text = snapshot.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChronicleError("daily_conflict", "daily snapshot is not UTF-8") from error
    marker_base = snapshot_path if identity_schema_version == 2 else daily_path
    snapshot_head, snapshot_body = parse_latest_revision(snapshot_text, marker_base, day)
    if snapshot_mode == "daily":
        snapshot_identity = revision_identity(RevisionIdentity(
            day,
            previous_revision_id,
            "daily",
            snapshot_body.encode("utf-8"),
            identity_schema_version,
        ))
        if (
            snapshot_head is None
            or snapshot_head.revision_id != revision_id
            or snapshot_head.previous_revision_id != previous_revision_id
            or snapshot_identity != revision_id
        ):
            raise ChronicleError("daily_conflict", "daily snapshot differs from its revision identity")
        if identity_schema_version == 2 and snapshot_head != revision:
            raise ChronicleError("daily_conflict", "daily snapshot pointer differs from its record")
    elif (
        snapshot_head is not None
        or revision_identity(RevisionIdentity(
            day, None, "legacy", snapshot, identity_schema_version
        )) != revision_id
    ):
        raise ChronicleError("daily_conflict", "legacy daily snapshot differs from its identity")
    verify_record_links(record_path, record_body, snapshot_path, previous_record_path)
    files = RevisionFiles(
        day,
        revision_id,
        previous_revision_id,
        record_path,
        snapshot_path,
        previous_record_path,
        snapshot_mode,
        snapshot,
    )
    if identity_schema_version == 2 and record_data != record_bytes(files):
        raise ChronicleError("daily_conflict", "daily revision record differs from its identity")
    return revision


def _verify_legacy_alias_chain(daily_path: Path, day: date, revision_id: str) -> None:
    """Verify aliases for this v2 ancestry when any of its aliases are present."""
    visited: set[str] = set()
    revision_ids: list[str] = []
    current_id: str | None = revision_id
    has_alias = False
    while current_id is not None:
        if current_id in visited:
            raise ChronicleError(
                "daily_conflict", "daily revision ancestry contains a cycle"
            )
        visited.add(current_id)
        legacy_record, _legacy_snapshot = legacy_paths(daily_path, day, current_id)
        metadata, _body, _normalized = parse_frontmatter(
            read_regular_file(legacy_record, "v2 revision record")
        )
        previous_revision_id = parent_id(
            optional_text(metadata, "previous_revision_id")
        )
        month_record, month_snapshot = month_paths(daily_path, day, current_id)
        if month_record.is_symlink() or month_snapshot.is_symlink():
            raise ChronicleError("daily_conflict", "v2 daily archive alias is unsafe")
        has_alias = has_alias or month_record.exists() or month_snapshot.exists()
        revision_ids.append(current_id)
        current_id = previous_revision_id

    if not has_alias:
        return
    for current_id in revision_ids:
        legacy_record, legacy_snapshot = legacy_paths(daily_path, day, current_id)
        month_record, month_snapshot = month_paths(daily_path, day, current_id)
        _ = read_month_revision(daily_path, day, current_id)
        if (
            read_regular_file(month_record, "revision record")
            != read_regular_file(legacy_record, "v2 revision record")
            or read_regular_file(month_snapshot, "revision snapshot")
            != read_regular_file(legacy_snapshot, "v2 revision snapshot")
        ):
            raise ChronicleError(
                "daily_conflict", "v2 daily archive alias differs from its source"
            )


def verify_daily_revision_chain(
    daily_path: Path,
    day: date,
    revision_id: str,
) -> DailyRevision:
    """Verify current records and aliased v2 ancestry without changing old bytes."""
    revision_id = canonical_id(revision_id, "revision ID")
    legacy_record = legacy_paths(daily_path, day, revision_id)[0]
    if legacy_record.is_symlink():
        raise ChronicleError("daily_conflict", "v2 daily revision record is unsafe")
    if legacy_record.exists():
        legacy_revision = verify_v2_chain(daily_path, day, revision_id)
        month_directory = month_paths(daily_path, day, revision_id)[0].parent
        if month_directory.is_symlink():
            raise ChronicleError("daily_conflict", "daily revision archive is unsafe")
        if month_directory.exists():
            if not month_directory.is_dir():
                raise ChronicleError(
                    "daily_conflict", "daily revision archive is unsafe"
                )
            _verify_legacy_alias_chain(daily_path, day, revision_id)
        return legacy_revision

    current_record = month_paths(daily_path, day, revision_id)[0]
    if current_record.is_symlink():
        raise ChronicleError("daily_conflict", "daily revision record is unsafe")
    if not current_record.exists():
        return verify_v2_chain(daily_path, day, revision_id)

    visited: set[str] = set()
    current_id: str | None = revision_id
    latest: DailyRevision | None = None
    while current_id is not None:
        if current_id in visited:
            raise ChronicleError(
                "daily_conflict", "daily revision ancestry contains a cycle"
            )
        visited.add(current_id)
        current = read_month_revision(daily_path, day, current_id)
        if latest is None:
            latest = current
        current_id = current.previous_revision_id
    if latest is None:
        raise ChronicleError("daily_conflict", "daily revision chain is empty")
    return latest
