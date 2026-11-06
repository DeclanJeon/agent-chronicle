"""Fail-closed knowledge retrieval with current source and digest checks."""

from datetime import datetime
from pathlib import Path

from chronicle.errors import ChronicleError
from chronicle.knowledge_common import decision_files, source_event
from chronicle.knowledge_decisions import extract_quote
from chronicle.knowledge_models import KnowledgeResult, KnowledgeSearchRequest
from chronicle.knowledge_relations import conflicted_candidate_ids, superseded_candidate_ids
from chronicle.markdown import approval_digest, parse_frontmatter
from chronicle.naming import sha256_text
from chronicle.privacy import contains_sensitive_pattern, normalize_text, reject_sensitive_text
from chronicle.schema import integer_field, list_field, optional_text, text_field


def _eligible_result(
    vault: Path,
    path: Path,
    request: KnowledgeSearchRequest,
) -> KnowledgeResult | None:
    """Revalidate digest, decision, source, access, project, and exact quote."""
    raw = path.read_bytes()
    metadata, body, normalized = parse_frontmatter(raw)
    if contains_sensitive_pattern(normalized):
        return None
    if text_field(metadata, "visibility") != "private":
        return None
    allowed = list_field(metadata, "allowed_contexts")
    if request.context_name not in allowed:
        return None
    project_id = text_field(metadata, "project_id")
    scope = text_field(metadata, "scope")
    if scope.startswith("project:"):
        scoped_project = scope.removeprefix("project:")
        if not scoped_project or scoped_project != project_id or request.project_id != scoped_project:
            return None
    elif scope != "personal":
        return None
    if request.project_id is not None and project_id != request.project_id:
        return None
    digest = approval_digest(raw, integer_field(metadata, "approval_digest_version"))
    history = decision_files(vault, text_field(metadata, "candidate_id"))
    if not history:
        return None
    latest = history[-1][1]
    if text_field(latest, "decision") != "approve" or text_field(latest, "approval_digest") != digest:
        return None
    due_value = optional_text(latest, "review_due_at")
    if due_value is None:
        return None
    try:
        due_at = datetime.fromisoformat(due_value)
    except ValueError as error:
        raise ChronicleError("invalid_decision", "approval review date is invalid") from error
    if due_at.tzinfo is None:
        raise ChronicleError("invalid_decision", "approval review date must include a timezone")
    if due_at <= datetime.now().astimezone():
        return None
    sources = list_field(metadata, "sources")
    if len(sources) != 1 or not isinstance(sources[0], dict):
        return None
    source_ref = sources[0]
    source_event_id = text_field(source_ref, "artifact_id")
    _source_path, source, source_content = source_event(vault, source_event_id)
    if text_field(source_ref, "revision_id") != text_field(source, "revision_id"):
        return None
    if project_id != text_field(source, "project_id"):
        return None
    if text_field(source_ref, "access_policy_version") != text_field(source, "access_policy_version"):
        return None
    quote = extract_quote(body, integer_field(metadata, "quote_chars"))
    if sha256_text(quote) != text_field(source_ref, "quote_hash") or quote not in source_content:
        return None
    searchable = "\n".join((text_field(metadata, "title"), text_field(metadata, "claim"), quote))
    if normalize_text(request.query).casefold() not in normalize_text(searchable).casefold():
        return None
    return KnowledgeResult(
        text_field(metadata, "candidate_id"), text_field(metadata, "title"),
        text_field(metadata, "claim"), quote, source_event_id, project_id, digest,
    )


def search_knowledge(vault: Path, request: KnowledgeSearchRequest) -> list[KnowledgeResult]:
    """Return only exact-evidence results that remain currently eligible."""
    reject_sensitive_text(request.query)
    if not request.query.strip() or len(request.query) > 512:
        raise ChronicleError("invalid_input", "search query must contain 1 to 512 characters")
    blocked_candidate_ids = conflicted_candidate_ids(vault)
    results: list[KnowledgeResult] = []
    candidates = vault / "90_System/KnowledgeCandidates"
    if candidates.is_symlink() or not candidates.is_dir():
        raise ChronicleError("unsafe_path", "knowledge candidate directory is missing or a symbolic link")
    for path in sorted(candidates.glob("*.md")):
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "knowledge candidate is a symbolic link")
        if path.stem in blocked_candidate_ids:
            continue
        result = _eligible_result(vault, path, request)
        if result is not None:
            results.append(result)
    needle = normalize_text(request.query).casefold()

    def rank(item: KnowledgeResult) -> tuple[int, str]:
        """Rank title matches before claim and quote matches with stable ID ties."""
        if needle in normalize_text(item.title).casefold():
            relevance = 0
        elif needle in normalize_text(item.claim).casefold():
            relevance = 1
        else:
            relevance = 2
        return relevance, item.candidate_id

    eligible_ids = {item.candidate_id for item in results}
    superseded = superseded_candidate_ids(vault, eligible_ids)
    return sorted((item for item in results if item.candidate_id not in superseded), key=rank)
