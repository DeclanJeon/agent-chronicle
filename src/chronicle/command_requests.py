"""Typed, parsed CLI command variants."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, TypeAlias

from chronicle.knowledge_models import (
    ApprovalInput, CandidateInput, KnowledgeSearchRequest, RelationInput,
    RelationReviewInput, RejectionInput, RetractionInput,
)
from chronicle.models import CaptureOptions
from chronicle.search import SearchRequest


@dataclass(frozen=True, slots=True)
class ClassifyCommand:
    """Append an explicit event project/type classification correction."""

    event_id: str
    project_id: str
    event_type: str | None
    reviewer: str
    reason: str


@dataclass(frozen=True, slots=True)
class OpenTodayCommand:
    """Preview today's daily-note path without launching an external editor."""

    preview: bool


@dataclass(frozen=True, slots=True)
class PrivacyInspectCommand:
    """Inspect one explicitly selected local text file without returning its contents."""

    file_path: Path


@dataclass(frozen=True, slots=True)
class StorageMigrateCommand:
    """Preview a local destination change without copying or modifying data."""

    destination: Path
    dry_run: bool


@dataclass(frozen=True, slots=True)
class InitCommand:
    """Initialize one selected local vault and fixed calendar timezone."""

    vault: Path | None
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    """Capture one explicitly selected local input file."""

    options: CaptureOptions


@dataclass(frozen=True, slots=True)
class ImportCommand:
    """Import a selected, explicitly chosen Pi session export."""

    file_path: Path
    source: str
    project_id: str
    title: str
    dry_run: bool
    start_byte: int
    end_byte: int | None


@dataclass(frozen=True, slots=True)
class DailyCommand:
    """Refresh a date-specific daily note."""

    day: date | None


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """Search committed records with structured filters."""

    request: SearchRequest


@dataclass(frozen=True, slots=True)
class ProposeCommand:
    """Create one exact-evidence candidate."""

    request: CandidateInput


@dataclass(frozen=True, slots=True)
class PreviewCommand:
    """Show a candidate snapshot and digest without changing it."""

    candidate_id: str


@dataclass(frozen=True, slots=True)
class ApproveCommand:
    """Approve only the exact snapshot previewed by the user."""

    request: ApprovalInput


@dataclass(frozen=True, slots=True)
class RejectCommand:
    """Reject one candidate before approval with a durable reason."""

    request: RejectionInput


@dataclass(frozen=True, slots=True)
class RetractCommand:
    """Retract one candidate from future reuse."""

    request: RetractionInput


@dataclass(frozen=True, slots=True)
class KnowledgeSearchCommand:
    """Search approved current knowledge only."""

    request: KnowledgeSearchRequest


ServiceAction = Literal["plan", "install", "status", "restart", "stop", "remove"]


InventoryKind = Literal["home", "projects", "sessions", "documents"]
ProjectAliasAction = Literal["add", "remove", "list"]


@dataclass(frozen=True, slots=True)
class ProjectAliasCommand:
    """List or append an explicit project alias event."""

    action: ProjectAliasAction
    project_id: str | None = None
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryCommand:
    """Read metadata-only project, session, document, or home inventory."""

    kind: InventoryKind
    project_id: str | None = None
    session_id: str | None = None
    document_id: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceCommand:
    """Plan or explicitly manage one local systemd user service."""

    action: ServiceAction
    preview: bool = False
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class RelateCommand:
    """Create an explicit, unapproved relationship proposal."""

    request: RelationInput


@dataclass(frozen=True, slots=True)
class RelationReviewCommand:
    """Confirm or dismiss one relation proposal without mutating candidates."""

    request: RelationReviewInput


@dataclass(frozen=True, slots=True)
class RelationListCommand:
    """List relationship proposals and their current review state."""

    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class DistillCommand:
    """Preview or persist rule-based claims from one selected session."""

    session_id: str
    preview: bool
    persist: bool = False


@dataclass(frozen=True, slots=True)
class RetryDistillCommand:
    """Resume one durable pending or failed local distillation job."""

    job_id: str


@dataclass(frozen=True, slots=True)
class DistillJobsCommand:
    """List durable local knowledge extraction job states."""


@dataclass(frozen=True, slots=True)
class WeeklyReviewCommand:
    """Create or refresh the current local Markdown review backlog."""


@dataclass(frozen=True, slots=True)
class ReviewDueCommand:
    """List local evidence, conflict, and review-due items without mutation."""

    limit: int = 25
    offset: int = 0


@dataclass(frozen=True, slots=True)
class AskCommand:
    """Run retrieval-only ask mode."""

    request: KnowledgeSearchRequest


@dataclass(frozen=True, slots=True)
class ContextCommand:
    """Build or revalidate a bounded, read-only local context bundle."""

    request: KnowledgeSearchRequest
    preview: bool
    max_characters: int = 8000
    save: bool = False
    saved_bundle_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourcesListCommand:
    """List registered local source scopes without enabling anything."""


@dataclass(frozen=True, slots=True)
class SourceEnableCommand:
    """Explicitly enable one version-pinned local source scope."""

    source: str
    selected_path: Path
    scope_kind: str
    project_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SourceDisableCommand:
    """Disable future reads for one registered source without deleting history."""

    source_id: str


@dataclass(frozen=True, slots=True)
class CollectCommand:
    """Run one collection pass or a bounded-interval foreground loop."""

    once: bool
    interval_seconds: float


@dataclass(frozen=True, slots=True)
class StatusCommand:
    """Read current collector, source, gap, and resume status."""


@dataclass(frozen=True, slots=True)
class DoctorCommand:
    """Run read-only checks against the configured local runtime."""


@dataclass(frozen=True, slots=True)
class RebuildCommand:
    """Rebuild the SQLite projection from committed Markdown."""

    pass


@dataclass(frozen=True, slots=True)
class DaemonCommand:
    """Run the foreground lock holder until SIGTERM."""

    pass


CommandRequest: TypeAlias = (
    ClassifyCommand | OpenTodayCommand | PrivacyInspectCommand | StorageMigrateCommand
    | InitCommand | CaptureCommand | ImportCommand | DailyCommand | SearchCommand | ProposeCommand
    | PreviewCommand | ApproveCommand | RejectCommand | RetractCommand | KnowledgeSearchCommand
    | ReviewDueCommand | WeeklyReviewCommand | DistillCommand
    | RetryDistillCommand | DistillJobsCommand | RelateCommand | RelationReviewCommand | RelationListCommand
    | ServiceCommand | InventoryCommand | ProjectAliasCommand
    | AskCommand | ContextCommand | SourcesListCommand | SourceEnableCommand
    | SourceDisableCommand | CollectCommand | StatusCommand | DoctorCommand
    | RebuildCommand | DaemonCommand
)
