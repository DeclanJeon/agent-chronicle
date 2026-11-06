"""Explicit, revision-bound knowledge relations with review-gated conflict blocking."""

from datetime import datetime
from pathlib import Path
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.destination import verify_vault
from chronicle.errors import ChronicleError
from chronicle.knowledge_common import preview_candidate
from chronicle.knowledge_models import RelationInput, RelationReviewInput
from chronicle.knowledge_relation_store import (
    RELATION_DECISIONS,
    RELATION_TYPES,
    decision_digest,
    decision_history,
    canonical_relation_id,
    read_relation,
    record_digest,
    relation_decision_path,
    relation_path,
    relation_records,
    relation_state,
)
from chronicle.locking import writer_lock
from chronicle.markdown import render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.privacy import normalize_text, reject_sensitive_text
from chronicle.naming import sha256_text
from chronicle.schema import text_field

def create_relation(context: CaptureContext, request: RelationInput) -> Path:
    """Record a user-specified relation without inferring, merging, or approving it."""
    rationale = normalize_text(request.reason).strip()
    reject_sensitive_text(
        request.first_candidate_id, request.second_candidate_id,
        request.relation_type, rationale,
    )
    if request.relation_type not in RELATION_TYPES:
        raise ChronicleError("invalid_input", "relationship type is unsupported")
    if not rationale or len(rationale) > 2048:
        raise ChronicleError("invalid_input", "relationship reason must contain 1 to 2048 characters")
    if request.first_candidate_id == request.second_candidate_id:
        raise ChronicleError("invalid_input", "a candidate cannot relate to itself")
    if request.relation_type in {"related_to", "contradicts"}:
        first_id, second_id = sorted((request.first_candidate_id, request.second_candidate_id))
    else:
        first_id, second_id = request.first_candidate_id, request.second_candidate_id
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    with writer_lock(vault):
        first, _first_snapshot, first_digest = preview_candidate(vault, first_id)
        second, _second_snapshot, second_digest = preview_candidate(vault, second_id)
        first_project = text_field(first, "project_id")
        second_project = text_field(second, "project_id")
        if first_project != second_project:
            raise ChronicleError("scope_mismatch", "relations cannot cross project scope")
        first_revision = text_field(first, "revision_id")
        second_revision = text_field(second, "revision_id")
        relation_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agent-chronicle/relation/v2/{request.relation_type}/{first_id}/{second_id}/"
            + f"{first_revision}/{second_revision}/{sha256_text(rationale)}",
        ))
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata: dict[str, JsonValue] = {
            "schema_version": 2,
            "kind": "knowledge_relation",
            "relation_id": relation_id,
            "relation_type": request.relation_type,
            "first_candidate_id": first_id,
            "first_candidate_revision_id": first_revision,
            "first_candidate_digest": first_digest,
            "second_candidate_id": second_id,
            "second_candidate_revision_id": second_revision,
            "second_candidate_digest": second_digest,
            "project_id": first_project,
            "rationale": rationale,
            "review_status": "pending_review",
            "created_at": created_at,
        }
        body = (
            "# Explicit knowledge relationship proposal\n\n"
            "User-authored link only. Endpoint digests and rationale are immutable; the relationship does not merge claims or establish truth.\n"
        )
        metadata["relation_digest"] = record_digest(metadata, body)
        encoded = render_markdown(metadata, body).encode("utf-8")
        path = relation_path(vault, relation_id)
        if path.exists():
            existing, _existing_body = read_relation(path)
            if any(
                existing.get(key) != metadata.get(key)
                for key in (
                    "relation_id", "relation_type", "first_candidate_id",
                    "first_candidate_revision_id", "first_candidate_digest",
                    "second_candidate_id", "second_candidate_revision_id",
                    "second_candidate_digest", "project_id", "rationale",
                )
            ):
                raise ChronicleError("relation_conflict", "relation ID exists with different content")
            return path
        atomic_write_new(path, encoded)
        return path


def review_relation(context: CaptureContext, request: RelationReviewInput) -> Path:
    """Append a digest-bound explicit confirmation or dismissal decision."""
    relation_id = canonical_relation_id(request.relation_id)
    reject_sensitive_text(relation_id, request.reviewer, request.reason)
    if request.decision not in RELATION_DECISIONS or not request.reviewer.strip() or not request.reason.strip():
        raise ChronicleError("invalid_input", "relation review requires a decision, reviewer, and reason")
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    with writer_lock(vault):
        relation_file = relation_path(vault, relation_id)
        metadata, _body = read_relation(relation_file)
        _ = relation_state(vault, metadata)
        relation_digest = text_field(metadata, "relation_digest")
        history = decision_history(vault, relation_id)
        if history and text_field(history[-1][1], "decision") == request.decision:
            return history[-1][0]
        previous_id = text_field(history[-1][1], "decision_id") if history else None
        decision_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agent-chronicle/relation-decision/v1/{relation_id}/"
            + f"{relation_digest}/{request.decision}/{previous_id or 'root'}",
        ))
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        decision_metadata: dict[str, JsonValue] = {
            "schema_version": 1,
            "kind": "knowledge_relation_decision",
            "decision_id": decision_id,
            "previous_decision_id": previous_id,
            "relation_id": relation_id,
            "relation_digest": relation_digest,
            "decision": request.decision,
            "reviewer": request.reviewer,
            "reason": request.reason,
            "created_at": created_at,
        }
        body = f"# Relation {request.decision}\n\nRelation: `{request.relation_id}`\n"
        decision_metadata["decision_digest"] = decision_digest(decision_metadata, body)
        path = relation_decision_path(vault, relation_id, created_at[:10], decision_id)
        atomic_write_new(path, render_markdown(decision_metadata, body).encode("utf-8"))
        return path


def list_relations(vault: Path, candidate_id: str | None = None) -> list[dict[str, JsonValue]]:
    """List revision-bound relation records and current review states."""
    rows: list[dict[str, JsonValue]] = []
    for _path, metadata in relation_records(vault):
        first_id = text_field(metadata, "first_candidate_id")
        second_id = text_field(metadata, "second_candidate_id")
        if candidate_id is not None and candidate_id not in (first_id, second_id):
            continue
        rows.append({
            "relation_id": text_field(metadata, "relation_id"),
            "relation_type": text_field(metadata, "relation_type"),
            "first_candidate_id": first_id,
            "first_candidate_revision_id": metadata.get("first_candidate_revision_id"),
            "first_candidate_digest": metadata.get("first_candidate_digest"),
            "second_candidate_id": second_id,
            "second_candidate_revision_id": metadata.get("second_candidate_revision_id"),
            "second_candidate_digest": metadata.get("second_candidate_digest"),
            "project_id": text_field(metadata, "project_id"),
            "rationale": metadata.get("rationale", ""),
            "status": relation_state(vault, metadata),
            "created_at": text_field(metadata, "created_at"),
        })
    return rows


def conflicted_candidate_ids(vault: Path) -> set[str]:
    """Exclude both endpoints of unreviewed or confirmed contradictions."""
    blocked: set[str] = set()
    for _path, metadata in relation_records(vault):
        if text_field(metadata, "relation_type") == "contradicts":
            state = relation_state(vault, metadata)
            if state in ("pending_review", "confirmed"):
                blocked.add(text_field(metadata, "first_candidate_id"))
                blocked.add(text_field(metadata, "second_candidate_id"))
    return blocked


def superseded_candidate_ids(vault: Path, eligible_candidate_ids: set[str]) -> set[str]:
    """Hide superseded targets only when the explicitly approved successor is eligible."""
    superseded: set[str] = set()
    if not eligible_candidate_ids:
        return superseded
    for _path, metadata in relation_records(vault):
        if (
            text_field(metadata, "relation_type") == "supersedes"
            and relation_state(vault, metadata) == "confirmed"
            and text_field(metadata, "first_candidate_id") in eligible_candidate_ids
        ):
            superseded.add(text_field(metadata, "second_candidate_id"))
    return superseded


def pending_relation_rows(vault: Path) -> list[dict[str, JsonValue]]:
    """Return relation proposals that still need explicit human review."""
    return [
        {
            "kind": "relation",
            "relation_id": row["relation_id"],
            "relation_type": row["relation_type"],
            "rationale": row["rationale"],
            "candidate_id": row["first_candidate_id"],
            "target_candidate_id": row["second_candidate_id"],
            "priority": 1 if row["relation_type"] == "contradicts" else 3,
            "status": "relation_pending_review",
            "review_due_at": None,
        }
        for row in list_relations(vault)
        if row["status"] == "pending_review"
    ]
