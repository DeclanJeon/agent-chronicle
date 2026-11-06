"""Explicit local distillation jobs with durable progress and retry checks."""

from typing import cast

from chronicle.destination import verify_vault
from chronicle.errors import ChronicleError
from chronicle.knowledge_candidates import create_candidate
from chronicle.knowledge_distill import preview_session_distillation
from chronicle.knowledge_job_identity import JOB_TYPES, input_digest, job_id
from chronicle.knowledge_job_store import append_job_state, job_history, job_lock, summarize_job
from chronicle.knowledge_models import CandidateInput, KnowledgeType
from chronicle.models import CaptureContext, JsonValue
from chronicle.schema import text_field


def _execute_job(
    context: CaptureContext,
    preview: dict[str, JsonValue],
    selected_job_id: str,
    digest: str,
) -> dict[str, JsonValue]:
    """Persist candidates after a durable pending state and append outcome state."""
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    session_id = text_field(preview, "session_id")
    items_value = preview.get("items")
    if not isinstance(items_value, list):
        raise ChronicleError("invalid_distill_input", "distillation items are malformed")
    source_event_id_set: set[str] = set()
    for item in items_value:
        if not isinstance(item, dict):
            continue
        source_rows = item.get("sources")
        if not isinstance(source_rows, list):
            continue
        for source in source_rows:
            if isinstance(source, dict):
                event_id = source.get("event_id")
                if isinstance(event_id, str):
                    source_event_id_set.add(event_id)
    source_event_ids = sorted(source_event_id_set)
    with job_lock(context, selected_job_id):
        history = job_history(vault, selected_job_id)
        if history:
            latest = history[-1][1]
            if text_field(latest, "input_digest") != digest or text_field(latest, "session_id") != session_id:
                raise ChronicleError("distill_input_changed", "distillation inputs changed; start a new job")
            if text_field(latest, "status") == "completed":
                return summarize_job(latest)
            attempt = latest.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool):
                raise ChronicleError("invalid_distill_job", "distillation attempt is invalid")
            if text_field(latest, "status") == "failed":
                attempt += 1
                _pending_path, _pending_state = append_job_state(
                    vault, selected_job_id, session_id, digest, source_event_ids,
                    "pending", attempt, [],
                )
        else:
            attempt = 1
            _pending_path, _pending_state = append_job_state(
                vault, selected_job_id, session_id, digest, source_event_ids,
                "pending", attempt, [],
            )

        candidate_ids: list[str] = []
        try:
            for item in items_value:
                if not isinstance(item, dict):
                    raise ChronicleError("invalid_distill_input", "distillation item is malformed")
                knowledge_type_value = item.get("knowledge_type")
                title = item.get("title")
                claim = item.get("claim")
                quote = item.get("exact_quote")
                sources = item.get("sources")
                if (
                    not isinstance(knowledge_type_value, str) or knowledge_type_value not in JOB_TYPES
                    or not isinstance(title, str) or not isinstance(claim, str)
                    or not isinstance(quote, str) or not isinstance(sources, list)
                ):
                    raise ChronicleError("invalid_distill_input", "distillation item fields are malformed")
                knowledge_type = cast(KnowledgeType, knowledge_type_value)
                for source in sources:
                    if not isinstance(source, dict):
                        raise ChronicleError("invalid_distill_input", "distillation source is malformed")
                    event_id = source.get("event_id")
                    if not isinstance(event_id, str):
                        raise ChronicleError("invalid_distill_input", "distillation source is malformed")
                    path = create_candidate(context, CandidateInput(
                        event_id, title, claim, quote,
                        "rule-distill/marked-zones-v1", knowledge_type,
                    ))
                    candidate_ids.append(path.stem)
        except ChronicleError as error:
            _failed_path, _failed_state = append_job_state(
                vault, selected_job_id, session_id, digest, source_event_ids,
                "failed", attempt, sorted(set(candidate_ids)), error.code,
            )
            raise
        except OSError as error:
            _failed_path, _failed_state = append_job_state(
                vault, selected_job_id, session_id, digest, source_event_ids,
                "failed", attempt, sorted(set(candidate_ids)), "io_error",
            )
            raise ChronicleError("distill_failed", "candidate publication failed") from error
        _path, completed = append_job_state(
            vault, selected_job_id, session_id, digest, source_event_ids,
            "completed", attempt, sorted(set(candidate_ids)),
        )
        return summarize_job(completed)


def persist_session_distillation(context: CaptureContext, session_id: str) -> dict[str, JsonValue]:
    """Create or resume an explicitly requested local candidate publication job."""
    preview = preview_session_distillation(context.config.vault, session_id)
    if preview.get("status") == "no_marked_zones":
        return {"status": "no_marked_zones", "job_id": None, "candidate_ids": [], "persisted": False}
    preview_items = preview.get("items")
    if not isinstance(preview_items, list) or not preview_items:
        return {
            "status": "no_eligible_candidates", "job_id": None,
            "candidate_ids": [], "persisted": False,
        }
    digest = input_digest(preview)
    selected_job_id = job_id(text_field(preview, "session_id"), digest)
    result = _execute_job(context, preview, selected_job_id, digest)
    return {**result, "persisted": result.get("status") == "completed"}


def retry_distillation(context: CaptureContext, selected_job_id: str) -> dict[str, JsonValue]:
    """Resume one pending/failed job only if its committed source set is unchanged."""
    vault = verify_vault(context.config.vault, context.config.host_id, context.config.source_instance_id)
    history = job_history(vault, selected_job_id)
    if not history:
        raise ChronicleError("distill_job_missing", "distillation job does not exist")
    latest = history[-1][1]
    if text_field(latest, "status") == "completed":
        return summarize_job(latest)
    session_id = text_field(latest, "session_id")
    preview = preview_session_distillation(vault, session_id)
    digest = input_digest(preview)
    if digest != text_field(latest, "input_digest"):
        raise ChronicleError("distill_input_changed", "distillation inputs changed; start a new job")
    return _execute_job(context, preview, selected_job_id, digest)
