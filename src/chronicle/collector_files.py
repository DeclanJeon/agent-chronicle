"""Bounded filesystem reconciliation and stable Pi source identities."""

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
from typing import BinaryIO

from chronicle.errors import ChronicleError
from chronicle.privacy import redact_text
from chronicle.source_models import SourceRegistration

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS_PER_FILE_PASS = 500
MAX_MATCHED_FILES = 1000
_ROTATED_NAME = re.compile(r".+\.jsonl\.\d+")


def now() -> datetime:
    """Return local aware time for operational metadata."""
    return datetime.now().astimezone()


def _is_record_name(name: str) -> bool:
    """Accept Pi session exports and numeric rotations, not arbitrary files."""
    return name.endswith(".jsonl") or _ROTATED_NAME.fullmatch(name) is not None


def scan_source_paths(source: SourceRegistration) -> tuple[Path, ...]:
    """Enumerate only explicitly selected Pi JSONL files; never follow symlinks."""
    root = source.scope_path
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ChronicleError("unsafe_input", "registered source scope cannot be resolved") from error
    if root.is_symlink() or resolved_root != root:
        raise ChronicleError("unsafe_input", "registered source scope changed through a symbolic link")
    paths: list[Path] = []
    walk_errors: list[OSError] = []

    def on_error(error: OSError) -> None:
        walk_errors.append(error)

    match source.scope_kind:  # noqa: MATCH_OK — source scope is a closed Literal
        case "file":
            paths.append(root)
            for candidate in root.parent.glob(f"{root.name}.*"):
                if _ROTATED_NAME.fullmatch(candidate.name) is not None:
                    paths.append(candidate)
        case "root":
            for current, directories, files in os.walk(root, followlinks=False, onerror=on_error):
                current_path = Path(current)
                directories[:] = [
                    name for name in directories if not (current_path / name).is_symlink()
                ]
                paths.extend(current_path / name for name in files if _is_record_name(name))
    if walk_errors:
        raise ChronicleError("source_scan_error", "selected source root contains an unreadable directory")
    unique = tuple(sorted(set(paths)))
    if len(unique) > MAX_MATCHED_FILES:
        raise ChronicleError("source_limit", "selected source contains too many matching files")
    return unique


def relative_source_path(source: SourceRegistration, path: Path) -> str:
    """Return a path relative to the explicit source scope."""
    match source.scope_kind:  # noqa: MATCH_OK — source scope is a closed Literal
        case "file":
            return redact_text(path.name)
        case "root":
            try:
                relative = path.relative_to(source.scope_path).as_posix()
            except ValueError as error:
                raise ChronicleError("unsafe_input", "source file escaped its registered root") from error
            return redact_text(relative)


def source_member_id(source_id: str, device: int, inode: int) -> str:
    """Create a non-path stable identity for one registered filesystem inode."""
    value = f"agent-chronicle/pi-member/v1\0{source_id}\0{device}\0{inode}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_id(member_id: str, generation: int) -> str:
    """Bind a source file generation to its stable member identity."""
    return hashlib.sha256(f"{member_id}\0{generation}".encode("utf-8")).hexdigest()


def stream_id(source_id: str, member_id: str, generation: int) -> str:
    """Return one checkpoint stream per registered file generation."""
    return f"chronicle-pi-v1:{source_id}:{member_id}:g{generation}"


def prefix_digest(stream: BinaryIO, length: int) -> str:
    """Hash exactly the committed prefix, detecting same-inode rewrites."""
    _ = stream.seek(0)
    remaining = length
    digest = hashlib.sha256()
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise ChronicleError("source_changed", "source became shorter than its committed cursor")
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()
