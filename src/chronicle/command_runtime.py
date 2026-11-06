"""Shared local command context and structured output helpers."""

import json

from chronicle.destination import verify_vault
from chronicle.models import CaptureContext, JsonValue
from chronicle.paths import (
    environment_paths,
    load_config,
    validate_store_layout,
    validate_store_roots,
)


def json_output(value: JsonValue) -> None:
    """Write structured output with JSON escaping so values stay inert."""
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def load_context() -> CaptureContext:
    """Load installation settings and revalidate destination ownership."""
    paths = environment_paths()
    validate_store_roots(paths)
    config = load_config(paths)
    validate_store_layout(config.vault, paths)
    _ = verify_vault(config.vault, config.host_id, config.source_instance_id)
    return CaptureContext(config, paths)
