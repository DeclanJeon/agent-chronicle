"""Shared strict reads for candidate notes, decisions, and source events."""

import json
from pathlib import Path
import uuid

from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.errors import ChronicleError
from chronicle.event_classification import effective_event_metadata
from chronicle.event_format import read_capture
from chronicle.markdown import approval_digest, parse_frontmatter
from chronicle.models import JsonValue
from chronicle.privacy import reject_sensitive_text
from chronicle.schema import integer_field, list_field, optional_text, text_field


def candidate_path(vault: Path, candidate_id: str) -> Path:
    """Resolve a candidate by its UUID-shaped stable identifier."""
    try:
        parsed = str(uuid.UUID(candidate_id))
    except ValueError as error:
        raise ChronicleError("invalid_candidate", "candidate ID must be a UUID") from error
    return vault / "90_System/KnowledgeCandidates" / f"{parsed}.md"


def source_event(vault: Path, event_id: str) -> tuple[Path, dict[str, JsonValue], str]:
    """Find one checkpoint-committed source event by exact ID."""
    snapshot = verify_checkpoint_chain(vault)
    references = [item for item in snapshot.references if item.event_id == event_id]
    if len(references) != 1:
        raise ChronicleError("source_unavailable", "source event is missing or ambiguous")
    reference = references[0]
    path = vault / reference.relative_path
    metadata, content = read_capture(path)
    if text_field(metadata, "payload_hash") != reference.payload_hash:
        raise ChronicleError("source_unavailable", "source event body no longer matches its checkpoint")
    return path, effective_event_metadata(vault, metadata), content


def decision_files(vault: Path, candidate_id: str) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Validate and order immutable decisions by their explicit previous-ID chain."""
    folder = vault / "90_System/KnowledgeDecisions" / candidate_id
    if not folder.exists():
        return []
    if folder.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge decision directory is a symbolic link")
    records: list[tuple[Path, dict[str, JsonValue]]] = []
    for path in sorted(folder.glob("*.md")):
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "knowledge decision record is a symbolic link")
        metadata, _body, _normalized = parse_frontmatter(path.read_bytes())
        if text_field(metadata, "kind") != "knowledge_decision":
            raise ChronicleError("invalid_decision", "decision folder contains an unknown record")
        records.append((path, metadata))
    if not records:
        return []
    by_id = {text_field(metadata, "decision_id"): (path, metadata) for path, metadata in records}
    if len(by_id) != len(records):
        raise ChronicleError("invalid_decision", "decision ID is duplicated")
    roots = [
        text_field(metadata, "decision_id")
        for _path, metadata in records
        if optional_text(metadata, "previous_decision_id") is None
    ]
    if len(roots) != 1:
        raise ChronicleError("invalid_decision", "decision history must have exactly one root")
    ordered: list[tuple[Path, dict[str, JsonValue]]] = []
    visited: set[str] = set()
    current_id: str | None = roots[0]
    while current_id is not None:
        if current_id in visited or current_id not in by_id:
            raise ChronicleError("invalid_decision", "decision history is cyclic or references a missing record")
        visited.add(current_id)
        current = by_id[current_id]
        ordered.append(current)
        successors = [
            text_field(metadata, "decision_id")
            for _path, metadata in records
            if optional_text(metadata, "previous_decision_id") == current_id
        ]
        if len(successors) > 1:
            raise ChronicleError("invalid_decision", "decision history has a fork")
        current_id = successors[0] if successors else None
    if visited != set(by_id):
        raise ChronicleError("invalid_decision", "decision history contains a detached record")
    return ordered


def _read_candidate_snapshot(vault: Path, candidate_id: str) -> tuple[dict[str, JsonValue], str, str]:
    """Load a candidate snapshot for same-module validation only."""
    path = candidate_path(vault, candidate_id)
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("candidate_missing", "knowledge candidate does not exist")
    raw = path.read_bytes()
    metadata, _body, _normalized = parse_frontmatter(raw)
    digest = approval_digest(raw, integer_field(metadata, "approval_digest_version"))
    return metadata, raw.decode("utf-8", errors="strict"), digest


def read_retraction_metadata(vault: Path, candidate_id: str) -> tuple[dict[str, JsonValue], str]:
    """Return only safe metadata needed to append a retraction decision."""
    metadata, _snapshot, digest = _read_candidate_snapshot(vault, candidate_id)
    document_id = text_field(metadata, "document_id")
    revision_id = text_field(metadata, "revision_id")
    approval_digest_version = integer_field(metadata, "approval_digest_version")
    scope = text_field(metadata, "scope")
    allowed_contexts = (
        list_field(metadata, "allowed_contexts") if "allowed_contexts" in metadata else []
    )
    reject_sensitive_text(
        document_id,
        revision_id,
        scope,
        json.dumps(allowed_contexts, ensure_ascii=False),
        digest,
    )
    decision_metadata: dict[str, JsonValue] = {
        "document_id": document_id,
        "revision_id": revision_id,
        "approval_digest_version": approval_digest_version,
        "scope": scope,
        "allowed_contexts": allowed_contexts,
    }
    return decision_metadata, digest


def preview_candidate(vault: Path, candidate_id: str) -> tuple[dict[str, JsonValue], str, str]:
    """Return only a candidate snapshot that passes the explicit privacy policy."""
    metadata, snapshot, digest = _read_candidate_snapshot(vault, candidate_id)
    reject_sensitive_text(snapshot)
    return metadata, snapshot, digest
