"""Shared path, identity, and immutable-record helpers for daily archives."""

from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import uuid
from typing import Final, Literal

from chronicle.atomic import atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.markdown import render_markdown
from chronicle.models import JsonValue

HEADER_PREFIX: Final[str] = "<!-- chronicle:daily-revision"
HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\A<!-- chronicle:daily-revision revision_id=([0-9a-f-]{36}) "
    + r"previous_revision_id=(none|[0-9a-f-]{36}) record=([^\s]+) "
    + r"snapshot=([^\s]+) -->\n"
)
DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\]\(<([^>]+)>\)")


@dataclass(frozen=True, slots=True)
class RevisionIdentity:
    """Inputs for versioned immutable daily revision identity."""

    day: date
    parent_id: str | None
    mode: str
    content: bytes
    schema_version: Literal[1, 2]


@dataclass(frozen=True, slots=True)
class RevisionFiles:
    """Bytes and paths that determine one immutable revision record."""

    day: date
    revision_id: str
    previous_revision_id: str | None
    record_path: Path
    snapshot_path: Path
    previous_record_path: Path | None
    snapshot_mode: str
    snapshot: bytes


def month_paths(daily_path: Path, day: date, revision_id: str) -> tuple[Path, Path]:
    """Return current same-depth monthly archive paths for one revision."""
    directory = daily_path.parent.parent / f"{day:%m}_Revisions"
    return directory / f"{revision_id}.md", directory / f"{revision_id}.snapshot.md"


def legacy_paths(daily_path: Path, day: date, revision_id: str) -> tuple[Path, Path]:
    """Return v2 date-subdirectory paths without changing their location."""
    directory = daily_path.parent / f"{day.isoformat()}_Revisions"
    return directory / f"{revision_id}.md", directory / f"{revision_id}.snapshot.md"


def canonical_id(value: str, label: str) -> str:
    """Require one canonical UUID in a stored revision link."""
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise ChronicleError("daily_conflict", f"daily {label} is invalid") from error
    if canonical != value:
        raise ChronicleError("daily_conflict", f"daily {label} is not canonical")
    return value


def parent_id(value: str | None) -> str | None:
    """Validate an optional previous-revision identifier."""
    return canonical_id(value, "previous revision ID") if value is not None else None


def read_regular_file(path: Path, label: str) -> bytes:
    """Read a regular immutable archive file, rejecting symlinks and I/O errors."""
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("daily_conflict", f"daily {label} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ChronicleError("daily_conflict", f"daily {label} cannot be read") from error


def publish_immutable(path: Path, content: bytes, label: str) -> None:
    """Publish exact bytes once, accepting only a byte-identical retry."""
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ChronicleError("daily_conflict", f"daily {label} may not be a symbolic link")
    if path.exists():
        if read_regular_file(path, label) != content:
            raise ChronicleError("daily_conflict", f"existing daily {label} differs")
        return
    try:
        atomic_write_new(path, content)
    except ChronicleError as error:
        if error.code != "collision":
            raise
        if read_regular_file(path, label) != content:
            raise ChronicleError("daily_conflict", f"concurrent daily {label} differs") from error


def revision_identity(identity_input: RevisionIdentity) -> str:
    """Derive a stable revision ID in the selected archive schema domain."""
    digest = hashlib.sha256(identity_input.content).hexdigest()
    match identity_input.schema_version:
        case 1:
            identity_version = "v1"
        case 2:
            identity_version = "v2"
    identity = (
        f"agent-chronicle/daily/{identity_version}\0{identity_input.day.isoformat()}"
        f"\0{identity_input.mode}\0{identity_input.parent_id or 'none'}\0{digest}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def record_bytes(revision: RevisionFiles) -> bytes:
    """Render the deterministic v3 record and its resolvable revision links."""
    metadata: dict[str, JsonValue] = {
        "schema_version": 2,
        "kind": "daily_revision",
        "daily_date": revision.day.isoformat(),
        "revision_id": revision.revision_id,
        "previous_revision_id": revision.previous_revision_id,
        "snapshot_file": revision.snapshot_path.name,
        "snapshot_mode": revision.snapshot_mode,
        "snapshot_sha256": hashlib.sha256(revision.snapshot).hexdigest(),
    }
    if revision.previous_revision_id is None:
        previous_link = "none"
    elif revision.previous_record_path is None:
        raise ChronicleError("daily_conflict", "previous daily record is missing")
    else:
        relative_parent = Path(
            os.path.relpath(revision.previous_record_path, start=revision.record_path.parent)
        ).as_posix()
        previous_link = f"[{revision.previous_revision_id}](<{relative_parent}>)"
    body = (
        f"# Daily revision {revision.day.isoformat()}\n\n"
        f"- Snapshot: [{revision.snapshot_path.name}](<{revision.snapshot_path.name}>)\n"
        f"- Previous revision: {previous_link}\n"
    )
    return render_markdown(metadata, body).encode("utf-8")


def verify_record_links(
    record_path: Path,
    body: str,
    snapshot_path: Path,
    previous_record_path: Path | None,
) -> None:
    """Require record links to resolve to its snapshot and actual parent record."""
    expected = (snapshot_path,) + (
        (previous_record_path,) if previous_record_path is not None else ()
    )
    linked_targets: list[Path] = []
    for match in LINK_PATTERN.finditer(body):
        target = match.group(1)
        linked_targets.append(Path(os.path.normpath(record_path.parent / target)))
    targets = tuple(linked_targets)
    if targets != expected or any(not target.is_file() for target in targets):
        raise ChronicleError("daily_conflict", "daily revision navigation links are invalid")
