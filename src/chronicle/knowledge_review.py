"""Stable, paged review backlog from immutable candidate and decision records."""

from datetime import datetime
from pathlib import Path

from chronicle.errors import ChronicleError
from chronicle.knowledge_common import decision_files, source_event
from chronicle.knowledge_families import family_member_ids
from chronicle.knowledge_relations import pending_relation_rows
from chronicle.markdown import approval_digest, parse_frontmatter
from chronicle.models import JsonValue
from chronicle.privacy import contains_sensitive_pattern
from chronicle.schema import integer_field, list_field, optional_text, text_field


def _review_row(
    vault: Path,
    path: Path,
    now: datetime,
) -> dict[str, JsonValue] | None:
    """Return a safe row when a candidate needs attention; omit current approvals."""
    candidate_id = path.stem
    try:
        if path.is_symlink() or not path.is_file():
            raise ChronicleError("unsafe_path", "candidate entry is not a regular file")
        raw = path.read_bytes()
        metadata, _body, normalized = parse_frontmatter(raw)
        version = metadata.get("schema_version")
        if (
            not isinstance(version, int) or isinstance(version, bool) or version != 2
            or text_field(metadata, "kind") != "knowledge_note"
        ):
            raise ChronicleError("invalid_candidate", "candidate record schema is unsupported")
        if text_field(metadata, "candidate_id") != candidate_id:
            raise ChronicleError("invalid_candidate", "candidate filename differs from its identity")
        title = text_field(metadata, "title")
        raw_duplicates = metadata.get("duplicate_candidate_ids", [])
        duplicate_candidate_ids: list[str] = []
        if not isinstance(raw_duplicates, list) or any(not isinstance(item, str) for item in raw_duplicates):
            raise ChronicleError("invalid_candidate", "duplicate candidate links are malformed")
        for duplicate_id in raw_duplicates:
            if isinstance(duplicate_id, str):
                duplicate_candidate_ids.append(duplicate_id)
        family_value = metadata.get("candidate_family_id")
        if isinstance(family_value, str):
            persisted_members = family_member_ids(vault, family_value)
            duplicate_candidate_ids = sorted(
                (set(duplicate_candidate_ids) | set(persisted_members)) - {candidate_id}
            )
        duplicate_status_value = metadata.get("duplicate_status", "none")
        if not isinstance(duplicate_status_value, str) or duplicate_status_value not in {"none", "possible_duplicate"}:
            raise ChronicleError("invalid_candidate", "duplicate status is malformed")
        duplicate_status = "possible_duplicate" if duplicate_candidate_ids else "none"
        duplicate_json_values: list[JsonValue] = [item for item in duplicate_candidate_ids]
        if contains_sensitive_pattern(normalized):
            return {
                "candidate_id": candidate_id,
                "title": "[privacy review required]",
                "priority": 0,
                "status": "privacy_review",
                "review_due_at": None,
            }
        history = decision_files(vault, candidate_id)
        if not history:
            if text_field(metadata, "review_status") != "candidate":
                raise ChronicleError("invalid_decision", "candidate has a review status without a decision")
            return {
                "candidate_id": candidate_id,
                "title": title,
                "priority": 4,
                "status": "pending_review",
                "review_due_at": None,
                "duplicate_status": duplicate_status,
                "duplicate_candidate_ids": duplicate_json_values,
            }
        latest = history[-1][1]
        decision = text_field(latest, "decision")
        if decision in ("reject", "retract"):
            return None
        if decision != "approve":
            raise ChronicleError("invalid_decision", "latest knowledge decision is unsupported")
        if (
            text_field(latest, "revision_id") != text_field(metadata, "revision_id")
            or text_field(latest, "approval_digest")
            != approval_digest(raw, integer_field(metadata, "approval_digest_version"))
        ):
            return {
                "candidate_id": candidate_id,
                "title": title,
                "priority": 0,
                "status": "approval_snapshot_changed",
                "review_due_at": optional_text(latest, "review_due_at"),
            }
        sources = list_field(metadata, "sources")
        if len(sources) != 1 or not isinstance(sources[0], dict):
            raise ChronicleError("invalid_candidate", "candidate evidence references are malformed")
        source_ref = sources[0]
        try:
            _source_path, source, _content = source_event(
                vault, text_field(source_ref, "artifact_id"),
            )
        except ChronicleError:
            return {
                "candidate_id": candidate_id,
                "title": title,
                "priority": 0,
                "status": "source_missing",
                "review_due_at": optional_text(latest, "review_due_at"),
            }
        if (
            text_field(source_ref, "revision_id") != text_field(source, "revision_id")
            or text_field(source_ref, "access_policy_version")
            != text_field(source, "access_policy_version")
        ):
            return {
                "candidate_id": candidate_id,
                "title": title,
                "priority": 0,
                "status": "source_policy_changed",
                "review_due_at": optional_text(latest, "review_due_at"),
            }
        due_value = optional_text(latest, "review_due_at")
        if due_value is None:
            status = "review_due_date_missing"
        else:
            try:
                due_at = datetime.fromisoformat(due_value)
            except ValueError:
                status = "review_due_date_invalid"
            else:
                if due_at.tzinfo is None:
                    status = "review_due_date_invalid"
                elif due_at <= now:
                    status = "review_due"
                else:
                    return None
        priority = 1 if status.startswith("review_due_date") else 2
        return {
            "candidate_id": candidate_id,
            "title": title,
            "priority": priority,
            "status": status,
            "review_due_at": due_value,
        }
    except ChronicleError as error:
        return {
            "candidate_id": candidate_id,
            "title": "[record needs attention]",
            "priority": 0,
            "status": error.code,
            "review_due_at": None,
        }


def all_due_review_items(vault: Path, now: datetime | None = None) -> list[dict[str, JsonValue]]:
    """Return the complete current backlog in stable order without changing review state."""
    candidates = vault / "90_System/KnowledgeCandidates"
    if candidates.is_symlink() or not candidates.is_dir():
        raise ChronicleError("unsafe_path", "knowledge candidate directory is missing or a symbolic link")
    review_time = now or datetime.now().astimezone()
    if review_time.tzinfo is None:
        raise ChronicleError("invalid_input", "review time must include a timezone")
    rows = [
        row for path in sorted(candidates.glob("*.md"))
        if (row := _review_row(vault, path, review_time)) is not None
    ]
    rows.extend(pending_relation_rows(vault))
    rows.sort(key=lambda row: (
        row["priority"],
        row["review_due_at"] if isinstance(row["review_due_at"], str) else "9999",
        row["candidate_id"],
    ))
    return rows


def due_review_page(vault: Path, limit: int = 25, offset: int = 0) -> dict[str, JsonValue]:
    """Return a stable priority page without marking any item as reviewed."""
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ChronicleError("invalid_input", "review page size must be between 1 and 100")
    if isinstance(offset, bool) or offset < 0:
        raise ChronicleError("invalid_input", "review page offset must be non-negative")
    rows = all_due_review_items(vault)
    page = rows[offset : offset + limit]
    page_values: list[JsonValue] = list(page)
    return {
        "items": page_values,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(rows),
        "marked_reviewed": False,
    }
