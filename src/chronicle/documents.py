"""Immutable document revision planning and durable publication."""

from datetime import datetime
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from chronicle.atomic import atomic_write_new, ensure_private_dir, require_private_directory
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture, render_capture
from chronicle.models import CaptureContext, CapturedInput, DocumentPlan, JsonValue
from chronicle.naming import safe_slug
from chronicle.schema import integer_field, text_field


def plan_revision(vault: Path, request: CapturedInput, event_id: str) -> DocumentPlan:
    """Reserve the next revision number under the caller's writer lock."""
    document_id = request.document_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"document:{event_id}"))
    revision_dir = vault / "10_Projects" / safe_slug(request.project_id) / "05_Deliverables" / document_id
    ensure_private_dir(revision_dir)
    require_private_directory(revision_dir)
    if revision_dir.is_symlink():
        raise ChronicleError("unsafe_path", "document revision directory is a symbolic link")
    latest_number = 0
    latest_id: str | None = None
    if revision_dir.exists():
        if not revision_dir.is_dir():
            raise ChronicleError("unsafe_path", "document revision path is not a directory")
        for path in sorted(revision_dir.glob("*.md")):
            if path.is_symlink():
                raise ChronicleError("unsafe_path", "revision path is a symbolic link")
            metadata, _content = read_capture(path)
            if text_field(metadata, "kind") != "artifact_revision":
                raise ChronicleError("revision_conflict", "document directory contains a non-revision Markdown file")
            revision = integer_field(metadata, "document_revision")
            if revision > latest_number:
                latest_number = revision
                latest_id = text_field(metadata, "revision_id")
    revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-chronicle/revision/v1/{event_id}"))
    return DocumentPlan(document_id, revision_id, latest_number + 1, latest_id)


def _existing_revision_path(
    revision_dir: Path,
    revision_id: str,
    expected_bytes: bytes,
) -> Path | None:
    """Find one byte-identical immutable revision regardless of its old filename."""
    if revision_dir.is_symlink():
        raise ChronicleError(
            "unsafe_path", "document revision directory is a symbolic link"
        )
    if not revision_dir.exists():
        return None
    if not revision_dir.is_dir():
        raise ChronicleError("unsafe_path", "document revision path is not a directory")

    matches: list[Path] = []
    for candidate in sorted(revision_dir.glob("*.md")):
        if candidate.is_symlink():
            raise ChronicleError("unsafe_path", "revision path is a symbolic link")
        candidate_metadata, _content = read_capture(candidate)
        if text_field(candidate_metadata, "kind") != "artifact_revision":
            raise ChronicleError(
                "revision_conflict",
                "document directory contains a non-revision Markdown file",
            )
        if text_field(candidate_metadata, "revision_id") == revision_id:
            matches.append(candidate)

    if len(matches) > 1:
        raise ChronicleError(
            "revision_conflict", "revision ID appears in multiple Markdown files"
        )
    if not matches:
        return None
    existing_path = matches[0]
    if existing_path.read_bytes() != expected_bytes:
        raise ChronicleError(
            "revision_conflict", "existing revision differs from immutable event"
        )
    return existing_path


def publish_revision(
    context: CaptureContext,
    metadata: dict[str, JsonValue],
    content: str,
) -> Path:
    """Create a revision once or verify byte-identical replay."""
    instant = datetime.fromisoformat(text_field(metadata, "occurred_at")).astimezone(
        ZoneInfo(context.config.timezone)
    )
    revision = integer_field(metadata, "document_revision")
    filename_prefix = (
        f"{instant:%Y%m%d_%H%M%S}_{safe_slug(text_field(metadata, 'title'), 60)}"
    )
    author_slug = safe_slug(text_field(metadata, "author_name"), 32)
    revision_id = text_field(metadata, "revision_id")
    revision_suffix = f"_{author_slug}_revision_v{revision:03d}_{revision_id[:8]}.md"
    filename = filename_prefix + revision_suffix
    path = (
        context.config.vault / "10_Projects" / safe_slug(text_field(metadata, "project_id"))
        / "05_Deliverables" / text_field(metadata, "document_id") / filename
    )
    revision_metadata = dict(metadata)
    revision_metadata["kind"] = "artifact_revision"
    body = render_capture(
        revision_metadata,
        text_field(metadata, "title"),
        content,
        text_field(metadata, "source_file_name"),
    )
    encoded = body.encode("utf-8")
    existing_path = _existing_revision_path(path.parent, revision_id, encoded)
    if existing_path is not None:
        return existing_path
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ChronicleError("revision_conflict", "existing revision differs from immutable event")
    else:
        atomic_write_new(path, encoded)
    return path
