"""Bounded storage validation for explicit knowledge relation histories."""

from pathlib import Path
import json
import uuid

from chronicle.errors import ChronicleError
from chronicle.knowledge_common import preview_candidate
from chronicle.markdown import parse_frontmatter
from chronicle.models import JsonValue
from chronicle.naming import sha256_text
from chronicle.privacy import reject_sensitive_text
from chronicle.schema import text_field

_LEGACY_RELATION_TYPES = ("related_to", "contradicts")
_RELATION_TYPES = ("supports", "related_to", "contradicts", "supersedes", "used_in")
_RELATION_DECISIONS = ("confirm", "dismiss")


def canonical_relation_id(relation_id: str) -> str:
    """Validate the canonical UUID used for all relation storage paths."""
    try:
        canonical = str(uuid.UUID(relation_id))
    except ValueError as error:
        raise ChronicleError("invalid_relation", "relation ID must be a canonical UUID") from error
    if canonical != relation_id:
        raise ChronicleError("invalid_relation", "relation ID must be a canonical UUID")
    return canonical


def _vault_child(vault: Path, relative: Path) -> Path:
    """Resolve one relation storage path and keep every component inside the Vault."""
    if relative.is_absolute() or ".." in relative.parts or vault.is_symlink():
        raise ChronicleError("unsafe_path", "relation storage path is unsafe")
    try:
        root = vault.resolve(strict=True)
        candidate = root / relative
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ChronicleError("unsafe_path", "relation storage path cannot be resolved") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ChronicleError("unsafe_path", "relation storage path traverses a symbolic link")
    if not resolved.is_relative_to(root):
        raise ChronicleError("unsafe_path", "relation storage path escapes the Vault")
    return candidate


def _record_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash immutable relation fields and body, excluding the self-digest."""
    payload = {key: value for key, value in metadata.items() if key != "relation_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def _decision_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash immutable relation review fields and body without self-reference."""
    payload = {key: value for key, value in metadata.items() if key != "decision_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def _relation_path(vault: Path, relation_id: str) -> Path:
    """Return one validated, Vault-contained relation record path."""
    canonical = canonical_relation_id(relation_id)
    return _vault_child(vault, Path("90_System/KnowledgeRelations") / f"{canonical}.md")


def relation_decision_path(
    vault: Path,
    relation_id: str,
    day: str,
    decision_id: str,
) -> Path:
    """Return one validated, Vault-contained immutable relation decision path."""
    canonical = canonical_relation_id(relation_id)
    try:
        canonical_decision = str(uuid.UUID(decision_id))
    except ValueError as error:
        raise ChronicleError("invalid_relation_decision", "relation decision ID is invalid") from error
    if canonical_decision != decision_id or len(day) != 10:
        raise ChronicleError("invalid_relation_decision", "relation decision path identity is invalid")
    return _vault_child(
        vault,
        Path("90_System/KnowledgeRelationDecisions")
        / canonical / f"{day}_{canonical_decision}.md",
    )


def _read_relation(path: Path) -> tuple[dict[str, JsonValue], str]:
    """Validate relation identity, schema, and immutable digest."""
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("unsafe_path", "knowledge relation path is not a regular file")
    metadata, body, _normalized = parse_frontmatter(path.read_bytes())
    version = metadata.get("schema_version")
    relation_type_value = metadata.get("relation_type")
    allowed_types = _LEGACY_RELATION_TYPES if version == 1 else _RELATION_TYPES
    if (
        not isinstance(version, int) or isinstance(version, bool) or version not in {1, 2}
        or text_field(metadata, "kind") != "knowledge_relation"
        or text_field(metadata, "relation_id") != path.stem
        or not isinstance(relation_type_value, str) or relation_type_value not in allowed_types
        or text_field(metadata, "review_status") != "pending_review"
    ):
        raise ChronicleError("invalid_relation", "knowledge relation schema is unsupported")
    first_id = text_field(metadata, "first_candidate_id")
    second_id = text_field(metadata, "second_candidate_id")
    relation_type = relation_type_value
    rationale_value = metadata.get("rationale", "")
    if not isinstance(rationale_value, str):
        raise ChronicleError("invalid_relation", "knowledge relation rationale is malformed")
    rationale = rationale_value
    if first_id == second_id:
        raise ChronicleError("invalid_relation", "knowledge relation endpoints must differ")
    if relation_type in {"related_to", "contradicts"} and first_id > second_id:
        raise ChronicleError("invalid_relation", "symmetric relation endpoints are not canonical")
    if version == 2 and (not rationale.strip() or len(rationale) > 2048):
        raise ChronicleError("invalid_relation", "knowledge relation rationale is invalid")
    if rationale:
        reject_sensitive_text(rationale)
    if text_field(metadata, "relation_digest") != _record_digest(metadata, body):
        raise ChronicleError("invalid_relation", "knowledge relation digest does not match")
    return metadata, body


def _relation_records(vault: Path) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Read all immutable relation proposals, failing closed on malformed records."""
    root = _vault_child(vault, Path("90_System/KnowledgeRelations"))
    if root.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge relation directory is a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ChronicleError("unsafe_path", "knowledge relation directory is not a directory")
    paths = sorted(root.glob("*.md"))
    if len(paths) > 10_000:
        raise ChronicleError("invalid_relation", "relation history exceeds its record limit")
    records: list[tuple[Path, dict[str, JsonValue]]] = []
    for path in paths:
        metadata, _body = _read_relation(path)
        records.append((path, metadata))
    return records


def _decision_history(vault: Path, relation_id: str) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Validate the append-only linear review chain for one relation."""
    canonical = canonical_relation_id(relation_id)
    root = _vault_child(vault, Path("90_System/KnowledgeRelationDecisions") / canonical)
    if root.is_symlink():
        raise ChronicleError("unsafe_path", "relation decision directory is a symbolic link")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ChronicleError("unsafe_path", "relation decision path is not a directory")
    files = sorted(root.glob("*.md"))
    if len(files) > 10_000:
        raise ChronicleError("invalid_relation_decision", "relation decision history exceeds its record limit")
    entries: dict[str, tuple[Path, dict[str, JsonValue]]] = {}
    children: dict[str | None, list[str]] = {}
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ChronicleError("unsafe_path", "relation decision is unsafe or too large")
        metadata, body, _normalized = parse_frontmatter(path.read_bytes())
        decision_id = text_field(metadata, "decision_id")
        version = metadata.get("schema_version")
        if (
            not isinstance(version, int) or isinstance(version, bool) or version != 1
            or text_field(metadata, "kind") != "knowledge_relation_decision"
            or text_field(metadata, "relation_id") != relation_id
            or text_field(metadata, "decision") not in _RELATION_DECISIONS
            or text_field(metadata, "decision_digest") != _decision_digest(metadata, body)
            or not path.stem.endswith(decision_id)
            or decision_id in entries
        ):
            raise ChronicleError("invalid_relation_decision", "relation decision identity is invalid")
        previous = metadata.get("previous_decision_id")
        if previous is not None and not isinstance(previous, str):
            raise ChronicleError("invalid_relation_decision", "previous relation decision is invalid")
        entries[decision_id] = (path, metadata)
        children.setdefault(previous, []).append(decision_id)
    if not entries:
        return []
    roots = children.get(None, [])
    if len(roots) != 1:
        raise ChronicleError("invalid_relation_decision", "relation decision chain has no unique root")
    ordered: list[tuple[Path, dict[str, JsonValue]]] = []
    current_id: str | None = roots[0]
    visited: set[str] = set()
    while current_id is not None:
        if current_id in visited or current_id not in entries:
            raise ChronicleError("invalid_relation_decision", "relation decision chain is cyclic or detached")
        visited.add(current_id)
        current = entries[current_id]
        ordered.append(current)
        next_ids = children.get(current_id, [])
        if len(next_ids) > 1:
            raise ChronicleError("invalid_relation_decision", "relation decision chain has a fork")
        current_id = next_ids[0] if next_ids else None
    if len(visited) != len(entries):
        raise ChronicleError("invalid_relation_decision", "relation decision chain has detached records")
    return ordered


def _relation_state(vault: Path, metadata: dict[str, JsonValue]) -> str:
    """Return pending/confirmed/dismissed after validating endpoint snapshots."""
    relation_id = text_field(metadata, "relation_id")
    first_id = text_field(metadata, "first_candidate_id")
    second_id = text_field(metadata, "second_candidate_id")
    first_metadata, _first_snapshot, first_digest = preview_candidate(vault, first_id)
    second_metadata, _second_snapshot, second_digest = preview_candidate(vault, second_id)
    if (
        first_digest != text_field(metadata, "first_candidate_digest")
        or second_digest != text_field(metadata, "second_candidate_digest")
        or text_field(first_metadata, "revision_id") != text_field(metadata, "first_candidate_revision_id")
        or text_field(second_metadata, "revision_id") != text_field(metadata, "second_candidate_revision_id")
        or text_field(first_metadata, "project_id") != text_field(metadata, "project_id")
        or text_field(second_metadata, "project_id") != text_field(metadata, "project_id")
    ):
        raise ChronicleError("relation_snapshot_changed", "a knowledge relation endpoint changed")
    history = _decision_history(vault, relation_id)
    if not history:
        return "pending_review"
    latest = history[-1][1]
    if text_field(latest, "relation_digest") != text_field(metadata, "relation_digest"):
        raise ChronicleError("relation_snapshot_changed", "reviewed relation differs from its immutable proposal")
    decision = text_field(latest, "decision")
    if decision not in _RELATION_DECISIONS:
        raise ChronicleError("invalid_relation_decision", "relation review decision is unsupported")
    return "confirmed" if decision == "confirm" else "dismissed"


RELATION_TYPES = _RELATION_TYPES
RELATION_DECISIONS = _RELATION_DECISIONS
record_digest = _record_digest
decision_digest = _decision_digest
relation_path = _relation_path
read_relation = _read_relation
relation_records = _relation_records
decision_history = _decision_history
relation_state = _relation_state
