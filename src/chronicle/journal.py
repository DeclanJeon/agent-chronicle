"""Markdown-first capture, checkpoint commit, and revision publication."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chronicle.atomic import atomic_write_new, fsync_existing_file_and_parent
from chronicle.checkpoints import CheckpointInput, publish_checkpoint
from chronicle.destination import verify_vault
from chronicle.documents import plan_revision, publish_revision
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture, render_capture
from chronicle.models import CaptureContext, CapturedInput, DocumentPlan, JournalCommit, JsonValue
from chronicle.markdown import parse_frontmatter
from chronicle.naming import safe_slug, sha256_text
from chronicle.privacy import redact_text
from chronicle.recovery import rebuild_from_markdown
from chronicle.locking import writer_lock
from chronicle.schema import text_field
from chronicle.source_provenance import (
    checkpoint_position,
    event_identity,
    source_position,
    validate_source_replay,
    validate_source_request,
)


@dataclass(frozen=True, slots=True)
class EventDraft:
    """Prepared, redacted payload and stable event/revision identity."""

    key: str
    event_id: str
    stream_id: str
    content: str
    plan: DocumentPlan
    observed_at: str


@dataclass(frozen=True, slots=True)
class ReplayInput:
    """Immutable source fields required to validate an idempotent replay."""

    request: CapturedInput
    idempotency_key: str
    content: str


def _find_event(vault: Path, event_id: str) -> tuple[Path, dict[str, JsonValue], str] | None:
    """Find exactly one immutable event by its complete ID."""
    matches: list[tuple[Path, dict[str, JsonValue], str]] = []
    event_root = vault / "80_Events"
    if event_root.is_symlink() or not event_root.is_dir():
        raise ChronicleError("unsafe_path", "event directory is missing or a symbolic link")
    for path in event_root.rglob("*.md"):
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "symbolic link found in event ledger")
        metadata, _body, _normalized = parse_frontmatter(path.read_bytes())
        if metadata.get("kind") == "event" and metadata.get("event_id") == event_id:
            _metadata, content = read_capture(path)
            matches.append((path, metadata, content))
    if len(matches) > 1:
        raise ChronicleError("event_conflict", "event ID appears in multiple Markdown files")
    return matches[0] if matches else None


def _validate_replay(metadata: dict[str, JsonValue], replay: ReplayInput) -> None:
    """Ensure replayed source identity and immutable payload metadata are unchanged."""
    request = replay.request
    if text_field(metadata, "idempotency_key") != replay.idempotency_key:
        raise ChronicleError("idempotency_conflict", "stored event identity does not match")
    if text_field(metadata, "payload_hash") != sha256_text(replay.content):
        raise ChronicleError("idempotency_conflict", "same source identity has different content")
    for key, requested in (
        ("source_session_id", request.source_session_id),
        ("source_file_id", request.source_file_id),
        ("source_file_name", request.source_file_name),
        ("title", request.title),
        ("project_id", request.project_id),
        ("author_name", request.author_name),
        ("time_basis", request.time_basis),
        ("event_type", request.event_type),
        ("source", request.source_adapter_id),
        ("source_version", request.source_adapter_version),
    ):
        if text_field(metadata, key) != requested:
            raise ChronicleError("idempotency_conflict", f"replay metadata changed: {key}")
    if request.time_basis == "source" and text_field(metadata, "occurred_at") != request.occurred_at:
        raise ChronicleError("idempotency_conflict", "source timestamp changed for an existing event")
    if request.document_id is not None and text_field(metadata, "document_id") != request.document_id:
        raise ChronicleError("idempotency_conflict", "document ID changed for an existing event")
    if text_field(metadata, "review_status") != ("final" if request.final else "draft"):
        raise ChronicleError("idempotency_conflict", "finalization state changed for an existing event")
    validate_source_replay(metadata, request, replay.content)


def _event_metadata(
    context: CaptureContext,
    request: CapturedInput,
    draft: EventDraft,
) -> dict[str, JsonValue]:
    """Build the complete v2 event envelope after redaction."""
    content_bytes = draft.content.encode("utf-8", errors="strict")
    plan = draft.plan
    return {
        "schema_version": 2,
        "kind": "event",
        "event_id": draft.event_id,
        "source_instance_id": context.config.source_instance_id,
        "source_event_id": request.source_event_id,
        "source_session_id": request.source_session_id,
        "idempotency_key_version": 1,
        "idempotency_key": draft.key,
        "source_position": source_position(request, draft.stream_id, content_bytes),
        "event_type": request.event_type,
        "actor_id": request.author_name,
        "occurred_at": request.occurred_at,
        "observed_at": draft.observed_at,
        "time_basis": request.time_basis,
        "timezone": datetime.fromisoformat(request.occurred_at).tzname() or "local-offset",
        "sequence": plan.revision_number,
        "project_id": request.project_id,
        "title": request.title,
        "author_name": request.author_name,
        "source_file_id": request.source_file_id,
        "source_file_name": request.source_file_name,
        "source": request.source_adapter_id,
        "source_version": request.source_adapter_version,
        "redaction_policy": "default-v1",
        "access_policy_version": "local-owner-v1",
        "payload_hash": sha256_text(draft.content),
        "content_chars": len(draft.content),
        "document_id": plan.document_id,
        "revision_id": plan.revision_id,
        "document_revision": plan.revision_number,
        "previous_revision_id": plan.previous_revision_id,
        "review_status": "final" if request.final else "draft",
        "change_type": "final" if request.final else ("draft" if plan.revision_number == 1 else "revision"),
    }


def _event_path(context: CaptureContext, metadata: dict[str, JsonValue]) -> Path:
    """Build an event path using the pinned installation timezone."""
    instant = datetime.fromisoformat(text_field(metadata, "occurred_at")).astimezone(
        ZoneInfo(context.config.timezone)
    )
    stamp = instant.strftime("%Y%m%d_%H%M%S")
    title = safe_slug(text_field(metadata, "title"), 60)
    author = safe_slug(text_field(metadata, "author_name"), 32)
    event_id = text_field(metadata, "event_id")
    return (
        context.config.vault / "80_Events" / instant.strftime("%Y/%m")
        / f"{stamp}_{title}_{author}_event_v001_{event_id[:8]}.md"
    )


def capture_event(
    context: CaptureContext,
    request: CapturedInput,
    fault_at: str | None = None,
) -> JournalCommit:
    """Commit one explicit capture while holding the OS-level writer lock."""
    validate_source_request(request)
    config = context.config
    vault = verify_vault(config.vault, config.host_id, config.source_instance_id)
    with writer_lock(vault):
        return _capture_event_locked(context, request, fault_at)


def _capture_event_locked(
    context: CaptureContext,
    request: CapturedInput,
    fault_at: str | None,
) -> JournalCommit:
    """Commit one explicit capture event after destination and lock checks."""
    config = context.config
    vault = verify_vault(config.vault, config.host_id, config.source_instance_id)
    content = redact_text(request.content)
    key, event_id, stream_id = event_identity(config.source_instance_id, request)
    existing = _find_event(vault, event_id)
    if existing is None:
        plan = plan_revision(vault, request, event_id)
        draft = EventDraft(
            key, event_id, stream_id, content, plan,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        metadata = _event_metadata(context, request, draft)
        event_path = _event_path(context, metadata)
        atomic_write_new(event_path, render_capture(metadata, request.title, content, request.source_file_name).encode("utf-8"))
        if fault_at == "after_event":
            raise ChronicleError("injected_failure", "stopped after event publication before checkpoint")
    else:
        event_path, metadata, stored_content = existing
        _validate_replay(metadata, ReplayInput(request, key, content))
        if stored_content != content:
            raise ChronicleError("idempotency_conflict", "stored event body differs from the masked input")
        fsync_existing_file_and_parent(event_path)
    payload_hash = text_field(metadata, "payload_hash")
    next_position, end_position, commit_reason = checkpoint_position(metadata, request)
    checkpoint = publish_checkpoint(vault, CheckpointInput(
        config.source_instance_id, stream_id, event_id, key, payload_hash, event_path,
        next_position, end_position, request.source_adapter_version,
        text_field(metadata, "observed_at"),
        commit_reason,
    ))
    if fault_at == "after_checkpoint":
        raise ChronicleError("injected_failure", "stopped after checkpoint before SQLite projection")
    revision_path = publish_revision(context, metadata, content)
    _recovery = rebuild_from_markdown(context)
    return JournalCommit(
        event_id, text_field(metadata, "revision_id"), event_path, revision_path,
        checkpoint.checkpoint_id, existing is not None,
    )
