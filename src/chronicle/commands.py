"""Typed user-visible command execution with local-only side effects."""

from datetime import datetime
from dataclasses import replace
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from chronicle.capture_input import load_capture_input
from chronicle.command_auxiliary import AuxiliaryCommand, execute_auxiliary
from chronicle.command_collector import CollectorCommand, execute_collector
from chronicle.command_runtime import json_output as _json, load_context as _context
from chronicle.command_requests import (
    CaptureCommand,
    CommandRequest,
    DailyCommand,
    InitCommand,
    ImportCommand,
    SearchCommand,
)
from chronicle.daily import update_daily
from chronicle.destination import initialize_vault, validate_destination
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.journal import capture_event
from chronicle.knowledge_commands import execute_knowledge
from chronicle.locking import writer_lock
from chronicle.pi_import import import_pi_session
from chronicle.project_aliases import resolve_project_id
from chronicle.paths import environment_paths, load_or_create_config, validate_store_layout, validate_store_roots
from chronicle.schema import text_field
from chronicle.search import search_documents


def _init(request: InitCommand) -> int:
    """Initialize one local vault, asking only for its storage path if needed."""
    vault_argument = request.vault
    if vault_argument is None:
        if not sys.stdin.isatty():
            raise ChronicleError("invalid_input", "non-interactive init requires --vault PATH")
        response = input("Vault path [Enter for ~/Documents/Obsidian/AgentHistory]: ").strip()
        vault_argument = Path(response or "~/Documents/Obsidian/AgentHistory")
    paths = environment_paths()
    validate_store_roots(paths)
    vault = validate_destination(vault_argument)
    validate_store_layout(vault, paths)
    config = load_or_create_config(vault, paths, request.timezone)
    _ = initialize_vault(vault, config.host_id, config.source_instance_id)
    _json({
        "status": "initialized",
        "vault": str(vault),
        "writer": "single-host-local",
        "timezone": config.timezone,
    })
    return 0


def _capture(command: CaptureCommand) -> int:
    """Commit explicit input and update its event-date daily note."""
    context = _context()
    project_id = resolve_project_id(context.config.vault, command.options.project_id)
    if project_id is None:
        raise ChronicleError("invalid_input", "capture requires a project ID")
    parsed = load_capture_input(replace(command.options, project_id=project_id), context)
    result = capture_event(context, parsed)
    event_metadata, _event_content = read_capture(result.event_path)
    day = datetime.fromisoformat(text_field(event_metadata, "occurred_at")).astimezone(
        ZoneInfo(context.config.timezone)
    ).date()
    with writer_lock(context.config.vault):
        daily = update_daily(
            context.config.vault,
            day,
            timezone_name=context.config.timezone,
        )
    _json({
        "status": "committed",
        "event_id": result.event_id,
        "revision_id": result.revision_id,
        "checkpoint_id": result.checkpoint_id,
        "replayed": result.replayed,
        "daily_event_count": daily.event_count,
        "daily_conflict_copy": daily.conflict_copy.name if daily.conflict_copy else None,
        "daily_revision_id": daily.revision_id,
        "daily_previous_revision_id": daily.previous_revision_id,
    })
    return 0


def _import(command: ImportCommand) -> int:
    """Preview or import a bounded range from one explicitly selected Pi session."""
    _json(import_pi_session(_context(), command))
    return 0


def _daily(command: DailyCommand) -> int:
    """Refresh one daily note without overwriting edits to generated text."""
    context = _context()
    with writer_lock(context.config.vault):
        result = update_daily(
            context.config.vault,
            command.day,
            timezone_name=context.config.timezone,
        )
    _json({
        "status": "conflict" if result.conflict_copy else "updated",
        "event_count": result.event_count,
        "conflict_copy": result.conflict_copy.name if result.conflict_copy else None,
        "revision_id": result.revision_id,
        "previous_revision_id": result.previous_revision_id,
    })
    return 0


def _search(command: SearchCommand) -> int:
    """Search committed event and revision Markdown without model calls."""
    context = _context()
    request = replace(
        command.request,
        project_id=resolve_project_id(context.config.vault, command.request.project_id),
    )
    hits = search_documents(
        context.config.vault,
        request,
        context.config.timezone,
    )
    _json([{
        "kind": hit.kind,
        "title": hit.title,
        "author": hit.author_name,
        "project": hit.project_id,
        "occurred_at": hit.occurred_at,
        "event_id": hit.event_id,
        "revision_id": hit.revision_id,
        "markdown_path": hit.path.relative_to(context.config.vault).as_posix(),
        "document_id": hit.document_id,
        "document_revision": hit.document_revision,
        "session_id": hit.session_id,
        "event_type": hit.event_type,
        "excerpt": hit.excerpt,
    } for hit in hits])
    return 0


def execute(command: CommandRequest) -> int:
    """Dispatch typed requests to local, collector, and knowledge handlers."""
    if isinstance(command, AuxiliaryCommand):
        return execute_auxiliary(command)
    if isinstance(command, CollectorCommand):
        return execute_collector(command)
    if isinstance(command, InitCommand):
        return _init(command)
    if isinstance(command, CaptureCommand):
        return _capture(command)
    if isinstance(command, ImportCommand):
        return _import(command)
    if isinstance(command, DailyCommand):
        return _daily(command)
    if isinstance(command, SearchCommand):
        return _search(command)
    _json(execute_knowledge(_context(), command))
    return 0
