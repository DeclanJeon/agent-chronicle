"""Small typed readers for validated Markdown metadata."""

import json
from typing import cast

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at configuration/manifest JSON boundaries."""
    mapping: dict[str, object] = {}
    for key, value in pairs:
        if key in mapping:
            raise ChronicleError("duplicate_json_key", f"duplicate JSON key: {key}")
        mapping[key] = value
    return mapping


def parse_json_object(source: str, label: str) -> dict[str, object]:
    """Decode one JSON object and contain stdlib's untyped return at its boundary."""
    try:
        decoded = cast(
            object,
            json.loads(source, object_pairs_hook=_strict_json_pairs),
        )
    except json.JSONDecodeError as error:
        raise ChronicleError("invalid_json", f"{label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ChronicleError("invalid_json", f"{label} must be a JSON object")
    untyped_mapping = cast(dict[object, object], decoded)
    mapping: dict[str, object] = {}
    for key, value in untyped_mapping.items():
        if not isinstance(key, str):
            raise ChronicleError("invalid_json", f"{label} keys must be strings")
        mapping[key] = value
    return mapping


def text_field(metadata: dict[str, JsonValue], name: str) -> str:
    """Read one required string metadata field."""
    value = metadata.get(name)
    if not isinstance(value, str):
        raise ChronicleError("invalid_metadata", f"{name} must be a string")
    return value


def optional_text(metadata: dict[str, JsonValue], name: str) -> str | None:
    """Read one nullable string metadata field."""
    value = metadata.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChronicleError("invalid_metadata", f"{name} must be a string or null")
    return value


def integer_field(metadata: dict[str, JsonValue], name: str) -> int:
    """Read one required integer metadata field, excluding booleans."""
    value = metadata.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChronicleError("invalid_metadata", f"{name} must be an integer")
    return value


def mapping_field(metadata: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    """Read one required mapping metadata field."""
    value = metadata.get(name)
    if not isinstance(value, dict):
        raise ChronicleError("invalid_metadata", f"{name} must be a mapping")
    return value


def list_field(metadata: dict[str, JsonValue], name: str) -> list[JsonValue]:
    """Read one required list metadata field."""
    value = metadata.get(name)
    if not isinstance(value, list):
        raise ChronicleError("invalid_metadata", f"{name} must be a list")
    return value
