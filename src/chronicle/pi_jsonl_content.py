"""Typed message extraction and privacy handling for Pi JSONL v3."""

from datetime import datetime, timezone
import json
import math
import re

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.naming import safe_slug
from chronicle.pi_jsonl_models import PiRecord
from chronicle.privacy import redact_text, reject_sensitive_metadata

_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie|session(?:[_-]?(?:id|token|key))?)"
)


def mapping(value: JsonValue) -> dict[str, JsonValue] | None:
    """Narrow one JSON value to an object after JSON decoding."""
    if isinstance(value, dict):
        return value
    return None


def required_text(value: dict[str, JsonValue], name: str) -> str:
    """Read a bounded required string without echoing untrusted values."""
    field = value.get(name)
    if not isinstance(field, str) or not field or len(field) > 512:
        raise ChronicleError("invalid_pi_session", "session record has an invalid required field")
    return field


def _redact_json(value: JsonValue, key: str | None = None) -> JsonValue:
    """Mask structured tool arguments before rendering them into imported text."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, dict):
        return {
            child_key: _redact_json(item, child_key)
            for child_key, item in value.items()
        }
    return value


def _json_text(value: JsonValue) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise ChronicleError("invalid_pi_session", "tool arguments are not supported JSON") from error


def _text_blocks(value: JsonValue) -> tuple[list[str], list[str], int]:
    """Return text and tool-call blocks, deliberately excluding thinking and images."""
    if isinstance(value, str):
        return ([value] if value else []), [], 0
    if not isinstance(value, list):
        raise ChronicleError("invalid_pi_session", "message content has an unsupported shape")
    text: list[str] = []
    calls: list[str] = []
    omitted = 0
    for item in value:
        block = mapping(item)
        if block is None:
            raise ChronicleError("invalid_pi_session", "message content block is invalid")
        kind = block.get("type")
        if kind == "text":
            content = block.get("text")
            if not isinstance(content, str):
                raise ChronicleError("invalid_pi_session", "text content block is invalid")
            if content:
                text.append(content)
        elif kind == "toolCall":
            call_id = required_text(block, "id")
            name = required_text(block, "name")
            reject_sensitive_metadata(call_id, name)
            arguments = mapping(block.get("arguments"))
            if arguments is None:
                raise ChronicleError("invalid_pi_session", "tool-call arguments must be an object")
            try:
                safe_arguments = _redact_json(arguments)
            except RecursionError as error:
                raise ChronicleError(
                    "invalid_pi_session", "tool arguments exceed the supported nesting depth"
                ) from error
            calls.append(f"Tool call {call_id}: {name}\nArguments: {_json_text(safe_arguments)}")
        elif kind in ("thinking", "image"):
            omitted += 1
        else:
            # Future content blocks are not guessed at; the owning session version is pinned.
            omitted += 1
    return text, calls, omitted


def _occurred_at(message: dict[str, JsonValue], entry: dict[str, JsonValue]) -> str:
    timestamp = message.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and math.isfinite(timestamp):
        try:
            instant = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise ChronicleError(
                "invalid_pi_session", "message timestamp is outside the supported range"
            ) from error
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fallback = entry.get("timestamp")
    if isinstance(fallback, str):
        try:
            instant = datetime.fromisoformat(fallback.replace("Z", "+00:00"))
        except ValueError as error:
            raise ChronicleError("invalid_pi_session", "message timestamp is invalid") from error
        if instant.tzinfo is not None:
            return instant.isoformat(timespec="milliseconds")
    raise ChronicleError("invalid_pi_session", "message timestamp is missing")


def parse_message_entry(entry: dict[str, JsonValue], start: int, end: int) -> PiRecord | None:
    """Map one versioned session entry into safe text and stable source locators."""
    if entry.get("type") != "message":
        return None
    entry_id = required_text(entry, "id")
    reject_sensitive_metadata(entry_id)
    if "parentId" not in entry:
        raise ChronicleError("invalid_pi_session", "message parent identity is missing")
    parent_id = entry.get("parentId")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ChronicleError("invalid_pi_session", "message parent identity is invalid")
    if isinstance(parent_id, str):
        reject_sensitive_metadata(parent_id)
    message = mapping(entry.get("message"))
    if message is None:
        raise ChronicleError("invalid_pi_session", "message entry has an invalid payload")
    role = message.get("role")
    if not isinstance(role, str):
        raise ChronicleError("invalid_pi_session", "message role is invalid")
    if role == "system":
        return None

    event_type: str
    actor_id: str
    content: str
    source_call_id: str | None = None
    omitted = 0
    if role in ("user", "assistant"):
        text, calls, omitted = _text_blocks(message.get("content"))
        segments = text + calls
        if not segments:
            return None
        content = "\n\n".join(segments)
        if role == "user":
            event_type, actor_id = "user_message", "user"
        elif calls and not text:
            event_type, actor_id = "tool_call", "assistant"
        else:
            event_type, actor_id = "assistant_message", "assistant"
    elif role == "toolResult":
        name = message.get("toolName")
        if not isinstance(name, str) or not name:
            raise ChronicleError("invalid_pi_session", "tool result name is invalid")
        call_id = message.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            raise ChronicleError("invalid_pi_session", "tool result call identity is invalid")
        reject_sensitive_metadata(call_id)
        text, _calls, omitted = _text_blocks(message.get("content"))
        if not text:
            return None
        error_state = message.get("isError") is True
        event_type = "tool_error" if error_state else "tool_result"
        actor_id = f"tool:{safe_slug(name, 64)}"
        source_call_id = call_id
        prefix = "Tool error" if error_state else "Tool result"
        content = f"{prefix} {call_id} ({name}):\n" + "\n\n".join(text)
    elif role == "bashExecution":
        command, output = message.get("command"), message.get("output")
        if not isinstance(command, str) or not isinstance(output, str):
            raise ChronicleError("invalid_pi_session", "bash execution content is invalid")
        if not command and not output:
            return None
        event_type, actor_id = "bash_execution", "tool:bash"
        details: list[str] = []
        exit_code = message.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            details.append(f"Exit code: {exit_code}")
        cancelled = message.get("cancelled")
        truncated = message.get("truncated")
        if isinstance(cancelled, bool):
            details.append(f"Cancelled: {str(cancelled).lower()}")
        if isinstance(truncated, bool):
            details.append(f"Truncated: {str(truncated).lower()}")
        suffix = "\n".join(details)
        content = f"Command:\n{command}\n\nOutput:\n{output}"
        if suffix:
            content += f"\n\n{suffix}"
    elif role == "custom":
        text, _calls, omitted = _text_blocks(message.get("content"))
        if not text:
            return None
        custom_type = message.get("customType")
        if not isinstance(custom_type, str) or not custom_type:
            custom_type = "custom"
        event_type, actor_id = "custom_message", f"custom:{safe_slug(custom_type, 64)}"
        content = "\n\n".join(text)
    elif role in ("branchSummary", "compactionSummary"):
        summary = message.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        event_type = "branch_summary" if role == "branchSummary" else "compaction_summary"
        actor_id, content = "assistant", summary
    else:
        return None

    reject_sensitive_metadata(actor_id, event_type)
    return PiRecord(
        entry_id,
        parent_id,
        source_call_id,
        event_type,
        actor_id,
        _occurred_at(message, entry),
        content,
        start,
        end,
        omitted,
    )
