"""Parsing of explicitly selected manual capture files."""

from datetime import datetime
import hashlib
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from chronicle.errors import ChronicleError
from chronicle.models import CaptureContext, CaptureOptions, CapturedInput
from chronicle.naming import safe_slug
from chronicle.privacy import normalize_text, reject_sensitive_metadata

_MAX_CAPTURE_BYTES = 10 * 1024 * 1024
_EVENT_TYPES = frozenset((
    "user_message", "assistant_message", "tool_call", "tool_result",
    "artifact_created", "artifact_revised", "test_result", "error",
    "approval", "session_closed", "coverage_gap", "file_change", "document", "other",
))


def _validate_text_field(value: str, name: str, limit: int = 512) -> str:
    """Parse a bounded non-empty CLI identity field."""
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in cleaned)
    ):
        raise ChronicleError("invalid_input", f"{name} must be non-empty and at most {limit} characters")
    return cleaned


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path is equal to or beneath a root."""
    return path == root or root in path.parents


def load_capture_input(options: CaptureOptions, context: CaptureContext) -> CapturedInput:
    """Read one explicitly named, strict UTF-8 file outside Chronicle stores."""
    reject_sensitive_metadata(
        options.source_event_id,
        options.source_session_id or "unknown",
        options.title,
        options.project_id,
        options.author_name,
        options.event_type,
    )
    if options.event_type not in _EVENT_TYPES:
        raise ChronicleError("invalid_input", "event type is unsupported")
    if options.file_path.is_symlink():
        raise ChronicleError("unsafe_input", "capture input may not be a symbolic link")
    try:
        resolved = options.file_path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ChronicleError("invalid_input", "capture input must be a regular file")
        reject_sensitive_metadata(resolved.name)
        if resolved.stat().st_size > _MAX_CAPTURE_BYTES:
            raise ChronicleError("input_too_large", "capture input exceeds the 10 MiB MVP limit")
        raw = resolved.read_bytes()
        if len(raw) > _MAX_CAPTURE_BYTES:
            raise ChronicleError("input_too_large", "capture input exceeds the 10 MiB MVP limit")
    except OSError as error:
        raise ChronicleError("input_error", error.strerror or "capture input cannot be read") from error
    protected_roots = (
        context.config.vault,
        context.paths.config_dir,
        context.paths.state_dir,
        context.paths.cache_dir,
    )
    if any(_is_within(resolved, root.expanduser().absolute()) for root in protected_roots):
        raise ChronicleError("unsafe_input", "Chronicle stores cannot be captured recursively")
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChronicleError("invalid_utf8", "capture input must be strict UTF-8") from error
    if content.startswith("\ufeff"):
        raise ChronicleError("invalid_bom", "capture input with UTF-8 BOM is not accepted")
    parsed_document_id: str | None = None
    if options.document_id is not None:
        try:
            parsed_document_id = str(UUID(options.document_id))
        except ValueError as error:
            raise ChronicleError("invalid_input", "document ID must be a UUID") from error
    if options.occurred_at is None:
        parsed_time = datetime.now(ZoneInfo(context.config.timezone))
        time_basis = "observed"
    else:
        try:
            parsed_time = datetime.fromisoformat(options.occurred_at)
        except ValueError as error:
            raise ChronicleError("invalid_input", "occurred-at must be ISO-8601") from error
        if parsed_time.tzinfo is None:
            raise ChronicleError("invalid_input", "occurred-at must include a timezone")
        time_basis = "source"
    return CapturedInput(
        content=normalize_text(content),
        source_event_id=_validate_text_field(options.source_event_id, "source-event-id"),
        source_session_id=_validate_text_field(options.source_session_id or "unknown", "session-id"),
        title=_validate_text_field(options.title, "title"),
        project_id=_validate_text_field(options.project_id, "project-id"),
        author_name=_validate_text_field(options.author_name, "author"),
        occurred_at=parsed_time.isoformat(timespec="seconds"),
        time_basis=time_basis,
        source_file_name=safe_slug(resolved.name, 120),
        source_file_id=hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
        document_id=parsed_document_id,
        final=options.final,
        event_type=options.event_type,
    )
