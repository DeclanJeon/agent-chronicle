"""Declarative parser surface for the local-only CLI."""

import argparse

from chronicle.cli_knowledge_parser import add_knowledge_parser
from chronicle.command_requests import InventoryKind, ProjectAliasAction, ServiceAction
from chronicle.event_classification import EVENT_TYPES
from chronicle.knowledge_models import KnowledgeType, RelationDecision, RelationType


class Arguments(argparse.Namespace):
    """Static view of argparse's dynamically populated namespace."""

    command: str | None = None
    knowledge_action: str | None = None
    relation_action: str | None = None
    relation_first_candidate_id: str | None = None
    relation_second_candidate_id: str | None = None
    relation_type: RelationType | None = None
    relation_id: str | None = None
    relation_decision: RelationDecision | None = None
    vault: str | None = None
    timezone: str | None = None
    file: str | None = None
    source_event_id: str | None = None
    session_id: str = "unknown"
    distill_session_id: str | None = None
    distill_preview: bool = False
    distill_save: bool = False
    distill_job_id: str | None = None
    title: str | None = None
    project_id: str | None = None
    source: str | None = None
    start_byte: int = 0
    end_byte: int | None = None
    dry_run: bool = False
    author: str | None = None
    occurred_at: str | None = None
    document_id: str | None = None
    final: bool = False
    event_type: str = "user_message"
    search_session_id: str | None = None
    search_event_type: str | None = None
    inventory_kind: InventoryKind | None = None
    inventory_session_id: str | None = None
    inventory_document_id: str | None = None
    classify_event_id: str | None = None
    classify_event_type: str | None = None
    privacy_action: str | None = None
    privacy_file: str | None = None
    storage_action: str | None = None
    storage_destination: str | None = None
    storage_dry_run: bool = False
    today: bool = False
    project_alias_action: ProjectAliasAction | None = None
    project_alias: str | None = None
    day: str | None = None
    date: str | None = None
    query: str | None = None
    source_action: str | None = None
    source_name: str | None = None
    source_file: str | None = None
    source_root: str | None = None
    source_id: str | None = None
    once: bool = False
    interval_seconds: float = 1.0
    event_id: str | None = None
    claim: str | None = None
    quote: str | None = None
    created_by: str | None = None
    knowledge_type: KnowledgeType = "lesson"
    candidate_id: str | None = None
    confirm_digest: str | None = None
    reviewer: str | None = None
    review_days: int = 30
    reason: str | None = None
    context_name: str = "personal"
    preview: bool = False
    max_characters: int = 8000
    save_context: bool = False
    saved_bundle_id: str | None = None
    review_due: bool = False
    weekly_review: bool = False
    review_limit: int = 25
    review_offset: int = 0
    service_action: ServiceAction | None = None
    service_preview: bool = False
    service_confirm: bool = False


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI surface without opening or creating user files."""
    parser = argparse.ArgumentParser(prog="chronicle", description="Local Markdown event journal and knowledge review")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="select and validate a single-host local vault")
    _ = init_parser.add_argument("--vault", help="vault directory; omitted in a terminal prompts once")
    _ = init_parser.add_argument(
        "--timezone", help="IANA timezone to pin; defaults to the system timezone"
    )

    capture = commands.add_parser("capture", help="explicitly capture one text file")
    _ = capture.add_argument("--file", required=True)
    _ = capture.add_argument("--source-event-id", required=True)
    _ = capture.add_argument("--session-id", default="unknown")
    _ = capture.add_argument("--title", required=True)
    _ = capture.add_argument("--project", dest="project_id", required=True)
    _ = capture.add_argument("--author", required=True)
    _ = capture.add_argument("--occurred-at", help="ISO-8601 timestamp with an explicit timezone")
    _ = capture.add_argument("--document-id", help="UUID of an existing document revision series")
    _ = capture.add_argument("--final", action="store_true", help="mark this revision as user-confirmed final")
    _ = capture.add_argument(
        "--event-type",
        choices=EVENT_TYPES,
        default="user_message",
    )

    import_parser = commands.add_parser("import", help="import an explicitly selected Pi JSONL v3 session")
    _ = import_parser.add_argument("--source", choices=("pi-jsonl-v3",), required=True)
    _ = import_parser.add_argument("--file", required=True)
    _ = import_parser.add_argument("--project", dest="project_id", required=True)
    _ = import_parser.add_argument("--title", default="Pi session import")
    _ = import_parser.add_argument("--dry-run", action="store_true")
    _ = import_parser.add_argument("--start-byte", type=int, default=0)
    _ = import_parser.add_argument("--end-byte", type=int)

    daily = commands.add_parser("daily", help="update one local daily note")
    _ = daily.add_argument("--date", dest="day", help="YYYY-MM-DD; default is local today")
    search = commands.add_parser("search", help="search committed Markdown records")
    _ = search.add_argument("--query", required=True)
    _ = search.add_argument("--date", help="filter by YYYY-MM-DD")
    _ = search.add_argument("--author")
    _ = search.add_argument("--project", dest="project_id")
    _ = search.add_argument("--session", dest="search_session_id")
    _ = search.add_argument(
        "--event-type", dest="search_event_type",
        choices=EVENT_TYPES,
    )

    classify = commands.add_parser("classify", help="append an event classification correction")
    _ = classify.add_argument("classify_event_id")
    _ = classify.add_argument("--project", dest="project_id", required=True)
    _ = classify.add_argument("--event-type", dest="classify_event_type", choices=EVENT_TYPES)
    _ = classify.add_argument("--reviewer", required=True)
    _ = classify.add_argument("--reason", required=True)
    open_parser = commands.add_parser("open", help="preview a daily-note path without launching an editor")
    _ = open_parser.add_argument("--today", action="store_true", required=True)
    _ = open_parser.add_argument("--preview", action="store_true", required=True)
    privacy = commands.add_parser("privacy", help="inspect selected text without returning contents")
    privacy_actions = privacy.add_subparsers(dest="privacy_action", required=True)
    privacy_inspect = privacy_actions.add_parser("inspect", help="scan one explicit local file")
    _ = privacy_inspect.add_argument("--file", dest="privacy_file", required=True)
    storage = commands.add_parser("storage", help="preview local storage changes")
    storage_actions = storage.add_subparsers(dest="storage_action", required=True)
    migrate = storage_actions.add_parser("migrate", help="validate a destination without copying")
    _ = migrate.add_argument("storage_destination")
    _ = migrate.add_argument("--dry-run", dest="storage_dry_run", action="store_true", required=True)
    _ = commands.add_parser("home", help="show metadata-only local vault summary")
    projects = commands.add_parser("projects", help="list project IDs from committed events")
    _ = projects.add_argument("--project", dest="project_id")
    sessions = commands.add_parser("sessions", help="list session IDs and event classifications")
    _ = sessions.add_argument("--project", dest="project_id")
    _ = sessions.add_argument("--session", dest="inventory_session_id")
    documents = commands.add_parser("documents", help="list immutable document revision series")
    _ = documents.add_argument("--project", dest="project_id")
    _ = documents.add_argument("--document-id", dest="inventory_document_id")
    project = commands.add_parser("project", help="manage stable project IDs and explicit aliases")
    project_actions = project.add_subparsers(dest="project_action", required=True)
    alias = project_actions.add_parser("alias", help="list or change project aliases")
    alias_actions = alias.add_subparsers(dest="project_alias_action", required=True)
    for alias_action in ("add", "remove"):
        alias_change = alias_actions.add_parser(alias_action)
        _ = alias_change.add_argument("--project-id", dest="project_id", required=True)
        _ = alias_change.add_argument("--alias", dest="project_alias", required=True)
    alias_list = alias_actions.add_parser("list")
    _ = alias_list.add_argument("--project-id", dest="project_id")

    add_knowledge_parser(commands.add_parser(
        "knowledge", help="review evidence-backed knowledge candidates",
    ))

    ask = commands.add_parser("ask", help="retrieval-only; generated answers are unsupported")
    _ = ask.add_argument("--query", required=True)
    _ = ask.add_argument("--project", dest="project_id")
    context = commands.add_parser("context", help="read-only local context preview")
    _ = context.add_argument("--query")
    _ = context.add_argument("--project", dest="project_id")
    _ = context.add_argument("--preview", action="store_true")
    _ = context.add_argument("--max-chars", type=int, default=8000)
    _ = context.add_argument("--save", dest="save_context", action="store_true")
    _ = context.add_argument("--saved-bundle", dest="saved_bundle_id")
    sources = commands.add_parser("sources", help="manage explicitly selected source scopes")
    source_actions = sources.add_subparsers(dest="source_action", required=True)
    _ = source_actions.add_parser("list", help="list registered sources; all are opt-in")
    enable = source_actions.add_parser("enable", help="enable one explicit Pi v3 file or directory root")
    _ = enable.add_argument("--source", dest="source_name", choices=("pi-jsonl-v3",), required=True)
    scope = enable.add_mutually_exclusive_group(required=True)
    _ = scope.add_argument("--file", dest="source_file")
    _ = scope.add_argument("--root", dest="source_root")
    _ = enable.add_argument("--project", dest="project_id", required=True)
    _ = enable.add_argument("--title", default="Pi session")
    disable = source_actions.add_parser("disable", help="stop reading a registered source")
    _ = disable.add_argument("source_id")

    collect = commands.add_parser("collect", help="run foreground source reconciliation")
    _ = collect.add_argument("--once", action="store_true", help="run one deterministic polling pass")
    _ = collect.add_argument("--interval-seconds", type=float, default=1.0)
    _ = commands.add_parser("status", help="show actual source cursors, gaps, and collector state")
    _ = commands.add_parser("doctor", help="run read-only local runtime checks")
    service = commands.add_parser("service", help="plan or explicitly manage a systemd user service")
    _ = service.add_argument("service_action", choices=("plan", "install", "status", "restart", "stop", "remove"))
    _ = service.add_argument("--preview", dest="service_preview", action="store_true")
    _ = service.add_argument("--confirm", dest="service_confirm", action="store_true")
    review = commands.add_parser("review", help="show or refresh local review backlog")
    review_mode = review.add_mutually_exclusive_group(required=True)
    _ = review_mode.add_argument("--due", dest="review_due", action="store_true")
    _ = review_mode.add_argument("--weekly", dest="weekly_review", action="store_true")
    _ = review.add_argument("--limit", type=int, default=25)
    _ = review.add_argument("--offset", type=int, default=0)
    _ = commands.add_parser("rebuild", help="rebuild SQLite from verified Markdown checkpoints")
    _ = commands.add_parser("daemon", help="run the foreground source collector until SIGTERM")
    return parser
