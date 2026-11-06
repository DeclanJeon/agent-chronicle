"""Stable identity and privacy-bounded inputs for rule-distillation jobs."""

import json
from pathlib import Path
import uuid

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.naming import sha256_text

JOB_TYPES = frozenset({"concept", "lesson", "decision", "procedure", "idea", "question", "preference"})


def job_id(session_id: str, input_digest: str) -> str:
    """Derive one stable job identity from the selected session and exact input set."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-chronicle/distill-job/v1/{session_id}/{input_digest}"))


def job_root(vault: Path, selected_job_id: str) -> Path:
    """Validate a canonical job UUID before resolving its Markdown folder."""
    try:
        canonical = str(uuid.UUID(selected_job_id))
    except ValueError as error:
        raise ChronicleError("invalid_distill_job", "distillation job ID must be a UUID") from error
    if canonical != selected_job_id:
        raise ChronicleError("invalid_distill_job", "distillation job ID must be canonical")
    return vault / "90_System/KnowledgeJobs" / canonical


def input_digest(preview: dict[str, JsonValue]) -> str:
    """Hash stable source identities and output-shaping fields without persisting quote text."""
    items = preview.get("items")
    if not isinstance(items, list):
        raise ChronicleError("invalid_distill_input", "distillation input rows are malformed")
    extractor = preview.get("extractor")
    if not isinstance(extractor, str):
        raise ChronicleError("invalid_distill_input", "distillation extractor version is missing")
    references: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ChronicleError("invalid_distill_input", "distillation item is malformed")
        knowledge_type = item.get("knowledge_type")
        project_id = item.get("project_id")
        title = item.get("title")
        claim = item.get("claim")
        quote = item.get("exact_quote")
        sources = item.get("sources")
        if (
            not isinstance(knowledge_type, str) or knowledge_type not in JOB_TYPES
            or not isinstance(project_id, str) or not isinstance(title, str)
            or not isinstance(claim, str) or not isinstance(quote, str)
            or not isinstance(sources, list) or not sources
        ):
            raise ChronicleError("invalid_distill_input", "distillation item fields are malformed")
        for source in sources:
            if not isinstance(source, dict):
                raise ChronicleError("invalid_distill_input", "distillation source is malformed")
            event_id = source.get("event_id")
            revision_id = source.get("revision_id")
            locator = source.get("locator")
            zone = source.get("zone")
            if (
                not isinstance(event_id, str) or not isinstance(revision_id, str)
                or not isinstance(locator, str) or not isinstance(zone, str)
            ):
                raise ChronicleError("invalid_distill_input", "distillation source identity is malformed")
            references.append({
                "event_id": event_id,
                "revision_id": revision_id,
                "locator": locator,
                "project_id": project_id,
                "knowledge_type": knowledge_type,
                "zone": zone,
                "title": title,
                "claim_hash": sha256_text(claim),
                "quote_hash": sha256_text(quote),
            })
    ordered = sorted(references, key=lambda row: (
        row["event_id"], row["revision_id"], row["locator"], row["project_id"],
        row["knowledge_type"], row["zone"], row["title"], row["claim_hash"], row["quote_hash"],
    ))
    encoded = json.dumps({
        "extractor": extractor, "candidate_policy": "local-evidence-v1", "sources": ordered,
    }, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)
