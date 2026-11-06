"""Immutable values crossing Chronicle's module boundaries."""

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class EnvironmentPaths:
    """Configuration, state, and cache roots for one local installation."""

    config_dir: Path
    state_dir: Path
    cache_dir: Path


@dataclass(frozen=True, slots=True)
class ChronicleConfig:
    """Stable IDs and selected vault for a local installation."""

    vault: Path
    host_id: str
    source_instance_id: str
    timezone: str


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Verified installation configuration and runtime-local roots."""

    config: ChronicleConfig
    paths: EnvironmentPaths


@dataclass(frozen=True, slots=True)
class DocumentPlan:
    """Immutable revision identity reserved under the writer lock."""

    document_id: str
    revision_id: str
    revision_number: int
    previous_revision_id: str | None


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    """Raw, explicit CLI choices for one manual capture."""

    file_path: Path
    source_event_id: str
    source_session_id: str
    title: str
    project_id: str
    author_name: str
    occurred_at: str | None
    document_id: str | None
    final: bool
    event_type: str = "user_message"


@dataclass(frozen=True, slots=True)
class CapturedInput:
    """Parsed explicit capture request."""

    content: str
    source_event_id: str
    source_session_id: str
    title: str
    project_id: str
    author_name: str
    occurred_at: str
    time_basis: str
    source_file_name: str
    source_file_id: str
    document_id: str | None
    final: bool
    event_type: str = "user_message"
    source_adapter_id: str = "manual-file"
    source_adapter_version: str = "manual-file-v1"
    source_stream_id: str | None = None
    record_start_byte: int = 0
    record_end_byte_exclusive: int | None = None
    record_part_index: int = 0
    record_part_count: int = 1
    rotation_generation: str = "explicit-one-shot"
    non_resumable_reason: str | None = "explicit manual capture is not a tailing adapter"
    source_parent_event_id: str | None = None
    source_call_id: str | None = None
    source_resume_token: str | None = None


@dataclass(frozen=True, slots=True)
class JournalCommit:
    """Outcome after an event and its checkpoint are durably published."""

    event_id: str
    revision_id: str
    event_path: Path
    revision_path: Path
    checkpoint_id: str
    replayed: bool
