"""Source identities, byte positions and replay validation for journal events."""

import hashlib
import json
import uuid

from chronicle.errors import ChronicleError
from chronicle.models import CapturedInput, JsonValue
from chronicle.privacy import reject_sensitive_metadata
from chronicle.schema import mapping_field, text_field


def event_identity(source_instance_id: str, request: CapturedInput) -> tuple[str, str, str]:
    """Derive stable source key, event ID and non-secret stream ID."""
    if request.source_adapter_id == "manual-file":
        identity = [
            source_instance_id,
            request.source_session_id,
            request.source_event_id,
            request.record_part_index,
        ]
    else:
        identity = [
            source_instance_id,
            request.source_adapter_id,
            request.source_session_id,
            request.source_event_id,
            request.record_part_index,
        ]
    if request.source_resume_token is not None:
        stream_id = request.source_stream_id
        if stream_id is None:
            raise ChronicleError("invalid_input", "resumable source must identify its stream")
        identity.extend((stream_id, request.rotation_generation))
    identity_bytes = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(identity_bytes).hexdigest()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-chronicle/event/v1/{key}"))
    stream_id = request.source_stream_id or f"{request.source_adapter_version}:{request.source_file_id}"
    return key, event_id, stream_id


def validate_source_request(request: CapturedInput) -> None:
    """Reject unsafe source identity metadata and invalid byte-position tuples."""
    reject_sensitive_metadata(
        request.source_event_id,
        request.source_session_id,
        request.title,
        request.project_id,
        request.author_name,
        request.source_file_name,
        request.source_adapter_id,
        request.source_adapter_version,
        request.source_stream_id or "",
    )
    if (
        not request.source_adapter_id
        or not request.source_adapter_version
        or (request.source_resume_token is not None and not request.source_stream_id)
        or (request.source_resume_token is None and not request.non_resumable_reason)
        or request.record_start_byte < 0
        or request.record_part_count < 1
        or request.record_part_index < 0
        or request.record_part_index >= request.record_part_count
        or (
            request.record_end_byte_exclusive is not None
            and request.record_end_byte_exclusive < request.record_start_byte
        )
    ):
        raise ChronicleError("invalid_input", "source identity or byte position is invalid")


def validate_source_replay(
    metadata: dict[str, JsonValue],
    request: CapturedInput,
    content: str,
) -> None:
    """Ensure the immutable source position still matches an existing event."""
    position = mapping_field(metadata, "source_position")
    expected_end = (
        request.record_end_byte_exclusive
        if request.record_end_byte_exclusive is not None
        else len(content.encode("utf-8"))
    )
    expected_stream = request.source_stream_id or f"{request.source_adapter_version}:{request.source_file_id}"
    expected_non_resumable_reason = (
        None if request.source_resume_token is not None else request.non_resumable_reason
    )
    if (
        position.get("adapter_id") != request.source_adapter_id
        or position.get("adapter_version") != request.source_adapter_version
        or position.get("stream_id") != expected_stream
        or position.get("file_id") != request.source_file_id
        or position.get("rotation_generation") != request.rotation_generation
        or position.get("record_start_byte") != request.record_start_byte
        or position.get("record_end_byte_exclusive") != expected_end
        or position.get("record_part_index") != request.record_part_index
        or position.get("record_part_count") != request.record_part_count
        or position.get("non_resumable_reason") != expected_non_resumable_reason
        or position.get("parent_event_id") != request.source_parent_event_id
        or position.get("source_call_id") != request.source_call_id
        or position.get("resume_token") != request.source_resume_token
    ):
        raise ChronicleError("idempotency_conflict", "source position changed for an existing event")


def source_position(
    request: CapturedInput,
    stream_id: str,
    content_bytes: bytes,
) -> dict[str, JsonValue]:
    """Build versioned, non-resumable provenance for one committed event."""
    return {
        "version": 1,
        "adapter_id": request.source_adapter_id,
        "adapter_version": request.source_adapter_version,
        "stream_id": stream_id,
        "file_id": request.source_file_id,
        "rotation_generation": request.rotation_generation,
        "record_start_byte": request.record_start_byte,
        "record_end_byte_exclusive": (
            request.record_end_byte_exclusive
            if request.record_end_byte_exclusive is not None
            else len(content_bytes)
        ),
        "record_part_index": request.record_part_index,
        "record_part_count": request.record_part_count,
        "cursor_codec_version": 1,
        "resume_token": request.source_resume_token,
        "parent_event_id": request.source_parent_event_id,
        "source_call_id": request.source_call_id,
        "non_resumable_reason": None if request.source_resume_token is not None else request.non_resumable_reason,
    }


def checkpoint_position(
    metadata: dict[str, JsonValue],
    request: CapturedInput,
) -> tuple[dict[str, JsonValue], int, str]:
    """Prepare the single-event checkpoint while keeping static imports non-resumable."""
    position = mapping_field(metadata, "source_position")
    end_position = position.get("record_end_byte_exclusive")
    if not isinstance(end_position, int) or isinstance(end_position, bool):
        raise ChronicleError("invalid_event", "source end position is invalid")
    next_position: dict[str, JsonValue] = {
        "version": 1,
        "source_event_id": text_field(metadata, "source_event_id"),
        "file_id": text_field(metadata, "source_file_id"),
        "record_end_byte_exclusive": end_position,
        "cursor_codec_version": 1,
        "resume_token": request.source_resume_token,
        "non_resumable_reason": None if request.source_resume_token is not None else request.non_resumable_reason,
    }
    if request.source_resume_token is not None:
        commit_reason = "foreground_collector_resumable"
    elif request.source_adapter_id == "manual-file":
        commit_reason = "explicit_manual_capture_non_resumable"
    else:
        commit_reason = "explicit_pi_jsonl_import_non_resumable"
    return next_position, end_position, commit_reason
