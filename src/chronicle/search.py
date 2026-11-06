"""Korean-friendly NFC substring search over committed local Markdown."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import unicodedata

from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.event_classification import effective_event_metadata
from chronicle.event_format import read_capture
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter
from chronicle.models import JsonValue
from chronicle.schema import integer_field, text_field


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Explicit search filters parsed at the CLI boundary."""

    query: str
    day: date | None
    author: str | None
    project_id: str | None
    session_id: str | None = None
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One committed Markdown event or revision matching user filters."""

    path: Path
    kind: str
    title: str
    author_name: str
    project_id: str
    occurred_at: str
    event_id: str
    revision_id: str
    excerpt: str
    session_id: str = "unknown"
    event_type: str = "user_message"
    document_id: str | None = None
    document_revision: int = 1


def _excerpt(content: str, query: str) -> str:
    """Return a bounded excerpt around the first normalized query occurrence."""
    normalized = unicodedata.normalize("NFC", content)
    needle = unicodedata.normalize("NFC", query).casefold()
    offset = normalized.casefold().find(needle)
    if offset < 0:
        return content[:240]
    return normalized[max(0, offset - 100) : offset + len(query) + 140]


def _matches(
    metadata: dict[str, JsonValue],
    content: str,
    request: SearchRequest,
    timezone: ZoneInfo,
) -> bool:
    """Check text and metadata filters before constructing a result."""
    query = unicodedata.normalize("NFC", request.query).casefold()
    haystack = "\n".join((text_field(metadata, "title"), content)).casefold()
    if query not in unicodedata.normalize("NFC", haystack):
        return False
    if request.author is not None and text_field(metadata, "author_name") != request.author:
        return False
    if request.project_id is not None and text_field(metadata, "project_id") != request.project_id:
        return False
    if request.session_id is not None and text_field(metadata, "source_session_id") != request.session_id:
        return False
    if request.event_type is not None and text_field(metadata, "event_type") != request.event_type:
        return False
    if request.day is not None and datetime.fromisoformat(
        text_field(metadata, "occurred_at")
    ).astimezone(timezone).date() != request.day:
        return False
    return True


def _verify_revision(
    metadata: dict[str, JsonValue],
    content: str,
    source_metadata: dict[str, JsonValue],
    source_content: str,
) -> None:
    """Require each searchable revision to match its checkpointed source event."""
    expected = dict(source_metadata)
    expected["kind"] = "artifact_revision"
    if metadata != expected or content != source_content:
        raise ChronicleError("revision_conflict", "artifact revision differs from its checkpointed event")


def search_documents(
    vault: Path,
    request: SearchRequest,
    timezone_name: str,
) -> list[SearchHit]:
    """Search committed records and filter calendar dates in the pinned timezone."""
    if not request.query.strip() or len(request.query) > 512:
        raise ChronicleError("invalid_input", "search query must contain 1 to 512 characters")
    timezone = ZoneInfo(timezone_name)
    snapshot = verify_checkpoint_chain(vault)
    committed_events: dict[str, tuple[dict[str, JsonValue], str]] = {}
    committed_paths: dict[str, Path] = {}
    for reference in snapshot.references:
        event_path = vault / reference.relative_path
        event_metadata, event_content = read_capture(event_path)
        committed_events[reference.event_id] = (event_metadata, event_content)
        committed_paths[reference.event_id] = event_path
    hits: list[SearchHit] = []
    roots = (vault / "80_Events", vault / "10_Projects")
    seen_revisions: set[str] = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise ChronicleError("unsafe_path", "search root is missing or a symbolic link")
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink():
                raise ChronicleError("unsafe_path", "search root contains a symbolic link")
            raw = path.read_bytes()
            if not raw.startswith(b"---"):
                continue
            metadata, _body, _normalized = parse_frontmatter(raw)
            kind = metadata.get("kind")
            if kind not in ("event", "artifact_revision"):
                continue
            event_id = text_field(metadata, "event_id")
            source = committed_events.get(event_id)
            if kind == "event" and source is None:
                continue
            metadata, content = read_capture(path)
            if kind == "event":
                if source is None:
                    continue
                if (
                    path != committed_paths[event_id]
                    or metadata != source[0]
                    or content != source[1]
                ):
                    raise ChronicleError("checkpoint_conflict", "event Markdown differs from its committed reference")
            if kind == "artifact_revision":
                if source is None:
                    continue
                revision_id = text_field(metadata, "revision_id")
                if revision_id in seen_revisions:
                    raise ChronicleError("revision_conflict", "revision ID appears in multiple Markdown files")
                seen_revisions.add(revision_id)
                _verify_revision(metadata, content, source[0], source[1])
            metadata = effective_event_metadata(vault, metadata)
            if not _matches(metadata, content, request, timezone):
                continue
            hits.append(SearchHit(
                path,
                text_field(metadata, "kind"),
                text_field(metadata, "title"),
                text_field(metadata, "author_name"),
                text_field(metadata, "project_id"),
                text_field(metadata, "occurred_at"),
                text_field(metadata, "event_id"),
                text_field(metadata, "revision_id"),
                _excerpt(content, request.query),
                text_field(metadata, "source_session_id"),
                text_field(metadata, "event_type"),
                text_field(metadata, "document_id"),
                integer_field(metadata, "document_revision"),
            ))
    return sorted(hits, key=lambda item: (item.occurred_at, item.title, item.revision_id))
