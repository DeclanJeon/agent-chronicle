"""Immutable source values for Pi JSONL v3 import."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PiRecord:
    """One source-message entry and its exact byte range in the session file."""

    source_event_id: str
    parent_event_id: str | None
    source_call_id: str | None
    event_type: str
    actor_id: str
    occurred_at: str
    content: str
    record_start_byte: int
    record_end_byte_exclusive: int
    omitted_content_blocks: int


@dataclass(frozen=True, slots=True)
class PiSession:
    """Selected, complete message records from one version-pinned Pi session."""

    version: int
    session_id: str
    source_file_id: str
    source_file_name: str
    stream_id: str
    records: tuple[PiRecord, ...]
    total_entries: int
    skipped_entries: int
    omitted_content_blocks: int
    source_bytes: int
    complete_end_byte: int
    pending_bytes: int
    selected_start_byte: int
    selected_end_byte: int
