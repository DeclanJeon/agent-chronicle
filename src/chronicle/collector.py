"""Foreground polling, coverage reporting, and checkpoint-backed resume."""

from datetime import datetime
import hashlib
from pathlib import Path
import stat

from chronicle.checkpoints import RecoverySnapshot, verify_checkpoint_chain
from chronicle.collector_files import scan_source_paths, source_member_id
from chronicle.collector_process import collect_source_file
from chronicle.collector_state import read_collector_state, write_collector_state
from chronicle.errors import ChronicleError
from chronicle.locking import collector_is_running, collector_lock
from chronicle.models import CaptureContext, JsonValue
from chronicle.source_models import (
    CollectorSnapshot,
    CollectorSummary,
    CoverageGap,
    FileCollectionOutcome,
    SourceFileState,
    SourceRegistration,
)
from chronicle.source_registry import list_sources, source_as_json


def _now() -> str:
    """Return an aware timestamp for local status records."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state_member(path: Path, source: SourceRegistration) -> str | None:
    """Derive a registered file identity without following a symlink."""
    try:
        entry = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        return None
    return source_member_id(source.source_id, entry.st_dev, entry.st_ino)


def _scope_failure(source: SourceRegistration, error: ChronicleError) -> FileCollectionOutcome:
    """Represent a root-scan failure as a source-level failed status row."""
    member_id = hashlib.sha256(f"{source.source_id}\0scope".encode()).hexdigest()
    state = SourceFileState(
        source.source_id, member_id, "[source-scan]", "", 0, 0, 1,
        "", 0, hashlib.sha256(b"").hexdigest(), 0, 0, "failed",
        None, error.code, 1, None,
    )
    return FileCollectionOutcome(state, (), 0, 0, False, True)


def _mark_missing(
    state: SourceFileState,
    now: str,
) -> tuple[SourceFileState, CoverageGap | None]:
    """Retain a vanished file cursor and add one explicit coverage gap."""
    if state.status == "missing":
        return state, None
    missing = SourceFileState(
        state.source_id, state.member_id, state.relative_path, state.file_id,
        state.device, state.inode, state.generation, state.session_id,
        state.cursor, state.prefix_sha256, state.size_bytes, state.pending_bytes,
        "missing", state.last_success_at, "source_file_missing", state.consecutive_failures,
        state.retry_after,
    )
    gap = CoverageGap(
        state.source_id, state.member_id, state.relative_path,
        state.cursor, None, "source_file_missing_after_rotation", now,
    )
    return missing, gap


def collect_once_locked(context: CaptureContext) -> CollectorSummary:
    """Reconcile all enabled sources while the caller owns the collector lock."""
    registry = list_sources(context)
    enabled = tuple(source for source in registry if source.enabled)
    previous = read_collector_state(context)
    now = _now()
    if not enabled:
        updated = CollectorSnapshot(now, now, "idle", previous.files, previous.gaps)
        write_collector_state(context, updated)
        return CollectorSummary("idle", 0, 0, 0, 0, 0, 0, 0)

    recovery = verify_checkpoint_chain(context.config.vault)
    by_member = {item.member_id: item for item in previous.files}
    updated_by_member = dict(by_member)
    gaps = list(previous.gaps)
    seen_by_source: dict[str, set[str]] = {source.source_id: set() for source in enabled}
    scan_failed_sources: set[str] = set()
    files_seen = new_events = replayed_events = pending_files = failed_files = gaps_added = 0

    for source in enabled:
        try:
            paths = scan_source_paths(source)
        except ChronicleError as error:
            outcome = _scope_failure(source, error)
            scan_failed_sources.add(source.source_id)
            updated_by_member[outcome.state.member_id] = outcome.state
            failed_files += 1
            continue
        for path in paths:
            files_seen += 1
            member_id = _state_member(path, source)
            prior = by_member.get(member_id) if member_id is not None else None
            outcome = collect_source_file(context, source, path, prior, recovery)
            updated_by_member[outcome.state.member_id] = outcome.state
            seen_by_source[source.source_id].add(outcome.state.member_id)
            gaps.extend(outcome.gaps)
            gaps_added += len(outcome.gaps)
            new_events += outcome.new_events
            replayed_events += outcome.replayed_events
            pending_files += int(outcome.pending)
            failed_files += int(outcome.failed)

    for source in enabled:
        if source.source_id in scan_failed_sources:
            continue
        seen = seen_by_source[source.source_id]
        for old in previous.files:
            if old.source_id == source.source_id and old.member_id not in seen:
                missing, gap = _mark_missing(old, now)
                updated_by_member[old.member_id] = missing
                if gap is not None:
                    gaps.append(gap)
                    gaps_added += 1

    status = "failed" if failed_files else ("pending" if pending_files else "completed")
    snapshot = CollectorSnapshot(
        now, now, status,
        tuple(sorted(updated_by_member.values(), key=lambda item: item.member_id)),
        tuple(gaps),
    )
    write_collector_state(context, snapshot)
    return CollectorSummary(
        status, len(enabled), files_seen, new_events, replayed_events,
        pending_files, failed_files, gaps_added,
    )


def collect_once(context: CaptureContext) -> CollectorSummary:
    """Run one explicit foreground reconciliation pass and return its counters."""
    with collector_lock(context.paths.state_dir):
        return collect_once_locked(context)


def status_report(context: CaptureContext) -> dict[str, JsonValue]:
    """Report actual source, cursor, gap, failure, and foreground-run state."""
    sources = list_sources(context)
    snapshot = read_collector_state(context)
    files: list[JsonValue] = []
    for item in snapshot.files:
        files.append({
            "source_id": item.source_id,
            "member_id": item.member_id,
            "relative_path": item.relative_path,
            "generation": item.generation,
            "cursor_byte": item.cursor,
            "file_size_bytes": item.size_bytes,
            "pending_bytes": item.pending_bytes,
            "status": item.status,
            "last_success_at": item.last_success_at,
            "last_error": item.last_error,
            "retry_after": item.retry_after,
        })
    source_rows: list[JsonValue] = []
    for source in sources:
        members = [item for item in snapshot.files if item.source_id == source.source_id]
        source_status = "disabled" if not source.enabled else "not_scanned"
        if source.enabled and members:
            source_status = "failed" if any(item.status == "failed" for item in members) else "active"
            if any(item.status == "missing" for item in members):
                source_status = "coverage_gap"
            if any(item.status in ("partial", "backpressure") for item in members):
                source_status = "pending"
        row = source_as_json(source)
        row["status"] = source_status
        row["last_success_at"] = max(
            (item.last_success_at for item in members if item.last_success_at is not None),
            default=None,
        )
        source_rows.append(row)
    gaps: list[JsonValue] = [{
        "source_id": item.source_id,
        "member_id": item.member_id,
        "relative_path": item.relative_path,
        "from_byte": item.from_byte,
        "to_byte": item.to_byte,
        "reason": item.reason,
        "observed_at": item.observed_at,
    } for item in snapshot.gaps]
    return {
        "collector": {
            "running": collector_is_running(context.paths.state_dir),
            "last_run_at": snapshot.last_run_at,
            "last_run_status": snapshot.last_run_status,
        },
        "sources": source_rows,
        "source_files": files,
        "coverage_gaps": gaps,
        "external_delivery": False,
    }


def require_verified_recovery(context: CaptureContext) -> RecoverySnapshot:
    """Expose checkpoint verification to status and doctor commands."""
    return verify_checkpoint_chain(context.config.vault)
