"""Explicit local Pi session import orchestration."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chronicle.command_requests import ImportCommand
from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.locking import writer_lock
from chronicle.models import CaptureContext, CapturedInput, JsonValue
from chronicle.pi_jsonl import parse_pi_jsonl


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a selected file is inside or equal to a protected root."""
    return path == root or root in path.parents


def import_pi_session(context: CaptureContext, command: ImportCommand) -> JsonValue:
    """Preview or import a bounded range from one explicitly selected Pi session."""
    selected = command.file_path.expanduser()
    if selected.is_symlink():
        raise ChronicleError("unsafe_input", "session input may not be a symbolic link")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ChronicleError("input_error", "session input cannot be resolved") from error
    protected_roots = (
        context.config.vault,
        context.paths.config_dir,
        context.paths.state_dir,
        context.paths.cache_dir,
    )
    if any(_is_within(resolved, root.absolute()) for root in protected_roots):
        raise ChronicleError("unsafe_input", "Chronicle stores cannot be imported recursively")
    session = parse_pi_jsonl(
        resolved,
        start_byte=command.start_byte,
        end_byte=command.end_byte,
    )
    if command.dry_run:
        return {
            "status": "dry_run",
            "source": "pi-jsonl-v3",
            "selected_records": len(session.records),
            "skipped_entries": session.skipped_entries,
            "omitted_content_blocks": session.omitted_content_blocks,
            "pending_bytes": session.pending_bytes,
            "start_byte": session.selected_start_byte,
            "end_byte": session.selected_end_byte,
            "writes": 0,
        }

    days: set[date] = set()
    replayed = 0
    for record in session.records:
        request = CapturedInput(
            record.content,
            record.source_event_id,
            session.session_id,
            command.title,
            command.project_id,
            record.actor_id,
            record.occurred_at,
            "source",
            session.source_file_name,
            session.source_file_id,
            None,
            False,
            event_type=record.event_type,
            source_adapter_id="pi-jsonl",
            source_adapter_version="pi-jsonl-v3",
            source_stream_id=session.stream_id,
            record_start_byte=record.record_start_byte,
            record_end_byte_exclusive=record.record_end_byte_exclusive,
            rotation_generation="static-export-v3",
            non_resumable_reason="explicit import does not tail or resume a live session",
            source_parent_event_id=record.parent_event_id,
            source_call_id=record.source_call_id,
        )
        commit = capture_event(context, request)
        replayed += int(commit.replayed)
        days.add(datetime.fromisoformat(record.occurred_at).astimezone(
            ZoneInfo(context.config.timezone)
        ).date())

    daily_conflicts = 0
    for day in sorted(days):
        with writer_lock(context.config.vault):
            daily = update_daily(context.config.vault, day, timezone_name=context.config.timezone)
        daily_conflicts += int(daily.conflict_copy is not None)
    return {
        "status": "imported_with_pending_tail" if session.pending_bytes else "imported",
        "source": "pi-jsonl-v3",
        "selected_records": len(session.records),
        "new_events": len(session.records) - replayed,
        "replayed_events": replayed,
        "skipped_entries": session.skipped_entries,
        "omitted_content_blocks": session.omitted_content_blocks,
        "pending_bytes": session.pending_bytes,
        "daily_conflicts": daily_conflicts,
        "processed_events": len(session.records),
    }
