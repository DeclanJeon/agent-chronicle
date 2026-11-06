"""Explicit local source opt-in and safe registry operations."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
from pathlib import Path
import stat
from typing import Literal
import uuid

from chronicle.atomic import ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.models import CaptureContext, JsonValue
from chronicle.privacy import reject_sensitive_metadata
from chronicle.source_models import SourceRegistration
from chronicle.source_registry_store import (
    ADAPTER_ID,
    POLICY_VERSION,
    parse_scope_kind,
    read_registry,
    write_registration,
)


@contextmanager
def _registry_lock(context: CaptureContext) -> Generator[None, None, None]:
    """Serialize short registry read-modify-write operations."""
    root = context.paths.config_dir
    ensure_private_dir(root)
    lock_path = root / "sources.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise ChronicleError("source_registry_error", "source registry lock cannot be opened") from error
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "source registry lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise ChronicleError("source_registry_error", "source registry lock failed") from error
        yield
    finally:
        os.close(descriptor)


def _validate_scope(
    context: CaptureContext,
    kind: Literal["file", "root"],
    selected: Path,
) -> Path:
    """Resolve one explicit scope, rejecting sensitive paths, symlinks, and Chronicle stores."""
    reject_sensitive_metadata(str(selected))
    expanded = selected.expanduser().absolute()
    if expanded.is_symlink():
        raise ChronicleError("unsafe_input", "source scope may not be a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ChronicleError("invalid_input", "source scope cannot be resolved") from error
    if resolved != expanded:
        raise ChronicleError("unsafe_input", "source scope path may not traverse symbolic links")
    reject_sensitive_metadata(str(resolved))
    try:
        mode = resolved.lstat().st_mode
    except OSError as error:
        raise ChronicleError("input_error", "source scope cannot be inspected") from error
    if kind == "file" and not stat.S_ISREG(mode):
        raise ChronicleError("invalid_input", "source file must be a regular file")
    if kind == "root" and not stat.S_ISDIR(mode):
        raise ChronicleError("invalid_input", "source root must be a directory")
    if resolved == Path.home().resolve() or resolved == Path(resolved.anchor):
        raise ChronicleError("unsafe_input", "home and filesystem roots cannot be source scopes")
    protected = (
        context.config.vault.absolute(), context.paths.config_dir.absolute(),
        context.paths.state_dir.absolute(), context.paths.cache_dir.absolute(),
    )
    for root in protected:
        if resolved == root or resolved in root.parents or root in resolved.parents:
            raise ChronicleError("unsafe_input", "source scope overlaps Chronicle storage")
    return resolved


def enable_source(
    context: CaptureContext,
    source: str,
    selected_path: Path,
    scope_kind: str,
    project_id: str,
    title: str,
) -> SourceRegistration:
    """Explicitly register and enable one version-pinned source scope."""
    if source != ADAPTER_ID:
        raise ChronicleError("unsupported_source", "only Pi JSONL v3 is supported by the collector")
    scope_variant = parse_scope_kind(scope_kind)
    project = project_id.strip()
    label = title.strip()
    reject_sensitive_metadata(project, label)
    if not project or len(project) > 128 or not label or len(label) > 256:
        raise ChronicleError("invalid_input", "project and title must be bounded non-empty text")
    resolved = _validate_scope(context, scope_variant, selected_path)
    registration = SourceRegistration(
        str(uuid.uuid4()), ADAPTER_ID, "3", POLICY_VERSION, scope_variant, resolved,
        project, label, True, datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    with _registry_lock(context):
        entries = read_registry(context)
        for existing in entries:
            if existing.scope_path == resolved and existing.scope_kind == scope_variant:
                raise ChronicleError("source_exists", "source scope is already registered")
        write_registration(context, registration)
    return registration


def change_source_enabled(context: CaptureContext, source_id: str, enabled: bool) -> SourceRegistration:
    """Enable or disable a registered source without removing its history."""
    try:
        if str(uuid.UUID(source_id)) != source_id:
            raise ValueError
    except ValueError as error:
        raise ChronicleError("invalid_input", "source ID must be a UUID") from error
    with _registry_lock(context):
        entries = read_registry(context)
        matches = [item for item in entries if item.source_id == source_id]
        if len(matches) != 1:
            raise ChronicleError("source_not_found", "source ID is not registered")
        previous = matches[0]
        changed = SourceRegistration(
            previous.source_id, previous.adapter_id, previous.adapter_version,
            previous.policy_version, previous.scope_kind, previous.scope_path,
            previous.project_id, previous.title, enabled, previous.created_at,
        )
        write_registration(context, changed)
    return changed


def list_sources(context: CaptureContext) -> tuple[SourceRegistration, ...]:
    """Return source registrations; no source is enabled unless explicitly chosen."""
    return read_registry(context)


def source_as_json(source: SourceRegistration) -> dict[str, JsonValue]:
    """Render one registry entry without exposing its local filesystem path."""
    return {
        "source_id": source.source_id,
        "adapter": source.adapter_id,
        "version": source.adapter_version,
        "policy_version": source.policy_version,
        "scope_kind": source.scope_kind,
        "scope": "[file scope withheld]" if source.scope_kind == "file" else "[root scope withheld]",
        "project_id": source.project_id,
        "title": source.title,
        "enabled": source.enabled,
        "created_at": source.created_at,
    }
