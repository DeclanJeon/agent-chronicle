"""Creation of immutable, evidence-bound knowledge candidate notes."""

from pathlib import Path
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.destination import verify_vault
from chronicle.errors import ChronicleError
from chronicle.knowledge_common import candidate_path, source_event
from chronicle.knowledge_families import record_family_member
from chronicle.knowledge_models import CandidateInput
from chronicle.locking import writer_lock
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.naming import fenced, sha256_text
from chronicle.privacy import contains_sensitive_pattern, normalize_text, reject_sensitive_text
from chronicle.schema import text_field

_KNOWLEDGE_TYPES = ("concept", "lesson", "decision", "procedure", "idea", "question", "preference")


def _family_id(project_id: str, knowledge_type: str, title: str, claim: str) -> str:
    """Derive a conservative exact-text family ID without semantic inference."""
    if knowledge_type not in _KNOWLEDGE_TYPES:
        raise ChronicleError("invalid_candidate", "knowledge type is unsupported")
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "agent-chronicle/knowledge-family/v1/"
        + f"{project_id}/{knowledge_type}/"
        + f"{normalize_text(title).casefold()}/"
        + normalize_text(claim).casefold(),
    ))


def create_candidate(context: CaptureContext, request: CandidateInput) -> Path:
    """Write one immutable candidate whose quote is exact committed source text."""
    reject_sensitive_text(
        request.event_id, request.title, request.claim, request.quote, request.created_by,
    )
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    if request.knowledge_type not in _KNOWLEDGE_TYPES:
        raise ChronicleError("invalid_input", "knowledge type is unsupported")
    if not request.title.strip() or not request.claim.strip() or not request.created_by.strip():
        raise ChronicleError("invalid_input", "candidate title, claim, and author are required")
    with writer_lock(vault):
        _source_path, source, source_content = source_event(vault, request.event_id)
        quote = normalize_text(request.quote)
        if not quote or quote not in source_content:
            raise ChronicleError("invalid_source_locator", "quote must occur verbatim in the captured source")
        source_revision = text_field(source, "revision_id")
        project_id = text_field(source, "project_id")
        quote_hash = sha256_text(quote)
        candidate_name = (
            f"agent-chronicle/candidate/v1/{request.event_id}/{source_revision}/"
            + f"{quote_hash}/{request.title}/{request.claim}"
        )
        if request.knowledge_type != "lesson":
            candidate_name = f"{candidate_name}/type/{request.knowledge_type}"
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, candidate_name))
        family_id = _family_id(project_id, request.knowledge_type, request.title, request.claim)
        duplicate_candidate_ids: list[str] = []
        candidate_root = vault / "90_System/KnowledgeCandidates"
        for existing_path in sorted(candidate_root.glob("*.md")):
            if existing_path.is_symlink():
                raise ChronicleError("unsafe_path", "knowledge candidate is a symbolic link")
            existing_metadata, _existing_body, existing_normalized = parse_frontmatter(existing_path.read_bytes())
            if contains_sensitive_pattern(existing_normalized):
                continue
            if text_field(existing_metadata, "candidate_id") != existing_path.stem:
                raise ChronicleError("invalid_candidate", "candidate filename differs from its identity")
            existing_type = existing_metadata.get("knowledge_type", "lesson")
            if not isinstance(existing_type, str) or existing_type not in _KNOWLEDGE_TYPES:
                raise ChronicleError("invalid_candidate", "existing knowledge type is unsupported")
            existing_family = _family_id(
                text_field(existing_metadata, "project_id"), existing_type,
                text_field(existing_metadata, "title"), text_field(existing_metadata, "claim"),
            )
            stored_family = existing_metadata.get("candidate_family_id")
            if stored_family is not None and stored_family != existing_family:
                raise ChronicleError("invalid_candidate", "candidate family ID does not match its content")
            if existing_family == family_id and existing_path.stem != candidate_id:
                duplicate_candidate_ids.append(existing_path.stem)
        revision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"knowledge-revision:{candidate_id}"))
        duplicate_ids: list[JsonValue] = []
        for duplicate_id in sorted(duplicate_candidate_ids):
            duplicate_ids.append(duplicate_id)
        metadata: dict[str, JsonValue] = {
            "schema_version": 2,
            "kind": "knowledge_note",
            "document_id": candidate_id,
            "revision_id": revision_id,
            "previous_revision_id": None,
            "candidate_id": candidate_id,
            "candidate_family_id": family_id,
            "family_membership_version": 1,
            "duplicate_candidate_ids": duplicate_ids,
            "duplicate_status": "possible_duplicate" if duplicate_candidate_ids else "none",
            "knowledge_type": request.knowledge_type,
            "title": request.title,
            "aliases": [],
            "tags": [],
            "project_ids": [project_id],
            "area_ids": [],
            "review_status": "candidate",
            "validity": "unverified",
            "claim_basis": "user_authored_candidate",
            "scope": f"project:{project_id}",
            "visibility": "private",
            "allowed_contexts": ["personal"],
            "sources": [{
                "artifact_id": request.event_id,
                "revision_id": source_revision,
                "locator": f"event:{request.event_id}#captured-content",
                "quote_hash": quote_hash,
                "access_policy_version": text_field(source, "access_policy_version"),
            }],
            "relations": [],
            "reviewed_by": None,
            "reviewed_at": None,
            "review_due_at": None,
            "supersedes_revision_id": None,
            "extractor_version": "manual-v1",
            "approval_digest_version": 1,
            "project_id": project_id,
            "created_by": request.created_by,
            "created_at": text_field(source, "observed_at"),
            "claim": request.claim,
            "quote_chars": len(quote),
        }
        body = (
            "# Knowledge candidate\n\n## Claim (user-authored; not yet approved)\n\n"
            f"{request.claim}\n\n## Exact source quote (untrusted data)\n\n{fenced(quote)}"
            f"\nSource event: `{request.event_id}`\n"
        )
        candidate_markdown = render_markdown(metadata, body)
        reject_sensitive_text(candidate_markdown)
        path = candidate_path(vault, candidate_id)
        encoded = candidate_markdown.encode("utf-8")
        record_membership = True
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ChronicleError("unsafe_path", "knowledge candidate is a symbolic link")
            existing = path.read_bytes()
            if existing != encoded:
                legacy_metadata = {
                    key: value for key, value in metadata.items()
                    if key != "family_membership_version"
                }
                legacy_encoded = render_markdown(legacy_metadata, body).encode("utf-8")
                if existing != legacy_encoded:
                    raise ChronicleError("candidate_conflict", "candidate ID exists with different content")
                record_membership = False  # Keep pre-v013 candidate Markdown byte-for-byte immutable.
        else:
            atomic_write_new(path, encoded)
        if record_membership:
            _ = record_family_member(
                vault, family_id, candidate_id, revision_id, sha256_text(candidate_markdown),
            )
        return path
