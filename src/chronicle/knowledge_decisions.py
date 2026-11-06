"""Digest-bound approval and immediate knowledge retraction events."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.destination import verify_vault
from chronicle.errors import ChronicleError
from chronicle.knowledge_common import (
    candidate_path,
    read_retraction_metadata,
    decision_files,
    preview_candidate,
    source_event,
)
from chronicle.knowledge_families import family_member_ids
from chronicle.knowledge_models import ApprovalInput, DecisionInput, RejectionInput, RetractionInput
from chronicle.locking import writer_lock
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.naming import sha256_text
from chronicle.privacy import reject_sensitive_text
from chronicle.schema import integer_field, list_field, optional_text, text_field


def _verify_family_membership(
    vault: Path, metadata: dict[str, JsonValue], candidate_id: str,
) -> None:
    """Require the immutable membership snapshot for candidates written by this schema."""
    version = metadata.get("family_membership_version")
    if version is None:
        return  # Legacy candidates predate durable overlap-family records.
    family_id = metadata.get("candidate_family_id")
    if (
        not isinstance(version, int) or isinstance(version, bool) or version != 1
        or not isinstance(family_id, str)
    ):
        raise ChronicleError("invalid_candidate", "candidate family membership version is unsupported")
    members = family_member_ids(vault, family_id)
    if candidate_id not in members:
        raise ChronicleError("invalid_candidate", "candidate differs from its immutable family record")


def _write_decision(vault: Path, request: DecisionInput) -> Path:
    """Append one idempotent immutable decision event after the caller's checks."""
    document_id = text_field(request.metadata, "document_id")
    revision_id = text_field(request.metadata, "revision_id")
    scope = text_field(request.metadata, "scope")
    allowed_contexts = request.metadata.get("allowed_contexts", [])
    reject_sensitive_text(
        request.candidate_id,
        request.decision,
        request.digest,
        request.reviewer,
        request.reason,
        document_id,
        revision_id,
        scope,
        json.dumps(allowed_contexts, ensure_ascii=False),
    )
    reviewed_at = datetime.now().astimezone()
    history = decision_files(vault, request.candidate_id)
    if history:
        latest = history[-1][1]
        same_decision = (
            text_field(latest, "decision") == request.decision
            and text_field(latest, "approval_digest") == request.digest
        )
        if same_decision and request.decision != "approve":
            return history[-1][0]
        if same_decision and request.decision == "approve":
            due_value = optional_text(latest, "review_due_at")
            if due_value is not None:
                try:
                    due_at = datetime.fromisoformat(due_value)
                except ValueError as error:
                    raise ChronicleError("invalid_decision", "approval review date is invalid") from error
                if due_at.tzinfo is None:
                    raise ChronicleError("invalid_decision", "approval review date must include a timezone")
                if due_at > reviewed_at:
                    return history[-1][0]
        previous_id: str | None = text_field(latest, "decision_id")
    else:
        previous_id = None
    revision_id = text_field(request.metadata, "revision_id")
    decision_name = (
        f"agent-chronicle/decision/v1/{request.candidate_id}/{revision_id}/"
        + f"{request.decision}/{request.digest}/{previous_id or 'root'}"
    )
    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, decision_name))
    created_at = reviewed_at.isoformat(timespec="seconds")
    due_at = (
        (reviewed_at + timedelta(days=request.review_days)).isoformat(timespec="seconds")
        if request.decision == "approve" and request.review_days is not None
        else None
    )
    decision_metadata: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": "knowledge_decision",
        "decision_id": decision_id,
        "previous_decision_id": previous_id,
        "decision": request.decision,
        "document_id": text_field(request.metadata, "document_id"),
        "revision_id": revision_id,
        "approval_digest_version": integer_field(request.metadata, "approval_digest_version"),
        "approval_digest": request.digest,
        "reviewer": request.reviewer,
        "created_at": created_at,
        "reviewed_at": created_at if request.decision == "approve" else None,
        "review_due_at": due_at,
        "reason": request.reason,
        "scope": text_field(request.metadata, "scope"),
        "allowed_contexts": request.metadata.get("allowed_contexts", []),
    }
    path = (
        vault / "90_System/KnowledgeDecisions" / request.candidate_id
        / f"{created_at[:10]}_{decision_id}.md"
    )
    body = f"# Knowledge {request.decision}\n\nCandidate: `{request.candidate_id}`\n"
    atomic_write_new(path, render_markdown(decision_metadata, body).encode("utf-8"))
    return path


def approve_candidate(context: CaptureContext, request: ApprovalInput) -> Path:
    """Record explicit approval only after an unchanged digest and source preview."""
    reject_sensitive_text(request.candidate_id, request.confirm_digest, request.reviewer)
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    if (
        not request.reviewer.strip()
        or isinstance(request.review_days, bool)
        or not 1 <= request.review_days <= 365
    ):
        raise ChronicleError("invalid_input", "approval requires a reviewer and review interval from 1 to 365 days")
    with writer_lock(vault):
        metadata, snapshot, digest = preview_candidate(vault, request.candidate_id)
        if digest != request.confirm_digest:
            raise ChronicleError("approval_snapshot_changed", "preview digest differs from confirmed digest")
        _verify_family_membership(vault, metadata, request.candidate_id)
        history = decision_files(vault, request.candidate_id)
        if history and text_field(history[-1][1], "decision") in {"reject", "retract"}:
            raise ChronicleError("candidate_closed", "rejected or retracted candidates cannot be re-approved")
        if text_field(metadata, "review_status") != "candidate":
            raise ChronicleError("invalid_candidate", "only an immutable candidate can be approved")
        second_metadata, second_snapshot, second_digest = preview_candidate(vault, request.candidate_id)
        if second_snapshot != snapshot or second_digest != digest:
            raise ChronicleError("approval_snapshot_changed", "candidate changed before approval")
        sources = list_field(second_metadata, "sources")
        if len(sources) != 1 or not isinstance(sources[0], dict):
            raise ChronicleError("source_unavailable", "candidate must have exactly one source")
        source_ref = sources[0]
        _source_path, source, source_content = source_event(vault, text_field(source_ref, "artifact_id"))
        _candidate_metadata, candidate_body, _candidate_normalized = parse_frontmatter(
            candidate_path(vault, request.candidate_id).read_bytes()
        )
        quote = extract_quote(candidate_body, integer_field(second_metadata, "quote_chars"))
        if quote not in source_content or sha256_text(quote) != text_field(source_ref, "quote_hash"):
            raise ChronicleError("source_unavailable", "source evidence cannot be revalidated")
        if text_field(source_ref, "revision_id") != text_field(source, "revision_id"):
            raise ChronicleError("source_unavailable", "source revision changed")
        if text_field(source_ref, "access_policy_version") != text_field(source, "access_policy_version"):
            raise ChronicleError("source_unavailable", "source access policy changed")
        _latest_metadata, _latest_snapshot, latest_digest = preview_candidate(vault, request.candidate_id)
        if latest_digest != digest:
            raise ChronicleError("approval_snapshot_changed", "candidate changed during approval")
        decision = DecisionInput(
            request.candidate_id, second_metadata, "approve", request.reviewer,
            "explicit user approval", digest, request.review_days,
        )
        return _write_decision(vault, decision)


def reject_candidate(context: CaptureContext, request: RejectionInput) -> Path:
    """Record a digest-bound rejection of an unapproved candidate."""
    reject_sensitive_text(request.candidate_id, request.reviewer, request.reason)
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    if not request.reason.strip() or not request.reviewer.strip():
        raise ChronicleError("invalid_input", "rejection requires reviewer and reason")
    with writer_lock(vault):
        metadata, snapshot, digest = preview_candidate(vault, request.candidate_id)
        _verify_family_membership(vault, metadata, request.candidate_id)
        history = decision_files(vault, request.candidate_id)
        if history:
            latest_decision = text_field(history[-1][1], "decision")
            if latest_decision in {"approve", "retract"}:
                raise ChronicleError("candidate_closed", "approved or retracted candidates cannot be rejected")
            if latest_decision != "reject":
                raise ChronicleError("invalid_decision", "candidate decision state is unsupported")
        if text_field(metadata, "review_status") != "candidate":
            raise ChronicleError("invalid_candidate", "only an immutable candidate can be rejected")
        second_metadata, second_snapshot, second_digest = preview_candidate(vault, request.candidate_id)
        if second_snapshot != snapshot or second_digest != digest:
            raise ChronicleError("approval_snapshot_changed", "candidate changed before rejection")
        decision = DecisionInput(
            request.candidate_id, second_metadata, "reject", request.reviewer, request.reason, digest,
        )
        return _write_decision(vault, decision)


def retract_candidate(context: CaptureContext, request: RetractionInput) -> Path:
    """Immediately append a user retraction that blocks subsequent reuse."""
    reject_sensitive_text(request.candidate_id, request.reviewer, request.reason)
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    if not request.reason.strip() or not request.reviewer.strip():
        raise ChronicleError("invalid_input", "retraction requires reviewer and reason")
    with writer_lock(vault):
        metadata, digest = read_retraction_metadata(vault, request.candidate_id)
        decision = DecisionInput(
            request.candidate_id, metadata, "retract", request.reviewer, request.reason, digest,
        )
        return _write_decision(vault, decision)


def extract_quote(body: str, quote_chars: int) -> str:
    """Extract a single fenced exact quote and enforce its stored character count."""
    marker = "## Exact source quote (untrusted data)\n\n"
    start = body.find(marker)
    if start < 0:
        raise ChronicleError("invalid_candidate", "source quote section is missing")
    section = body[start + len(marker) :]
    lines = section.splitlines(keepends=True)
    if not lines or not lines[0].startswith("```"):
        raise ChronicleError("invalid_candidate", "source quote fence is missing")
    fence = lines[0].split("text", maxsplit=1)[0]
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\n") == fence), -1)
    if end < 0:
        raise ChronicleError("invalid_candidate", "source quote fence is incomplete")
    quote = "".join(lines[1:end])
    if len(quote) < quote_chars:
        raise ChronicleError("invalid_candidate", "quoted text is shorter than metadata")
    return quote[:quote_chars]
