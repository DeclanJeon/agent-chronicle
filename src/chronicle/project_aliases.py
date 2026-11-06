"""Append-only local project alias events keyed to stable project identifiers."""

from datetime import datetime
import json
from pathlib import Path
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.destination import verify_vault
from chronicle.errors import ChronicleError
from chronicle.inventory import inventory_report
from chronicle.locking import writer_lock
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.naming import sha256_text
from chronicle.privacy import normalize_text, reject_sensitive_metadata
from chronicle.schema import text_field


def _alias_key(alias: str) -> str:
    """Normalize a user alias without turning it into a project identity."""
    normalized = normalize_text(alias).strip().casefold()
    if not normalized or len(normalized) > 128 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ChronicleError("invalid_input", "project alias must be bounded non-empty text")
    reject_sensitive_metadata(alias)
    return normalized


def _event_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash alias event fields and fixed body without self-reference."""
    payload = {key: value for key, value in metadata.items() if key != "event_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def _alias_root(vault: Path) -> Path:
    return vault / "90_System/ProjectAliasEvents"


def _read_event(path: Path) -> dict[str, JsonValue]:
    """Validate one immutable project alias event and its path identity."""
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("unsafe_path", "project alias event is not a regular file")
    metadata, body, _normalized = parse_frontmatter(path.read_bytes())
    version = metadata.get("schema_version")
    if (
        not isinstance(version, int) or isinstance(version, bool) or version != 1
        or text_field(metadata, "kind") != "project_alias_event"
        or text_field(metadata, "action") not in ("add", "remove")
        or text_field(metadata, "alias_key") != _alias_key(text_field(metadata, "alias"))
        or text_field(metadata, "event_digest") != _event_digest(metadata, body)
    ):
        raise ChronicleError("invalid_project_alias", "project alias event is malformed")
    expected_name = f"{sha256_text(text_field(metadata, 'alias_key'))[:16]}_{text_field(metadata, 'event_id')}"
    if path.stem != expected_name:
        raise ChronicleError("invalid_project_alias", "project alias event filename is invalid")
    return metadata


def _history(vault: Path, alias_key: str) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Validate and order the linear event chain for one normalized alias."""
    root = _alias_root(vault)
    if root.is_symlink():
        raise ChronicleError("unsafe_path", "project alias event directory is a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ChronicleError("unsafe_path", "project alias event path is not a directory")
    prefix = f"{sha256_text(alias_key)[:16]}_"
    entries: dict[str, tuple[Path, dict[str, JsonValue]]] = {}
    children: dict[str | None, list[str]] = {}
    for path in sorted(root.glob(f"{prefix}*.md")):
        metadata = _read_event(path)
        if text_field(metadata, "alias_key") != alias_key:
            continue
        event_id = text_field(metadata, "event_id")
        if event_id in entries:
            raise ChronicleError("invalid_project_alias", "project alias event ID is duplicated")
        previous = metadata.get("previous_event_id")
        if previous is not None and not isinstance(previous, str):
            raise ChronicleError("invalid_project_alias", "previous alias event ID is invalid")
        entries[event_id] = (path, metadata)
        children.setdefault(previous, []).append(event_id)
    if not entries:
        return []
    roots = children.get(None, [])
    if len(roots) != 1:
        raise ChronicleError("invalid_project_alias", "project alias event chain has no unique root")
    ordered: list[tuple[Path, dict[str, JsonValue]]] = []
    current: str | None = roots[0]
    visited: set[str] = set()
    while current is not None:
        if current in visited or current not in entries:
            raise ChronicleError("invalid_project_alias", "project alias event chain is cyclic or detached")
        visited.add(current)
        item = entries[current]
        ordered.append(item)
        next_events = children.get(current, [])
        if len(next_events) > 1:
            raise ChronicleError("invalid_project_alias", "project alias event chain has a fork")
        current = next_events[0] if next_events else None
    if len(visited) != len(entries):
        raise ChronicleError("invalid_project_alias", "project alias event chain has detached records")
    return ordered


def _all_aliases(vault: Path) -> dict[str, tuple[str, str]]:
    """Return active alias-key mappings and reject cross-project ambiguity."""
    root = _alias_root(vault)
    if root.is_symlink():
        raise ChronicleError("unsafe_path", "project alias event directory is a symbolic link")
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ChronicleError("unsafe_path", "project alias event path is not a directory")
    keys: set[str] = set()
    for path in sorted(root.glob("*.md")):
        metadata = _read_event(path)
        keys.add(text_field(metadata, "alias_key"))
    active: dict[str, tuple[str, str]] = {}
    for key in sorted(keys):
        history = _history(vault, key)
        if not history:
            continue
        latest = history[-1][1]
        if text_field(latest, "action") == "add":
            active[key] = (text_field(latest, "project_id"), text_field(latest, "alias"))
    projects: dict[str, str] = {}
    for key, (project_id, alias) in active.items():
        existing = projects.get(key)
        if existing is not None and existing != project_id:
            raise ChronicleError("ambiguous_project_alias", "project alias points to multiple IDs")
        projects[key] = project_id
        if not alias:
            raise ChronicleError("invalid_project_alias", "project alias display label is empty")
    return active


def _known_project_ids(vault: Path) -> set[str]:
    """Read stable project IDs from checkpointed events only."""
    report = inventory_report(vault, "projects")
    items = report.get("items", [])
    if not isinstance(items, list):
        raise ChronicleError("invalid_project_index", "project inventory is malformed")
    identifiers: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ChronicleError("invalid_project_index", "project inventory entry is malformed")
        identifiers.add(text_field(item, "project_id"))
    return identifiers


def _reject_project_id_collisions(
    aliases: dict[str, tuple[str, str]],
    project_ids: set[str],
) -> None:
    """Reject normalized aliases that would shadow a different stable project ID."""
    for key, (target_project, _alias) in aliases.items():
        if any(
            stable_project != target_project and _alias_key(stable_project) == key
            for stable_project in project_ids
        ):
            raise ChronicleError(
                "ambiguous_project_alias", "project alias collides with a stable project ID",
            )


def change_project_alias(
    context: CaptureContext,
    project_id: str,
    alias: str,
    action: str,
) -> Path:
    """Append an explicit alias add/remove event for an existing project ID."""
    stable_id = project_id.strip()
    key = _alias_key(alias)
    if not stable_id or len(stable_id) > 128 or action not in ("add", "remove"):
        raise ChronicleError("invalid_input", "project ID or alias action is invalid")
    reject_sensitive_metadata(stable_id, alias)
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    with writer_lock(vault):
        known_project_ids = _known_project_ids(vault)
        if stable_id not in known_project_ids:
            raise ChronicleError("project_missing", "project alias requires an existing committed project ID")
        if action == "add" and any(
            existing_id != stable_id and _alias_key(existing_id) == key
            for existing_id in known_project_ids
        ):
            raise ChronicleError(
                "ambiguous_project_alias", "project alias collides with a stable project ID",
            )
        histories = _history(vault, key)
        latest = histories[-1][1] if histories else None
        current_project = text_field(latest, "project_id") if latest is not None and text_field(latest, "action") == "add" else None
        if action == "add" and current_project == stable_id:
            return histories[-1][0]
        if action == "add" and current_project is not None and current_project != stable_id:
            raise ChronicleError("ambiguous_project_alias", "alias is already assigned; remove it first")
        if action == "remove" and current_project != stable_id:
            raise ChronicleError("project_alias_missing", "alias is not assigned to this project ID")
        previous_id = text_field(latest, "event_id") if latest is not None else None
        event_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agent-chronicle/project-alias/v1/{key}/{stable_id}/{action}/{previous_id or 'root'}",
        ))
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata: dict[str, JsonValue] = {
            "schema_version": 1,
            "kind": "project_alias_event",
            "event_id": event_id,
            "previous_event_id": previous_id,
            "action": action,
            "alias_key": key,
            "alias": alias.strip(),
            "project_id": stable_id,
            "created_at": created_at,
        }
        body = f"# Project alias {action}\n\nAlias mapping is explicit; project IDs remain unchanged.\n"
        metadata["event_digest"] = _event_digest(metadata, body)
        path = _alias_root(vault) / f"{sha256_text(key)[:16]}_{event_id}.md"
        if path.exists():
            existing = _read_event(path)
            if existing.get("event_digest") != metadata.get("event_digest"):
                raise ChronicleError("project_alias_conflict", "alias event ID has different content")
            return path
        atomic_write_new(path, render_markdown(metadata, body).encode("utf-8"))
        return path


def resolve_project_id(vault: Path, value: str | None) -> str | None:
    """Resolve a known project alias to its stable ID; otherwise preserve the value."""
    if value is None:
        return None
    known_project_ids = _known_project_ids(vault)
    aliases = _all_aliases(vault)
    _reject_project_id_collisions(aliases, known_project_ids)
    if value in known_project_ids:
        return value
    key = _alias_key(value)
    mapped = aliases.get(key)
    if mapped is None:
        return value
    return mapped[0]


def project_aliases(vault: Path, project_id: str | None = None) -> list[dict[str, JsonValue]]:
    """List active local aliases without exposing project event content."""
    rows: list[dict[str, JsonValue]] = []
    aliases = _all_aliases(vault)
    _reject_project_id_collisions(aliases, _known_project_ids(vault))
    for key, (stable_id, alias) in sorted(aliases.items()):
        if project_id is not None and stable_id != project_id:
            continue
        rows.append({"project_id": stable_id, "alias": alias, "alias_key": key})
    return rows
