"""Commit Pi records, durable source cursors, and bounded failure metadata."""

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.locking import writer_lock
from chronicle.models import CaptureContext, CapturedInput
from chronicle.naming import safe_slug
from chronicle.pi_jsonl_models import PiRecord
from chronicle.source_models import CoverageGap, FileCollectionOutcome, SourceFileState, SourceRegistration

from chronicle.collector_files import file_id, now, relative_source_path, source_member_id, stream_id
from chronicle.collector_tokens import make_resume_token


def new_gap(
    source: SourceRegistration,
    member_id: str,
    relative: str,
    cursor: int,
    size: int,
    reason: str,
) -> CoverageGap:
    """Create a timestamped coverage-gap record without echoing source content."""
    return CoverageGap(source.source_id, member_id, relative, cursor, size, reason, now().isoformat(timespec="seconds"))


def base_state(
    source: SourceRegistration,
    member_id: str,
    relative: str,
    device: int,
    inode: int,
    generation: int,
    size: int,
) -> SourceFileState:
    """Build an empty generation cursor for a newly discovered file."""
    return SourceFileState(
        source.source_id, member_id, relative, file_id(member_id, generation),
        device, inode, generation, "", 0, hashlib.sha256(b"").hexdigest(),
        size, 0, "ready", None, None, 0, None,
    )


def check_retry(state: SourceFileState | None) -> bool:
    """Return whether a failed source remains inside its bounded retry delay."""
    if state is None or state.retry_after is None:
        return False
    try:
        return datetime.fromisoformat(state.retry_after) > now()
    except ValueError as error:
        raise ChronicleError("collector_state_corrupt", "source retry time is invalid") from error


def capture_record(
    context: CaptureContext,
    source: SourceRegistration,
    state: SourceFileState,
    record: PiRecord,
    prefix_sha256: str,
) -> bool:
    """Commit one redacted event and its daily projection before cursor ACK."""
    token = make_resume_token(
        state.device, state.inode, state.generation,
        record.record_end_byte_exclusive, state.session_id, state.member_id,
        prefix_sha256,
    )
    request = CapturedInput(
        content=record.content,
        source_event_id=record.source_event_id,
        source_session_id=state.session_id,
        title=source.title,
        project_id=source.project_id,
        author_name=record.actor_id,
        occurred_at=record.occurred_at,
        time_basis="source",
        source_file_name=safe_slug(f"pi-session-{state.member_id[:12]}.jsonl", 120),
        source_file_id=state.file_id,
        document_id=None,
        final=False,
        event_type=record.event_type,
        source_adapter_id="pi-jsonl",
        source_adapter_version="pi-jsonl-v3",
        source_stream_id=stream_id(source.source_id, state.member_id, state.generation),
        record_start_byte=record.record_start_byte,
        record_end_byte_exclusive=record.record_end_byte_exclusive,
        rotation_generation=f"generation-{state.generation}",
        non_resumable_reason=None,
        source_parent_event_id=record.parent_event_id,
        source_call_id=record.source_call_id,
        source_resume_token=token,
    )
    committed = capture_event(context, request)
    day = datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00")).astimezone(
        ZoneInfo(context.config.timezone)
    ).date()
    with writer_lock(context.config.vault):
        _daily = update_daily(context.config.vault, day, timezone_name=context.config.timezone)
    return committed.replayed


def failed_outcome(
    source: SourceRegistration,
    path: Path,
    prior: SourceFileState | None,
    error: ChronicleError,
) -> FileCollectionOutcome:
    """Preserve the last acknowledged cursor and schedule a bounded retry."""
    current = now()
    failures = 1 if prior is None else prior.consecutive_failures + 1
    attempt = 6 if failures > 6 else failures
    delay = 1 << attempt
    if delay > 60:
        delay = 60
    retry_after = (current + timedelta(seconds=delay)).isoformat(timespec="seconds")
    if prior is None:
        try:
            entry = path.lstat()
            device = entry.st_dev
            inode = entry.st_ino
            member_id = source_member_id(source.source_id, device, inode)
            generation = 1
            current_file_id = file_id(member_id, generation)
            relative = relative_source_path(source, path)
            size = entry.st_size if stat.S_ISREG(entry.st_mode) else 0
        except (OSError, ChronicleError):
            member_id, device, inode, generation, current_file_id, relative, size = (
                hashlib.sha256(f"{source.source_id}\0{path.name}".encode()).hexdigest(),
                0, 0, 1, "", path.name, 0,
            )
        state = SourceFileState(
            source.source_id, member_id, relative, current_file_id, device, inode, generation,
            "", 0, hashlib.sha256(b"").hexdigest(), size, 0, "failed", None,
            error.code, failures, retry_after,
        )
    else:
        state = SourceFileState(
            prior.source_id, prior.member_id, prior.relative_path, prior.file_id,
            prior.device, prior.inode, prior.generation, prior.session_id,
            prior.cursor, prior.prefix_sha256, prior.size_bytes, prior.pending_bytes,
            "failed", prior.last_success_at, error.code, failures, retry_after,
        )
    return FileCollectionOutcome(state, (), 0, 0, False, True)
