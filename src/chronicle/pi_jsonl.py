"""Read-only parser for the installed Pi 1.5 session JSONL v3 format."""

import hashlib
import json
from pathlib import Path
from typing import cast

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.naming import safe_slug
from chronicle.pi_jsonl_content import mapping, parse_message_entry, required_text
from chronicle.pi_jsonl_models import PiRecord, PiSession
from chronicle.privacy import reject_sensitive_metadata

_MAX_SESSION_BYTES = 128 * 1024 * 1024


class _DuplicateKey(ValueError):
    """Signal ambiguous JSONL objects without exposing their source text."""


def _object_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(token: str) -> JsonValue:
    raise ValueError(token)


def _decode_line(raw: bytes) -> dict[str, JsonValue]:
    if not raw.endswith(b"\n"):
        raise ChronicleError("incomplete_record", "session record is not LF-terminated")
    body = raw[:-1]
    if body.endswith(b"\r"):
        body = body[:-1]
    try:
        decoded = cast(JsonValue, json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        ))
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, RecursionError, ValueError) as error:
        raise ChronicleError("invalid_pi_session", "session JSONL record is invalid") from error
    result = mapping(decoded)
    if result is None:
        raise ChronicleError("invalid_pi_session", "session JSONL record must be an object")
    return result


def parse_pi_jsonl_header(raw_line: bytes) -> tuple[int, str]:
    """Parse the first complete Pi session header and pin its supported version."""
    row = _decode_line(raw_line)
    if row.get("type") != "session":
        raise ChronicleError("invalid_pi_session", "session header must be the first JSONL record")
    version = row.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 3:
        raise ChronicleError("unsupported_source_version", "only Pi session JSONL version 3 is supported")
    session_id = required_text(row, "id")
    reject_sensitive_metadata(session_id)
    return version, session_id


def parse_pi_jsonl_message(raw_line: bytes, start: int, end: int) -> PiRecord | None:
    """Parse one complete streaming record using the pinned v3 message contract."""
    return parse_message_entry(_decode_line(raw_line), start, end)


def parse_pi_jsonl(path: Path, *, start_byte: int = 0, end_byte: int | None = None) -> PiSession:
    """Parse one explicit Pi v3 file and import only complete, byte-aligned records."""
    if path.is_symlink():
        raise ChronicleError("unsafe_input", "session input may not be a symbolic link")
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ChronicleError("invalid_input", "session input must be a regular file")
        size = resolved.stat().st_size
        if size > _MAX_SESSION_BYTES:
            raise ChronicleError("input_too_large", "session JSONL exceeds the 128 MiB import limit")
        raw = resolved.read_bytes()
    except ChronicleError:
        raise
    except OSError as error:
        raise ChronicleError("input_error", error.strerror or "session input cannot be read") from error

    offset = 0
    complete_end = 0
    header_end_byte = 0
    header: dict[str, JsonValue] | None = None
    records: list[PiRecord] = []
    seen_ids: set[str] = set()
    boundaries = {0}
    total_entries = 0
    skipped_entries = 0
    pending_bytes = 0
    while offset < len(raw):
        line_end = raw.find(b"\n", offset)
        if line_end < 0:
            pending_bytes = len(raw) - offset
            break
        record_start = offset
        record_end = line_end + 1
        row = _decode_line(raw[record_start:record_end])
        offset = record_end
        complete_end = record_end
        boundaries.add(record_end)
        if header is None:
            if row.get("type") != "session":
                raise ChronicleError("invalid_pi_session", "session header must be the first JSONL record")
            version = row.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version != 3:
                raise ChronicleError("unsupported_source_version", "only Pi session JSONL version 3 is supported")
            session_id = required_text(row, "id")
            reject_sensitive_metadata(session_id)
            header = row
            header_end_byte = record_end
            continue
        total_entries += 1
        parsed = parse_message_entry(row, record_start, record_end)
        if parsed is None:
            skipped_entries += 1
            continue
        if parsed.source_event_id in seen_ids:
            raise ChronicleError("invalid_pi_session", "session contains duplicate message entry IDs")
        seen_ids.add(parsed.source_event_id)
        records.append(parsed)

    if header is None:
        raise ChronicleError("invalid_pi_session", "complete versioned session header is required")
    session_id = required_text(header, "id")
    boundaries.add(header_end_byte)
    selected_end = complete_end if end_byte is None else end_byte
    if (
        isinstance(start_byte, bool) or isinstance(selected_end, bool)
        or start_byte < 0 or selected_end < start_byte or selected_end > complete_end
        or start_byte not in boundaries or selected_end not in boundaries
    ):
        raise ChronicleError("invalid_import_range", "import byte range must align to complete record boundaries")
    selected = tuple(
        item for item in records
        if item.record_start_byte >= start_byte and item.record_end_byte_exclusive <= selected_end
    )
    file_name = safe_slug(resolved.name, 120)
    reject_sensitive_metadata(file_name)
    stable_id = hashlib.sha256(f"pi-jsonl-v3\0{session_id}".encode("utf-8")).hexdigest()
    stream_id = f"pi-jsonl-v3:{stable_id}"
    return PiSession(
        3,
        session_id,
        stable_id,
        file_name,
        stream_id,
        selected,
        total_entries,
        skipped_entries,
        sum(item.omitted_content_blocks for item in selected),
        len(raw),
        complete_end,
        pending_bytes,
        start_byte,
        selected_end,
    )
