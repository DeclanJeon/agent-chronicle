"""Process one registered Pi file with bounded byte cursors and replay."""

import hashlib
import os
from pathlib import Path
import stat

from chronicle.checkpoints import RecoverySnapshot
from chronicle.errors import ChronicleError
from chronicle.pi_jsonl import parse_pi_jsonl_header, parse_pi_jsonl_message
from chronicle.source_models import CoverageGap, FileCollectionOutcome, SourceFileState, SourceRegistration
from chronicle.models import CaptureContext

from chronicle.collector_capture import base_state, capture_record, check_retry, failed_outcome, new_gap
from chronicle.collector_files import (
    MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_RECORDS_PER_FILE_PASS,
    now, prefix_digest, relative_source_path, source_member_id,
)
from chronicle.collector_tokens import resume_after_state_loss


def _failed_with_gaps(
    source: SourceRegistration,
    path: Path,
    prior: SourceFileState | None,
    error: ChronicleError,
    gaps: list[CoverageGap],
) -> FileCollectionOutcome:
    """Retain discovered coverage gaps alongside a per-file failure."""
    outcome = failed_outcome(source, path, prior, error)
    return FileCollectionOutcome(
        outcome.state, tuple(gaps), outcome.new_events, outcome.replayed_events,
        outcome.pending, outcome.failed,
    )


def collect_source_file(
    context: CaptureContext,
    source: SourceRegistration,
    path: Path,
    previous: SourceFileState | None,
    recovery: RecoverySnapshot,
) -> FileCollectionOutcome:
    """Read complete records from one explicitly selected source file."""
    prior = previous
    gaps: list[CoverageGap] = []
    try:
        relative = relative_source_path(source, path)
        if path.is_symlink() or path.resolve(strict=True) != path.absolute():
            raise ChronicleError("unsafe_input", "source file may not traverse symbolic links")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ChronicleError("unsafe_input", "source entry must be a regular file")
            if file_stat.st_size > MAX_FILE_BYTES:
                raise ChronicleError("input_too_large", "Pi session file exceeds the 128 MiB collector limit")
            device, inode, size = file_stat.st_dev, file_stat.st_ino, file_stat.st_size
            member_id = source_member_id(source.source_id, device, inode)
            prior = previous if previous is not None and previous.member_id == member_id else None
            if (
                prior is not None and check_retry(prior)
                and prior.relative_path == relative and prior.size_bytes == size
                and prior.cursor <= size
                and (prior.cursor == 0 or prefix_digest(stream, prior.cursor) == prior.prefix_sha256)
            ):
                return FileCollectionOutcome(prior, (), 0, 0, False, True)
            if prior is None:
                generation, recovered_gap = resume_after_state_loss(
                    stream, size, source, member_id, device, inode, recovery, relative,
                )
                if recovered_gap is not None:
                    gaps.append(recovered_gap)
                state = base_state(source, member_id, relative, device, inode, generation, size)
            else:
                generation = prior.generation
                state = SourceFileState(
                    source.source_id, member_id, relative, prior.file_id, device, inode,
                    generation, prior.session_id, prior.cursor, prior.prefix_sha256,
                    size, 0, prior.status, prior.last_success_at, prior.last_error,
                    prior.consecutive_failures, prior.retry_after,
                )
                if state.cursor > size:
                    gaps.append(new_gap(source, member_id, relative, state.cursor, size, "file_truncated"))
                    generation += 1
                    state = base_state(source, member_id, relative, device, inode, generation, size)
                elif state.cursor and prefix_digest(stream, state.cursor) != state.prefix_sha256:
                    gaps.append(new_gap(source, member_id, relative, state.cursor, size, "committed_prefix_changed"))
                    generation += 1
                    state = base_state(source, member_id, relative, device, inode, generation, size)
            prior = state
            _ = stream.seek(0)
            prefix = hashlib.sha256()
            cursor = 0
            session_id = state.session_id
            if session_id:
                header = stream.readline(MAX_LINE_BYTES + 1)
                version, actual_session = parse_pi_jsonl_header(header)
                if version != 3 or actual_session != session_id:
                    raise ChronicleError("source_changed", "Pi session identity changed before the committed cursor")
                prefix.update(header)
                cursor = len(header)
            else:
                header = stream.readline(MAX_LINE_BYTES + 1)
                if not header:
                    raise ChronicleError("invalid_pi_session", "session header is missing")
                if len(header) > MAX_LINE_BYTES:
                    raise ChronicleError("record_too_large", "Pi session header exceeds the line limit")
                if not header.endswith(b"\n"):
                    return FileCollectionOutcome(
                        SourceFileState(
                            state.source_id, state.member_id, relative, state.file_id,
                            device, inode, state.generation, "", 0,
                            hashlib.sha256(b"").hexdigest(), size, len(header), "partial",
                            state.last_success_at, None, 0, None,
                        ), tuple(gaps), 0, 0, True, False,
                    )
                _version, session_id = parse_pi_jsonl_header(header)
                prefix.update(header)
                cursor = len(header)
                state = SourceFileState(
                    state.source_id, state.member_id, relative, state.file_id,
                    device, inode, state.generation, session_id, cursor,
                    prefix.hexdigest(), size, 0, "ready", state.last_success_at,
                    None, 0, None,
                )
                prior = state
            if state.cursor > cursor and prefix_digest(stream, state.cursor) == state.prefix_sha256:
                cursor = state.cursor
                prefix = hashlib.sha256()
                _ = stream.seek(0)
                remaining = cursor
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ChronicleError("source_changed", "source became shorter during collection")
                    prefix.update(block)
                    remaining -= len(block)
            _ = stream.seek(cursor)
            pending_bytes = 0
            new_events = 0
            replayed_events = 0
            processed = 0
            while processed < MAX_RECORDS_PER_FILE_PASS:
                start = cursor
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    raise ChronicleError("record_too_large", "Pi session record exceeds the line limit")
                if not raw.endswith(b"\n"):
                    pending_bytes = len(raw)
                    break
                end = start + len(raw)
                record = parse_pi_jsonl_message(raw, start, end)
                prefix.update(raw)
                cursor = end
                processed += 1
                if record is not None:
                    replayed = capture_record(
                        context, source,
                        SourceFileState(
                            state.source_id, state.member_id, relative, state.file_id,
                            device, inode, state.generation, session_id, start,
                            state.prefix_sha256, size, 0, "ready", state.last_success_at,
                            None, 0, None,
                        ),
                        record, prefix.hexdigest(),
                    )
                    replayed_events += int(replayed)
                    new_events += int(not replayed)
            current_size = os.fstat(stream.fileno()).st_size
            if prefix_digest(stream, cursor) != prefix.hexdigest():
                raise ChronicleError("source_changed", "source prefix changed during collection")
            stream_position = stream.tell()
            backpressure = stream_position < size or current_size > size
            status = "partial" if pending_bytes else ("backpressure" if backpressure else "ready")
            now_text = now().isoformat(timespec="seconds")
            finished = SourceFileState(
                source.source_id, member_id, relative, state.file_id, device, inode,
                state.generation, session_id, cursor, prefix.hexdigest(), current_size,
                pending_bytes, status, now_text, None, 0, None,
            )
            return FileCollectionOutcome(
                finished, tuple(gaps), new_events, replayed_events,
                bool(pending_bytes or backpressure), False,
            )
    except ChronicleError as error:
        return _failed_with_gaps(source, path, prior, error, gaps)
    except OSError as error:
        wrapped = ChronicleError("source_io_error", error.strerror or "source file could not be read")
        return _failed_with_gaps(source, path, prior, wrapped, gaps)
