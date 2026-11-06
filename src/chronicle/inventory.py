"""Local inventory views derived only from checkpointed Markdown events."""

from collections import defaultdict
from pathlib import Path

from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.event_format import read_capture
from chronicle.errors import ChronicleError
from chronicle.event_classification import effective_event_metadata
from chronicle.models import JsonValue
from chronicle.schema import integer_field, optional_text, text_field


def _committed_metadata(vault: Path) -> list[dict[str, JsonValue]]:
    """Read each event through the verified checkpoint chain, never raw folders."""
    snapshot = verify_checkpoint_chain(vault)
    metadata_rows: list[dict[str, JsonValue]] = []
    for reference in snapshot.references:
        event_path = vault / reference.relative_path
        if event_path.is_symlink():
            raise ChronicleError("unsafe_path", "inventory event path is a symbolic link")
        metadata, _content = read_capture(event_path)
        if text_field(metadata, "event_id") != reference.event_id:
            raise ChronicleError("checkpoint_conflict", "inventory event differs from its checkpoint")
        metadata_rows.append(effective_event_metadata(vault, metadata))
    return metadata_rows


def inventory_report(
    vault: Path,
    kind: str,
    project_id: str | None = None,
    session_id: str | None = None,
    document_id: str | None = None,
) -> dict[str, JsonValue]:
    """Return a stable, metadata-only view of projects, sessions, or revisions."""
    rows = _committed_metadata(vault)
    projects: dict[str, list[dict[str, JsonValue]]] = defaultdict(list)
    sessions: dict[tuple[str, str], list[dict[str, JsonValue]]] = defaultdict(list)
    documents: dict[tuple[str, str], list[dict[str, JsonValue]]] = defaultdict(list)
    for row in rows:
        current_project = text_field(row, "project_id")
        current_session = text_field(row, "source_session_id")
        projects[current_project].append(row)
        sessions[(current_project, current_session)].append(row)
        current_document = optional_text(row, "document_id")
        if current_document is not None:
            documents[(current_project, current_document)].append(row)

    if kind == "home":
        return {
            "status": "ready",
            "event_count": len(rows),
            "project_count": len(projects),
            "session_count": len(sessions),
            "document_count": len(documents),
        }
    if kind == "projects":
        items: list[JsonValue] = []
        for identifier, group in sorted(projects.items()):
            if project_id is not None and identifier != project_id:
                continue
            items.append({
                "project_id": identifier,
                "event_count": len(group),
                "session_count": len({text_field(row, "source_session_id") for row in group}),
                "document_count": len({optional_text(row, "document_id") for row in group} - {None}),
                "latest_event_at": max(text_field(row, "occurred_at") for row in group),
            })
        return {"kind": "projects", "items": items, "total": len(items)}
    if kind == "sessions":
        items = []
        for (current_project, identifier), group in sorted(sessions.items()):
            if project_id is not None and current_project != project_id:
                continue
            if session_id is not None and identifier != session_id:
                continue
            event_types: list[JsonValue] = []
            for event_type in sorted({text_field(row, "event_type") for row in group}):
                event_types.append(event_type)
            items.append({
                "session_id": identifier,
                "project_id": current_project,
                "event_count": len(group),
                "first_event_at": min(text_field(row, "occurred_at") for row in group),
                "latest_event_at": max(text_field(row, "occurred_at") for row in group),
                "event_types": event_types,
            })
        return {"kind": "sessions", "items": items, "total": len(items)}
    if kind == "documents":
        items = []
        for (current_project, identifier), group in sorted(documents.items()):
            if project_id is not None and current_project != project_id:
                continue
            if document_id is not None and identifier != document_id:
                continue
            revisions: dict[str, dict[str, JsonValue]] = {}
            for row in group:
                revision_id = text_field(row, "revision_id")
                existing = revisions.get(revision_id)
                if existing is not None and existing != row:
                    raise ChronicleError("revision_conflict", "document revision identity is duplicated")
                revisions[revision_id] = row
            latest = max(
                revisions.values(),
                key=lambda item: integer_field(item, "document_revision"),
            )
            items.append({
                "document_id": identifier,
                "project_id": current_project,
                "revision_count": len(revisions),
                "latest_revision_id": text_field(latest, "revision_id"),
                "latest_revision_number": integer_field(latest, "document_revision"),
                "latest_event_at": text_field(latest, "occurred_at"),
                "is_final": text_field(latest, "review_status") == "final",
            })
        return {"kind": "documents", "items": items, "total": len(items)}
    raise ChronicleError("invalid_command", "unknown inventory view")
