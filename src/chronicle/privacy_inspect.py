"""Explicit local privacy scan that never returns source lines or values."""

import os
from pathlib import Path
import stat

from chronicle.errors import ChronicleError
from chronicle.models import CaptureContext, JsonValue
from chronicle.privacy import normalize_text, redact_text

_MAX_INSPECT_BYTES = 10 * 1024 * 1024


def inspect_text_file(context: CaptureContext, selected_path: Path) -> dict[str, JsonValue]:
    """Inspect one user-selected UTF-8 file and report only line-number metadata."""
    absolute = selected_path.expanduser().absolute()
    if absolute.is_symlink():
        raise ChronicleError("unsafe_input", "privacy inspection does not follow symbolic links")
    try:
        resolved = absolute.resolve(strict=True)
        if resolved != absolute:
            raise ChronicleError("unsafe_input", "privacy inspection path traverses a symbolic link")
        internal_stores = (
            context.paths.config_dir.absolute(),
            context.paths.state_dir.absolute(),
            context.paths.cache_dir.absolute(),
        )
        if any(resolved == root or root in resolved.parents for root in internal_stores):
            raise ChronicleError("unsafe_input", "privacy inspection cannot read Chronicle internal stores")
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
    except ChronicleError:
        raise
    except OSError as error:
        raise ChronicleError("input_error", "selected privacy file cannot be opened") from error
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid():
            raise ChronicleError("unsafe_input", "privacy inspection requires a user-owned regular file")
        if entry.st_size > _MAX_INSPECT_BYTES:
            raise ChronicleError("input_too_large", "privacy inspection file exceeds the 10 MiB limit")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_INSPECT_BYTES + 1)
    except OSError as error:
        raise ChronicleError("input_error", "selected privacy file cannot be read") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_INSPECT_BYTES:
        raise ChronicleError("input_too_large", "privacy inspection file exceeds the 10 MiB limit")
    try:
        source = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChronicleError("invalid_utf8", "privacy inspection requires strict UTF-8") from error
    if source.startswith("\ufeff"):
        raise ChronicleError("invalid_bom", "privacy inspection does not accept a UTF-8 BOM")
    normalized = normalize_text(source)
    sensitive_lines: list[JsonValue] = []
    lines = normalized.splitlines()
    for line_number, line in enumerate(lines, start=1):
        if redact_text(line) != line:
            sensitive_lines.append(line_number)
    return {
        "status": "patterns_found" if sensitive_lines else "no_recognized_patterns",
        "policy_version": "redact-omit-nontext-v1",
        "file_bytes": len(raw),
        "line_count": len(lines),
        "sensitive_line_count": len(sensitive_lines),
        "sensitive_line_numbers": sensitive_lines,
        "source_values_returned": False,
        "external_delivery": False,
        "limitations": "pattern-based local inspection is not a general DLP guarantee",
    }
