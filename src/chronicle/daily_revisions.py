"""Immutable daily Markdown snapshots and their verified causal chain."""

from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
import re
import uuid

from chronicle.atomic import atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import JsonValue
from chronicle.schema import integer_field, optional_text, text_field

_HEADER_PREFIX = "<!-- chronicle:daily-revision"
_HEADER = re.compile(
    r"\A<!-- chronicle:daily-revision revision_id=([0-9a-f-]{36}) "
    + r"previous_revision_id=(none|[0-9a-f-]{36}) record=([^\s]+) "
    + r"snapshot=([^\s]+) -->\n"
)
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class DailyRevision:
    """One immutable daily view and its parent reference."""

    revision_id: str
    previous_revision_id: str | None
    record_path: Path
    snapshot_path: Path
    snapshot_mode: str


def _revision_paths(daily_path: Path, day: date, revision_id: str) -> tuple[Path, Path]:
    directory = daily_path.parent / f"{day.isoformat()}_Revisions"
    return directory / f"{revision_id}.md", directory / f"{revision_id}.snapshot.md"


def _canonical_id(value: str, label: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise ChronicleError("daily_conflict", f"daily {label} is invalid") from error
    if canonical != value:
        raise ChronicleError(
            "daily_conflict", f"daily {label} must use canonical UUID form"
        )
    return value


def _parent_id(value: str | None) -> str | None:
    return _canonical_id(value, "previous revision ID") if value is not None else None


def parse_latest_revision(
    markdown: str,
    daily_path: Path,
    day: date,
) -> tuple[DailyRevision | None, str]:
    """Remove and parse the generated head marker, rejecting malformed links."""
    if not markdown.startswith(_HEADER_PREFIX):
        return None, markdown
    match = _HEADER.match(markdown)
    if match is None:
        raise ChronicleError("daily_conflict", "daily revision marker is malformed")
    revision_id = _canonical_id(match.group(1), "revision ID")
    previous_value = None if match.group(2) == "none" else match.group(2)
    previous_revision_id = _parent_id(previous_value)
    record_path, snapshot_path = _revision_paths(daily_path, day, revision_id)
    expected_record = record_path.relative_to(daily_path.parent).as_posix()
    expected_snapshot = snapshot_path.relative_to(daily_path.parent).as_posix()
    if match.group(3) != expected_record or match.group(4) != expected_snapshot:
        raise ChronicleError(
            "daily_conflict", "daily revision marker points outside its dated archive"
        )
    return DailyRevision(
        revision_id,
        previous_revision_id,
        record_path,
        snapshot_path,
        "daily",
    ), markdown[match.end() :]


def _read_regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("daily_conflict", f"daily {label} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ChronicleError(
            "daily_conflict", f"daily {label} cannot be read"
        ) from error


def _read_revision(
    daily_path: Path,
    day: date,
    revision_id: str,
) -> DailyRevision:
    revision_id = _canonical_id(revision_id, "revision ID")
    record_path, snapshot_path = _revision_paths(daily_path, day, revision_id)
    directory = record_path.parent
    if directory.is_symlink() or not directory.is_dir():
        raise ChronicleError(
            "daily_conflict", "daily revision archive directory is missing or unsafe"
        )
    record_bytes = _read_regular_file(record_path, "revision record")
    metadata, _body, _normalized = parse_frontmatter(record_bytes)
    if (
        integer_field(metadata, "schema_version") != 1
        or text_field(metadata, "kind") != "daily_revision"
        or text_field(metadata, "daily_date") != day.isoformat()
        or text_field(metadata, "revision_id") != revision_id
        or text_field(metadata, "snapshot_file") != snapshot_path.name
    ):
        raise ChronicleError(
            "daily_conflict", "daily revision record identity is inconsistent"
        )
    previous_revision_id = _parent_id(optional_text(metadata, "previous_revision_id"))
    snapshot_mode = text_field(metadata, "snapshot_mode")
    if snapshot_mode not in ("daily", "legacy") or (
        snapshot_mode == "legacy" and previous_revision_id is not None
    ):
        raise ChronicleError(
            "daily_conflict", "daily snapshot mode or ancestry is invalid"
        )
    expected_digest = text_field(metadata, "snapshot_sha256")
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ChronicleError("daily_conflict", "daily snapshot digest is malformed")
    snapshot = _read_regular_file(snapshot_path, "revision snapshot")
    if hashlib.sha256(snapshot).hexdigest() != expected_digest:
        raise ChronicleError(
            "daily_conflict", "daily revision snapshot digest does not match its record"
        )
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
        raise ChronicleError(
            "daily_conflict", "daily revision snapshot is not UTF-8"
        ) from error
    snapshot_head, snapshot_body = parse_latest_revision(snapshot_text, daily_path, day)
    if snapshot_mode == "daily":
        snapshot_identity = _revision_identity(
            day, previous_revision_id, "daily", snapshot_body.encode("utf-8")
        )
        if snapshot_head != revision or snapshot_identity != revision_id:
            raise ChronicleError(
                "daily_conflict", "daily snapshot differs from its revision identity"
            )
    elif (
        snapshot_head is not None
        or _revision_identity(day, None, "legacy", snapshot) != revision_id
    ):
        raise ChronicleError(
            "daily_conflict", "legacy daily snapshot differs from its revision identity"
        )
    return revision


def verify_daily_revision_chain(
    daily_path: Path,
    day: date,
    revision_id: str,
) -> DailyRevision:
    """Verify the current revision, every immutable snapshot, and its ancestry."""
    visited: set[str] = set()
    revision: DailyRevision | None = None
    current_id: str | None = revision_id
    while current_id is not None:
        if current_id in visited:
            raise ChronicleError(
                "daily_conflict", "daily revision ancestry contains a cycle"
            )
        visited.add(current_id)
        current = _read_revision(daily_path, day, current_id)
        if revision is None:
            revision = current
        current_id = current.previous_revision_id
    if revision is None:
        raise ChronicleError("daily_conflict", "daily revision chain is empty")
    return revision


def _publish_immutable(path: Path, content: bytes, label: str) -> None:
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ChronicleError(
            "daily_conflict", f"daily {label} may not be a symbolic link"
        )
    if path.exists():
        if _read_regular_file(path, label) != content:
            raise ChronicleError(
                "daily_conflict",
                f"existing daily {label} differs from deterministic retry",
            )
        return
    try:
        atomic_write_new(path, content)
    except ChronicleError as error:
        if error.code != "collision":
            raise
        if _read_regular_file(path, label) != content:
            raise ChronicleError(
                "daily_conflict", f"concurrent daily {label} differs from this revision"
            ) from error


def _revision_identity(
    day: date,
    parent_id: str | None,
    mode: str,
    content: bytes,
) -> str:
    digest = hashlib.sha256(content).hexdigest()
    identity = (
        f"agent-chronicle/daily/v1\0{day.isoformat()}\0{mode}"
        f"\0{parent_id or 'none'}\0{digest}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _record_bytes(
    day: date,
    revision_id: str,
    parent_id: str | None,
    snapshot_path: Path,
    snapshot_mode: str,
    snapshot: bytes,
) -> bytes:
    metadata: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": "daily_revision",
        "daily_date": day.isoformat(),
        "revision_id": revision_id,
        "previous_revision_id": parent_id,
        "snapshot_file": snapshot_path.name,
        "snapshot_mode": snapshot_mode,
        "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
    }
    previous_link = (
        f"[{parent_id}](<{parent_id}.md>)" if parent_id is not None else "none"
    )
    body = (
        f"# Daily revision {day.isoformat()}\n\n"
        f"- Snapshot: [{snapshot_path.name}](<{snapshot_path.name}>)\n"
        f"- Previous revision: {previous_link}\n"
    )
    return render_markdown(metadata, body).encode("utf-8")


def archive_legacy_daily(
    daily_path: Path,
    day: date,
    previous_markdown: bytes,
) -> DailyRevision:
    """Preserve an existing unversioned daily note byte-for-byte before migration."""
    revision_id = _revision_identity(day, None, "legacy", previous_markdown)
    record_path, snapshot_path = _revision_paths(daily_path, day, revision_id)
    _publish_immutable(snapshot_path, previous_markdown, "legacy snapshot")
    _publish_immutable(
        record_path,
        _record_bytes(
            day, revision_id, None, snapshot_path, "legacy", previous_markdown
        ),
        "legacy revision record",
    )
    return DailyRevision(revision_id, None, record_path, snapshot_path, "legacy")


def create_daily_revision(
    daily_path: Path,
    day: date,
    markdown_body: str,
    previous_revision_id: str | None,
) -> tuple[DailyRevision, bytes]:
    """Publish an immutable daily snapshot and record before moving its latest view."""
    parent_id = _parent_id(previous_revision_id)
    body_bytes = markdown_body.encode("utf-8")
    revision_id = _revision_identity(day, parent_id, "daily", body_bytes)
    record_path, snapshot_path = _revision_paths(daily_path, day, revision_id)
    record_relative = record_path.relative_to(daily_path.parent).as_posix()
    snapshot_relative = snapshot_path.relative_to(daily_path.parent).as_posix()
    parent_marker = parent_id or "none"
    header = (
        f"<!-- chronicle:daily-revision revision_id={revision_id} "
        f"previous_revision_id={parent_marker} record={record_relative} "
        f"snapshot={snapshot_relative} -->\n"
    )
    snapshot = f"{header}{markdown_body}".encode("utf-8")
    _publish_immutable(snapshot_path, snapshot, "revision snapshot")
    _publish_immutable(
        record_path,
        _record_bytes(day, revision_id, parent_id, snapshot_path, "daily", snapshot),
        "revision record",
    )
    return (
        DailyRevision(revision_id, parent_id, record_path, snapshot_path, "daily"),
        snapshot,
    )
