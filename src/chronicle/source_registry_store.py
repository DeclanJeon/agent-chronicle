"""Strict, private persistence for explicit source registrations."""

import json
import os
from pathlib import Path
import stat
from typing import Literal
import uuid

from chronicle.atomic import atomic_replace, atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.models import CaptureContext, JsonValue
from chronicle.privacy import reject_sensitive_metadata
from chronicle.schema import parse_json_object
from chronicle.source_models import SourceRegistration

REGISTRY_DIR = "sources"
ADAPTER_ID = "pi-jsonl-v3"
POLICY_VERSION = "redact-omit-nontext-v1"
_REGISTRATION_FIELDS = frozenset({
    "schema_version", "source_id", "adapter_id", "adapter_version", "policy_version",
    "scope_kind", "scope_path", "project_id", "title", "enabled", "created_at",
})


def parse_scope_kind(value: str) -> Literal["file", "root"]:
    """Parse a persisted scope into the supported file/root variants."""
    if value == "file":
        return "file"
    if value == "root":
        return "root"
    raise ChronicleError("invalid_source_registry", "registry scope kind is unsupported")


def _registration(path: Path) -> SourceRegistration:
    """Parse and validate one private per-source registration file."""
    try:
        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "source registry entry must be private and user-owned")
        entry = parse_json_object(path.read_text(encoding="utf-8"), "source registry entry")
    except ChronicleError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ChronicleError("source_registry_error", "source registry entry cannot be read") from error
    if frozenset(entry) != _REGISTRATION_FIELDS:
        raise ChronicleError("invalid_source_registry", "registry entry fields are unsupported")
    schema_version = entry.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise ChronicleError("invalid_source_registry", "registry schema version is unsupported")

    def read_text(key: str) -> str:
        """Read one required string from this flat registry entry."""
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            raise ChronicleError("invalid_source_registry", "registry string field is invalid")
        return value

    enabled = entry.get("enabled")
    if not isinstance(enabled, bool):
        raise ChronicleError("invalid_source_registry", "registry enabled flag is invalid")
    scope_value = parse_scope_kind(read_text("scope_kind"))
    adapter_id = read_text("adapter_id")
    adapter_version = read_text("adapter_version")
    policy_version = read_text("policy_version")
    if (adapter_id, adapter_version, policy_version) != (ADAPTER_ID, "3", POLICY_VERSION):
        raise ChronicleError("invalid_source_registry", "registry parser or policy version is unsupported")
    source_id = read_text("source_id")
    try:
        if str(uuid.UUID(source_id)) != source_id:
            raise ValueError
    except ValueError as error:
        raise ChronicleError("invalid_source_registry", "registry source identity is invalid") from error
    path_value = read_text("scope_path")
    if not Path(path_value).is_absolute():
        raise ChronicleError("invalid_source_registry", "registry scope path must be absolute")
    project_id = read_text("project_id")
    title = read_text("title")
    created_at = read_text("created_at")
    reject_sensitive_metadata(source_id, project_id, title)
    if path.stem != source_id:
        raise ChronicleError("invalid_source_registry", "registry filename differs from source ID")
    return SourceRegistration(
        source_id, adapter_id, adapter_version, policy_version, scope_value,
        Path(path_value), project_id, title, enabled, created_at,
    )


def _source_directory(context: CaptureContext) -> Path:
    """Return the private per-source registry directory."""
    return context.paths.config_dir / REGISTRY_DIR


def read_registry(context: CaptureContext) -> tuple[SourceRegistration, ...]:
    """Read registrations; a missing directory means all sources are OFF."""
    root = _source_directory(context)
    try:
        directory_stat = root.lstat()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise ChronicleError("source_registry_error", "source registry directory cannot be inspected") from error
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise ChronicleError("unsafe_source_registry", "source registry directory is unsafe")
    try:
        paths = sorted(root.iterdir())
    except OSError as error:
        raise ChronicleError("source_registry_error", "source registry directory cannot be read") from error
    if any(path.suffix != ".json" or path.is_symlink() or not path.is_file() for path in paths):
        raise ChronicleError("unsafe_source_registry", "source registry contains an unsupported entry")
    return tuple(_registration(path) for path in paths)


def _serialize(source: SourceRegistration) -> bytes:
    """Render one flat source registration deterministically."""
    value: dict[str, JsonValue] = {
        "schema_version": 1,
        "source_id": source.source_id,
        "adapter_id": source.adapter_id,
        "adapter_version": source.adapter_version,
        "policy_version": source.policy_version,
        "scope_kind": source.scope_kind,
        "scope_path": str(source.scope_path),
        "project_id": source.project_id,
        "title": source.title,
        "enabled": source.enabled,
        "created_at": source.created_at,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_registration(context: CaptureContext, source: SourceRegistration) -> None:
    """Atomically publish one private source registration."""
    root = _source_directory(context)
    ensure_private_dir(root)
    path = root / f"{source.source_id}.json"
    content = _serialize(source)
    if path.exists():
        atomic_replace(path, content)
    else:
        atomic_write_new(path, content)
