"""Append-only explicit corrections to event project and type classification."""

from datetime import datetime
import json
from pathlib import Path
import stat
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.locking import writer_lock
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.naming import sha256_text
from chronicle.privacy import reject_sensitive_metadata, reject_sensitive_text
from chronicle.schema import text_field

EVENT_TYPES = (
    "user_message", "assistant_message", "tool_call", "tool_result",
    "artifact_created", "artifact_revised", "test_result", "error",
    "approval", "session_closed", "coverage_gap", "file_change", "document", "other",
)


def _record_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash immutable classification fields and body without self-reference."""
    payload = {key: value for key, value in metadata.items() if key != "classification_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def _raw_event(vault: Path, event_id: str) -> tuple[dict[str, JsonValue], str]:
    """Load one event only through its verified checkpoint reference."""
    snapshot = verify_checkpoint_chain(vault)
    matches = [reference for reference in snapshot.references if reference.event_id == event_id]
    if len(matches) != 1:
        raise ChronicleError("source_unavailable", "classification event is not uniquely committed")
    path = vault / matches[0].relative_path
    if path.is_symlink():
        raise ChronicleError("unsafe_path", "classification source is a symbolic link")
    metadata, content = read_capture(path)
    if text_field(metadata, "event_id") != event_id:
        raise ChronicleError("checkpoint_conflict", "classification source differs from its checkpoint")
    return metadata, content


def _records(vault: Path, event_id: str) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Validate and order one event's append-only classification chain."""
    root = vault / "90_System/Classifications" / event_id
    if root.is_symlink():
        raise ChronicleError("unsafe_path", "classification directory is a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ChronicleError("unsafe_path", "classification path is not a directory")
    files = sorted(root.glob("*.md"))
    if len(files) > 10_000:
        raise ChronicleError("invalid_classification", "classification history exceeds its record limit")
    entries: dict[str, tuple[Path, dict[str, JsonValue]]] = {}
    children: dict[str | None, list[str]] = {}
    for path in files:
        try:
            info = path.lstat()
        except OSError as error:
            raise ChronicleError("classification_unavailable", "classification record cannot be inspected") from error
        if not path.is_file() or not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ChronicleError("unsafe_path", "classification event is unsafe or too large")
        metadata, body, _normalized = parse_frontmatter(path.read_bytes())
        classification_id = text_field(metadata, "classification_id")
        version = metadata.get("schema_version")
        event_type = text_field(metadata, "event_type_after")
        previous = metadata.get("previous_classification_id")
        if (
            not isinstance(version, int) or isinstance(version, bool) or version != 1
            or text_field(metadata, "kind") != "event_classification"
            or text_field(metadata, "source_event_id") != event_id
            or event_type not in EVENT_TYPES
            or (previous is not None and not isinstance(previous, str))
            or text_field(metadata, "classification_digest") != _record_digest(metadata, body)
            or not path.stem.endswith(classification_id)
            or classification_id in entries
        ):
            raise ChronicleError("invalid_classification", "classification event is malformed")
        entries[classification_id] = (path, metadata)
        children.setdefault(previous, []).append(classification_id)
    if not entries:
        return []
    roots = children.get(None, [])
    if len(roots) != 1:
        raise ChronicleError("invalid_classification", "classification chain has no unique root")
    ordered: list[tuple[Path, dict[str, JsonValue]]] = []
    current: str | None = roots[0]
    visited: set[str] = set()
    while current is not None:
        if current in visited or current not in entries:
            raise ChronicleError("invalid_classification", "classification chain is cyclic or detached")
        visited.add(current)
        item = entries[current]
        ordered.append(item)
        next_ids = children.get(current, [])
        if len(next_ids) > 1:
            raise ChronicleError("invalid_classification", "classification chain has a fork")
        current = next_ids[0] if next_ids else None
    if len(visited) != len(entries):
        raise ChronicleError("invalid_classification", "classification chain has detached records")
    return ordered


def effective_event_metadata(vault: Path, raw_metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Overlay the latest explicit correction while preserving the committed source."""
    event_id = text_field(raw_metadata, "event_id")
    history = _records(vault, event_id)
    if not history:
        return raw_metadata
    expected_project = text_field(raw_metadata, "project_id")
    expected_type = text_field(raw_metadata, "event_type")
    source_revision = text_field(raw_metadata, "revision_id")
    for _path, record in history:
        if (
            text_field(record, "source_revision_id") != source_revision
            or text_field(record, "project_id_before") != expected_project
            or text_field(record, "event_type_before") != expected_type
        ):
            raise ChronicleError("classification_source_changed", "classification chain differs from its source")
        expected_project = text_field(record, "project_id_after")
        expected_type = text_field(record, "event_type_after")
    effective = dict(raw_metadata)
    effective["project_id"] = expected_project
    effective["event_type"] = expected_type
    effective["classification_id"] = text_field(history[-1][1], "classification_id")
    return effective


def classify_event(
    context: CaptureContext,
    event_id: str,
    project_id: str,
    event_type: str | None,
    reviewer: str,
    reason: str,
) -> dict[str, JsonValue]:
    """Append an explicit, reasoned correction without rewriting original events."""
    if event_type is not None and event_type not in EVENT_TYPES:
        raise ChronicleError("invalid_input", "event type is unsupported")
    stable_project_id = project_id.strip()
    reviewer_name = reviewer.strip()
    explanation = reason.strip()
    if (
        not stable_project_id or len(stable_project_id) > 128
        or not reviewer_name or len(reviewer_name) > 128
        or not explanation or len(explanation) > 2_048
    ):
        raise ChronicleError("invalid_input", "classification requires bounded project, reviewer, and reason")
    reject_sensitive_metadata(event_id, stable_project_id, reviewer_name)
    reject_sensitive_text(explanation)
    vault = context.config.vault
    with writer_lock(vault):
        raw, _content = _raw_event(vault, event_id)
        current = effective_event_metadata(vault, raw)
        target_type = event_type if event_type is not None else text_field(current, "event_type")
        if (
            stable_project_id == text_field(current, "project_id")
            and target_type == text_field(current, "event_type")
        ):
            return {
                "status": "unchanged",
                "event_id": event_id,
                "project_id": stable_project_id,
                "event_type": target_type,
            }
        history = _records(vault, event_id)
        previous = text_field(history[-1][1], "classification_id") if history else None
        source_revision = text_field(raw, "revision_id")
        classification_seed = json.dumps(
            ["agent-chronicle/classification/v1", event_id, source_revision,
             stable_project_id, target_type, previous or "root"],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        classification_id = str(uuid.uuid5(uuid.NAMESPACE_URL, classification_seed))
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata: dict[str, JsonValue] = {
            "schema_version": 1,
            "kind": "event_classification",
            "classification_id": classification_id,
            "previous_classification_id": previous,
            "source_event_id": event_id,
            "source_revision_id": source_revision,
            "project_id_before": text_field(current, "project_id"),
            "event_type_before": text_field(current, "event_type"),
            "project_id_after": stable_project_id,
            "event_type_after": target_type,
            "reviewer": reviewer_name,
            "reason": explanation,
            "created_at": created_at,
        }
        body = (
            "# Explicit event classification correction\n\n"
            "Original event Markdown is unchanged; this append-only record changes only search/inventory classification.\n"
        )
        metadata["classification_digest"] = _record_digest(metadata, body)
        path = (
            vault / "90_System/Classifications" / event_id
            / f"{created_at[:10]}_{classification_id}.md"
        )
        atomic_write_new(path, render_markdown(metadata, body).encode("utf-8"))
        return {
            "status": "corrected",
            "event_id": event_id,
            "classification_id": classification_id,
            "project_id": stable_project_id,
            "event_type": target_type,
            "record_file": path.name,
        }
