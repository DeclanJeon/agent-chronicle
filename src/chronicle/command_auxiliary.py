"""Local-only inventory, retrieval, privacy, and classification commands."""

from dataclasses import replace
from datetime import datetime
from typing import TypeAlias
from zoneinfo import ZoneInfo

from chronicle.command_requests import (
    AskCommand,
    ClassifyCommand,
    ContextCommand,
    InventoryCommand,
    OpenTodayCommand,
    PrivacyInspectCommand,
    ProjectAliasCommand,
    ServiceCommand,
    StorageMigrateCommand,
    WeeklyReviewCommand,
)
from chronicle.command_runtime import json_output, load_context
from chronicle.context_preview import check_saved_context_bundle, render_context_preview
from chronicle.destination import validate_destination
from chronicle.errors import ChronicleError
from chronicle.event_classification import classify_event
from chronicle.inventory import inventory_report
from chronicle.knowledge import search_knowledge
from chronicle.locking import writer_lock
from chronicle.models import JsonValue
from chronicle.privacy_inspect import inspect_text_file
from chronicle.project_aliases import change_project_alias, project_aliases, resolve_project_id
from chronicle.service_manager import manage_service
from chronicle.weekly_review import publish_weekly_review

AuxiliaryCommand: TypeAlias = (
    AskCommand | ClassifyCommand | ContextCommand | InventoryCommand | OpenTodayCommand
    | PrivacyInspectCommand | ProjectAliasCommand | ServiceCommand | StorageMigrateCommand
    | WeeklyReviewCommand
)


def _weekly_review() -> int:
    """Refresh the current local Markdown review snapshot without recording review completion."""
    context = load_context()
    today = datetime.now(ZoneInfo(context.config.timezone)).date()
    with writer_lock(context.config.vault):
        result = publish_weekly_review(context.config.vault, today)
    json_output({
        "status": result.status,
        "week_file": result.path.name,
        "item_count": result.item_count,
        "conflict_copy": result.conflict_copy.name if result.conflict_copy else None,
        "caught_up": result.caught_up,
        "reviewed": False,
        "external_delivery": False,
    })
    return 0


def _service(command: ServiceCommand) -> int:
    """Plan or manage the explicitly requested local systemd service."""
    json_output(manage_service(command.action, command.preview, command.confirmed))
    return 0


def _inventory(command: InventoryCommand) -> int:
    """Show metadata-only local IDs and revision counts from committed records."""
    context = load_context()
    project_id = resolve_project_id(context.config.vault, command.project_id)
    json_output(inventory_report(
        context.config.vault, command.kind, project_id,
        command.session_id, command.document_id,
    ))
    return 0


def _ask(command: AskCommand) -> int:
    """Return retrieval-only evidence and explicitly refuse generated answers."""
    context = load_context()
    request = replace(
        command.request,
        project_id=resolve_project_id(context.config.vault, command.request.project_id),
    )
    results = search_knowledge(context.config.vault, request)
    json_output({
        "mode": "unsupported_answer_mode",
        "generated_answer": None,
        "results": [{
            "title": item.title,
            "exact_quote": item.quote,
            "source_event_id": item.source_event_id,
            "project_id": item.project_id,
        } for item in results],
    })
    return 0


def _context_preview(command: ContextCommand) -> int:
    """Render or revalidate a bounded local context bundle; never export it."""
    context = load_context()
    if command.saved_bundle_id is not None:
        json_output(check_saved_context_bundle(context, command.saved_bundle_id))
        return 0
    if not command.preview:
        raise ChronicleError("unsupported_export", "only --preview is supported; external export is disabled")
    request = replace(
        command.request,
        project_id=resolve_project_id(context.config.vault, command.request.project_id),
    )
    json_output(render_context_preview(context, request, command.max_characters, command.save))
    return 0


def _classify(command: ClassifyCommand) -> int:
    """Append a classification event and leave the committed source untouched."""
    context = load_context()
    stable_project_id = resolve_project_id(context.config.vault, command.project_id)
    if stable_project_id is None:
        raise ChronicleError("invalid_input", "classification requires a project ID")
    json_output(classify_event(
        context, command.event_id, stable_project_id, command.event_type,
        command.reviewer, command.reason,
    ))
    return 0


def _open_today(command: OpenTodayCommand) -> int:
    """Return today's local note path and never launch an external editor."""
    if not command.preview:
        raise ChronicleError("unsupported_open", "only the --preview path is supported")
    context = load_context()
    day = datetime.now(ZoneInfo(context.config.timezone)).date()
    path = context.config.vault / "50_Daily" / day.strftime("%Y/%m") / f"{day.isoformat()}.md"
    if path.is_symlink():
        raise ChronicleError("unsafe_path", "today's daily note is a symbolic link")
    json_output({"status": "preview_only", "daily_note_path": str(path), "editor_launched": False})
    return 0


def _privacy_inspect(command: PrivacyInspectCommand) -> int:
    """Report only privacy-pattern counts for one selected text file."""
    json_output(inspect_text_file(load_context(), command.file_path))
    return 0


def _storage_migrate(command: StorageMigrateCommand) -> int:
    """Validate a same-host destination without copying or mutating data."""
    if not command.dry_run:
        raise ChronicleError("migration_apply_unsupported", "storage migration supports only --dry-run")
    context = load_context()
    destination = validate_destination(command.destination)
    source = context.config.vault
    if destination == source or destination in source.parents or source in destination.parents:
        raise ChronicleError("invalid_destination", "migration source and destination may not overlap")
    json_output({
        "status": "migration_preview_only",
        "source": str(source),
        "destination": str(destination),
        "copy_performed": False,
        "destination_must_be_empty_before_any_future_apply": True,
    })
    return 0


def _project_alias(command: ProjectAliasCommand) -> int:
    """List aliases or append an explicit add/remove event."""
    context = load_context()
    if command.action == "list":
        rows: list[JsonValue] = []
        for item in project_aliases(context.config.vault, command.project_id):
            rows.append(item)
        json_output(rows)
        return 0
    if command.project_id is None or command.alias is None:
        raise ChronicleError("invalid_command", "project alias change requires ID and alias")
    path = change_project_alias(context, command.project_id, command.alias, command.action)
    status = "alias_added" if command.action == "add" else "alias_removed"
    json_output({"status": status, "event_file": path.name})
    return 0


def execute_auxiliary(command: AuxiliaryCommand) -> int:
    """Execute one local-only auxiliary command."""
    if isinstance(command, ServiceCommand):
        return _service(command)
    if isinstance(command, WeeklyReviewCommand):
        return _weekly_review()
    if isinstance(command, InventoryCommand):
        return _inventory(command)
    if isinstance(command, AskCommand):
        return _ask(command)
    if isinstance(command, ContextCommand):
        return _context_preview(command)
    if isinstance(command, ClassifyCommand):
        return _classify(command)
    if isinstance(command, OpenTodayCommand):
        return _open_today(command)
    if isinstance(command, PrivacyInspectCommand):
        return _privacy_inspect(command)
    if isinstance(command, StorageMigrateCommand):
        return _storage_migrate(command)
    return _project_alias(command)
