"""Explicit source registry, foreground collection, and recovery commands."""

import os
import shutil
import sqlite3
import sys
from typing import TypeAlias

from chronicle.collector import collect_once, require_verified_recovery, status_report
from chronicle.command_requests import (
    CollectCommand,
    DaemonCommand,
    DoctorCommand,
    RebuildCommand,
    SourceDisableCommand,
    SourceEnableCommand,
    SourcesListCommand,
    StatusCommand,
)
from chronicle.command_runtime import json_output, load_context
from chronicle.daemon import run_foreground
from chronicle.daily_schedule import inspect_daily_schedule
from chronicle.errors import ChronicleError
from chronicle.service_unit import unit_installed
from chronicle.locking import writer_lock
from chronicle.project_aliases import resolve_project_id
from chronicle.recovery import rebuild_from_markdown
from chronicle.source_registry import (
    change_source_enabled,
    enable_source,
    list_sources,
    source_as_json,
)

CollectorCommand: TypeAlias = (
    CollectCommand | DaemonCommand | DoctorCommand | RebuildCommand | SourceDisableCommand
    | SourceEnableCommand | SourcesListCommand | StatusCommand
)


def _sources_list() -> int:
    """Print registered sources without enabling or reading them."""
    sources = list_sources(load_context())
    json_output([source_as_json(item) for item in sources])
    return 0


def _source_enable(command: SourceEnableCommand) -> int:
    """Persist one explicit source opt-in after scope and policy checks."""
    context = load_context()
    project_id = resolve_project_id(context.config.vault, command.project_id)
    if project_id is None:
        raise ChronicleError("invalid_input", "source registration requires a project ID")
    source = enable_source(
        context, command.source, command.selected_path, command.scope_kind,
        project_id, command.title,
    )
    result = source_as_json(source)
    result["status"] = "enabled"
    json_output(result)
    return 0


def _source_disable(command: SourceDisableCommand) -> int:
    """Disable future reads while preserving source identity and captured history."""
    source = change_source_enabled(load_context(), command.source_id, False)
    result = source_as_json(source)
    result["status"] = "disabled"
    json_output(result)
    return 0


def _collect(command: CollectCommand) -> int:
    """Run one polling pass or a persistent foreground collector loop."""
    context = load_context()
    if not command.once:
        return run_foreground(context, sys.stdout, command.interval_seconds)
    summary = collect_once(context)
    json_output(summary.as_json())
    return 1 if summary.status == "failed" else 0


def _status() -> int:
    """Print real persisted source progress and current foreground lock state."""
    json_output(status_report(load_context()))
    return 0


def _doctor() -> int:
    """Check local storage/runtime without installing or enabling any service."""
    context = load_context()
    report = status_report(context)
    disk_free: int | None
    try:
        disk_free = shutil.disk_usage(context.config.vault).free
    except OSError:
        disk_free = None
    try:
        with sqlite3.connect(":memory:") as connection:
            _ = connection.execute("CREATE VIRTUAL TABLE chronicle_fts_probe USING fts5(value)")
        fts5 = "available"
    except sqlite3.Error:
        fts5 = "unavailable"
    try:
        checkpoints = len(require_verified_recovery(context).references)
        checkpoint_status = "verified"
    except ChronicleError as error:
        checkpoints = 0
        checkpoint_status = error.code
    schedule_error: str | None = None
    last_attempted = None
    try:
        last_attempted = inspect_daily_schedule(context)
    except ChronicleError as error:
        schedule_error = error.code
    writable = os.access(context.config.vault, os.W_OK)
    healthy = (
        disk_free is not None and writable and checkpoint_status == "verified"
        and fts5 == "available" and schedule_error is None
    )
    schedule_status = "uninitialized" if last_attempted is None else "ready"
    json_output({
        "status": "healthy" if healthy else "attention",
        "daily_schedule": {
            "status": "attention" if schedule_error is not None else schedule_status,
            "error": schedule_error,
            "last_attempted_date": last_attempted.isoformat() if last_attempted is not None else None,
        },
        "vault": str(context.config.vault),
        "vault_writable": writable,
        "disk_free_bytes": disk_free,
        "sqlite_fts5": fts5,
        "checkpoint_integrity": checkpoint_status,
        "committed_events": checkpoints,
        "collector": report["collector"],
        "service_installation": "installed" if unit_installed() else "not_installed",
        "external_delivery": False,
    })
    return 0 if healthy else 1


def _rebuild() -> int:
    """Rebuild SQLite and cursor projections from verified Markdown only."""
    context = load_context()
    with writer_lock(context.config.vault):
        result = rebuild_from_markdown(context)
    json_output({
        "status": "rebuilt_with_orphans" if result.orphan_event_ids else "rebuilt",
        "committed_events": result.committed_events,
        "uncheckpointed_event_ids": list(result.orphan_event_ids),
    })
    return 0


def execute_collector(command: CollectorCommand) -> int:
    """Execute one explicit source/collector maintenance command."""
    if isinstance(command, SourcesListCommand):
        return _sources_list()
    if isinstance(command, SourceEnableCommand):
        return _source_enable(command)
    if isinstance(command, SourceDisableCommand):
        return _source_disable(command)
    if isinstance(command, CollectCommand):
        return _collect(command)
    if isinstance(command, StatusCommand):
        return _status()
    if isinstance(command, DoctorCommand):
        return _doctor()
    if isinstance(command, RebuildCommand):
        return _rebuild()
    return run_foreground(load_context(), sys.stdout)
