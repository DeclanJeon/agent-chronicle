"""Immutable source registry and foreground collector values."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chronicle.models import JsonValue


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    """One explicitly enabled source scope and pinned parser policy."""

    source_id: str
    adapter_id: str
    adapter_version: str
    policy_version: str
    scope_kind: Literal["file", "root"]
    scope_path: Path
    project_id: str
    title: str
    enabled: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """One known or suspected interval that was not captured completely."""

    source_id: str
    member_id: str
    relative_path: str
    from_byte: int
    to_byte: int | None
    reason: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class SourceFileState:
    """Durable operational cursor for one source file identity and generation."""

    source_id: str
    member_id: str
    relative_path: str
    file_id: str
    device: int
    inode: int
    generation: int
    session_id: str
    cursor: int
    prefix_sha256: str
    size_bytes: int
    pending_bytes: int
    status: str
    last_success_at: str | None
    last_error: str | None
    consecutive_failures: int
    retry_after: str | None


@dataclass(frozen=True, slots=True)
class FileCollectionOutcome:
    """One file scan result and its durable source effects."""

    state: SourceFileState
    gaps: tuple[CoverageGap, ...]
    new_events: int
    replayed_events: int
    pending: bool
    failed: bool


@dataclass(frozen=True, slots=True)
class CollectorSnapshot:
    """Persisted source cursors, coverage gaps, and last collection result."""

    updated_at: str | None
    last_run_at: str | None
    last_run_status: str
    files: tuple[SourceFileState, ...]
    gaps: tuple[CoverageGap, ...]


@dataclass(frozen=True, slots=True)
class CollectorSummary:
    """Counters and status returned by one foreground reconciliation pass."""

    status: str
    sources_seen: int
    files_seen: int
    new_events: int
    replayed_events: int
    pending_files: int
    failed_files: int
    gaps_added: int

    def as_json(self) -> dict[str, JsonValue]:
        """Return the stable user-facing JSON representation."""
        return {
            "status": self.status,
            "sources_seen": self.sources_seen,
            "files_seen": self.files_seen,
            "new_events": self.new_events,
            "replayed_events": self.replayed_events,
            "pending_files": self.pending_files,
            "failed_files": self.failed_files,
            "gaps_added": self.gaps_added,
        }
