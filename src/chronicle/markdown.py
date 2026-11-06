"""Strict Markdown frontmatter parsing and deterministic rendering."""

# PyYAML ships no bundled stubs; this file contains its untyped boundary and
# validates every decoded value before it enters the typed application model.
# pyright: reportAny=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import hashlib
import math
from collections.abc import Hashable, Mapping
import unicodedata
from typing import cast, override

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.privacy import normalize_text

_DIGEST_DOMAIN = b"agent-chronicle/knowledge-approval/v1\0"


class StrictSafeLoader(yaml.SafeLoader):
    """Safe loader rejecting duplicate keys, non-string keys, and aliases."""

    @override
    def compose_node(self, parent: Node | None, index: int) -> Node:
        if self.check_event(AliasEvent):
            raise ChronicleError("invalid_yaml", "YAML aliases are unsupported")
        node = super().compose_node(parent, index)
        if node is None:
            raise ChronicleError("invalid_yaml", "YAML node could not be composed")
        return node

    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        mapping: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            key = cast(object, self.construct_object(key_node, deep=deep))
            if not isinstance(key, str):
                raise ChronicleError("invalid_yaml", "frontmatter keys must be strings")
            if key in mapping:
                raise ChronicleError("duplicate_yaml_key", f"duplicate key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _to_json_value(value: object) -> JsonValue:
    """Restrict safe YAML output to JSON-compatible data values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ChronicleError("invalid_yaml", "frontmatter floats must be finite")
        return value
    if isinstance(value, list):
        values = cast(list[object], value)
        return [_to_json_value(item) for item in values]
    if isinstance(value, dict):
        entries = cast(dict[object, object], value)
        result: dict[str, JsonValue] = {}
        for key, item in entries.items():
            if not isinstance(key, str):
                raise ChronicleError("invalid_yaml", "frontmatter keys must be strings")
            result[key] = _to_json_value(item)
        return result
    raise ChronicleError("invalid_yaml", "frontmatter contains an unsupported value")


def parse_frontmatter(markdown: bytes) -> tuple[dict[str, JsonValue], str, str]:
    """Decode strict UTF-8 Markdown and parse one safe YAML frontmatter block."""
    try:
        decoded = markdown.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChronicleError("invalid_utf8", "Markdown must be strict UTF-8") from error
    if decoded.startswith("\ufeff"):
        raise ChronicleError("invalid_bom", "UTF-8 BOM is not accepted")
    normalized = normalize_text(decoded)
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        raise ChronicleError("invalid_frontmatter", "opening --- delimiter is required")
    closing_index = next(
        (index for index in range(1, len(lines)) if lines[index].rstrip("\n") == "---"),
        -1,
    )
    if closing_index < 0:
        raise ChronicleError("invalid_frontmatter", "closing --- delimiter is required")
    source = "".join(lines[1:closing_index])
    try:
        loaded = yaml.load(source, Loader=StrictSafeLoader)
    except ChronicleError:
        raise
    except yaml.YAMLError as error:
        raise ChronicleError("invalid_yaml", "frontmatter is not safe supported YAML") from error
    value = _to_json_value(loaded)
    if not isinstance(value, dict):
        raise ChronicleError("invalid_frontmatter", "frontmatter must be a mapping")
    body = "".join(lines[closing_index + 1 :])
    return value, body, normalized


def render_markdown(metadata: Mapping[str, JsonValue], body: str) -> str:
    """Serialize typed metadata as safe YAML and preserve the supplied body."""
    try:
        frontmatter = yaml.safe_dump(
            dict(metadata),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=100000,
        )
    except yaml.YAMLError as error:
        raise ChronicleError("invalid_frontmatter", "metadata cannot be serialized") from error
    return f"---\n{frontmatter}---\n{body}"


def approval_digest(markdown: bytes, version: int) -> str:
    """Compute the v1 digest over normalized complete Markdown, including metadata."""
    if version != 1:
        raise ChronicleError("unknown_digest_version", f"unsupported digest version: {version}")
    _, _, normalized = parse_frontmatter(markdown)
    normalized_bytes = unicodedata.normalize("NFC", normalized).encode("utf-8", errors="strict")
    return hashlib.sha256(_DIGEST_DOMAIN + normalized_bytes).hexdigest()
