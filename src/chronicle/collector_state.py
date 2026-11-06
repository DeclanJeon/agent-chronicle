"""Private durable operational state for foreground source collection."""

import hashlib
import json
import os
from pathlib import Path
import stat

from chronicle.atomic import atomic_replace, atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.models import CaptureContext, JsonValue
from chronicle.schema import parse_json_object
from chronicle.source_models import CollectorSnapshot, CoverageGap, SourceFileState

_STATE_DIR = "collector-state"


def _state_root(context: CaptureContext) -> Path:
    """Return the private operational state root."""
    return context.paths.state_dir / _STATE_DIR


def _file_state(path: Path) -> SourceFileState:
    """Parse one private source cursor file and verify its stable filename."""
    try:
        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "collector file state must be private and user-owned")
        entry = parse_json_object(path.read_text(encoding="utf-8"), "collector file state")
    except ChronicleError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ChronicleError("collector_state_corrupt", "collector file state cannot be read") from error
    version = entry.get("schema_version")
    status = entry.get("status")
    source_id = entry.get("source_id")
    member_id = entry.get("member_id")
    relative = entry.get("relative_path")
    file_id = entry.get("file_id")
    session_id = entry.get("session_id")
    prefix_hash = entry.get("prefix_sha256")
    if (
        not isinstance(version, int) or isinstance(version, bool) or version != 1
        or status not in ("ready", "partial", "backpressure", "failed", "missing")
        or not isinstance(source_id, str) or not isinstance(member_id, str)
        or not isinstance(relative, str) or not isinstance(file_id, str)
        or not isinstance(session_id, str) or not isinstance(prefix_hash, str)
        or member_id != path.stem
        or (prefix_hash and (len(prefix_hash) != 64 or any(char not in "0123456789abcdef" for char in prefix_hash)))
    ):
        raise ChronicleError("collector_state_corrupt", "collector file state fields are invalid")
    last_success = entry.get("last_success_at")
    last_error = entry.get("last_error")
    retry_after = entry.get("retry_after")
    if any(value is not None and not isinstance(value, str) for value in (last_success, last_error, retry_after)):
        raise ChronicleError("collector_state_corrupt", "collector nullable text field is invalid")
    last_success_text = last_success if isinstance(last_success, str) else None
    last_error_text = last_error if isinstance(last_error, str) else None
    retry_after_text = retry_after if isinstance(retry_after, str) else None
    integer_values: list[int] = []
    for key in ("device", "inode", "generation", "cursor", "size_bytes", "pending_bytes", "consecutive_failures"):
        value = entry.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ChronicleError("collector_state_corrupt", "collector integer field is invalid")
        integer_values.append(value)
    return SourceFileState(
        source_id, member_id, relative, file_id, integer_values[0], integer_values[1],
        integer_values[2], session_id, integer_values[3], prefix_hash,
        integer_values[4], integer_values[5], status, last_success_text, last_error_text,
        integer_values[6], retry_after_text,
    )


def _gap_state(path: Path) -> CoverageGap:
    """Parse one private coverage-gap record."""
    try:
        file_stat = path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "coverage gap must be private and user-owned")
        entry = parse_json_object(path.read_text(encoding="utf-8"), "coverage gap")
    except ChronicleError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ChronicleError("collector_state_corrupt", "coverage gap cannot be read") from error
    source_id = entry.get("source_id")
    member_id = entry.get("member_id")
    relative = entry.get("relative_path")
    reason = entry.get("reason")
    observed_at = entry.get("observed_at")
    end = entry.get("to_byte")
    if (
        not isinstance(entry.get("schema_version"), int)
        or isinstance(entry.get("schema_version"), bool)
        or entry.get("schema_version") != 1
        or not isinstance(source_id, str) or not isinstance(member_id, str)
        or not isinstance(relative, str) or not isinstance(reason, str)
        or not isinstance(observed_at, str)
        or (end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < 0))
    ):
        raise ChronicleError("collector_state_corrupt", "coverage gap fields are invalid")
    start = entry.get("from_byte")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise ChronicleError("collector_state_corrupt", "coverage gap start is invalid")
    return CoverageGap(source_id, member_id, relative, start, end, reason, observed_at)


def read_collector_state(context: CaptureContext) -> CollectorSnapshot:
    """Load typed collector files and gaps; absent state means empty."""
    root = _state_root(context)
    if not root.exists():
        return CollectorSnapshot(None, None, "never_run", (), ())
    if root.is_symlink() or not root.is_dir():
        raise ChronicleError("unsafe_path", "collector state directory is unsafe")
    meta_path = root / "meta.json"
    if not meta_path.exists():
        return CollectorSnapshot(None, None, "never_run", (), ())
    try:
        meta_stat = meta_path.lstat()
        if (
            not stat.S_ISREG(meta_stat.st_mode)
            or meta_stat.st_uid != os.getuid()
            or stat.S_IMODE(meta_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "collector metadata must be private and user-owned")
        meta = parse_json_object(meta_path.read_text(encoding="utf-8"), "collector metadata")
    except ChronicleError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ChronicleError("collector_state_corrupt", "collector metadata cannot be read") from error
    schema_version = meta.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ChronicleError("collector_state_corrupt", "collector state schema is unsupported")
    updated = meta.get("updated_at")
    last_run = meta.get("last_run_at")
    run_status = meta.get("last_run_status")
    if (
        (updated is not None and not isinstance(updated, str))
        or (last_run is not None and not isinstance(last_run, str))
        or not isinstance(run_status, str)
    ):
        raise ChronicleError("collector_state_corrupt", "collector metadata fields are invalid")
    files_root = root / "files"
    gaps_root = root / "gaps"
    for directory in (files_root, gaps_root):
        try:
            directory_stat = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ChronicleError("collector_state_corrupt", "collector state directory cannot be inspected") from error
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ChronicleError("unsafe_path", "collector state child directory is unsafe")
    files = tuple(_file_state(path) for path in sorted(files_root.glob("*.json"))) if files_root.exists() else ()
    gaps = tuple(_gap_state(path) for path in sorted(gaps_root.glob("*.json"))) if gaps_root.exists() else ()
    return CollectorSnapshot(updated, last_run, run_status, files, gaps)


def _file_value(item: SourceFileState) -> dict[str, JsonValue]:
    """Serialize one source cursor to an independent JSON object."""
    return {
        "schema_version": 1, "source_id": item.source_id,
        "member_id": item.member_id, "relative_path": item.relative_path,
        "file_id": item.file_id, "device": item.device, "inode": item.inode,
        "generation": item.generation, "session_id": item.session_id,
        "cursor": item.cursor, "prefix_sha256": item.prefix_sha256,
        "size_bytes": item.size_bytes, "pending_bytes": item.pending_bytes,
        "status": item.status, "last_success_at": item.last_success_at,
        "last_error": item.last_error, "consecutive_failures": item.consecutive_failures,
        "retry_after": item.retry_after,
    }


def _gap_value(item: CoverageGap) -> dict[str, JsonValue]:
    """Serialize one immutable coverage gap."""
    return {
        "schema_version": 1, "source_id": item.source_id,
        "member_id": item.member_id, "relative_path": item.relative_path,
        "from_byte": item.from_byte, "to_byte": item.to_byte,
        "reason": item.reason, "observed_at": item.observed_at,
    }


def _write_flat(path: Path, value: dict[str, JsonValue]) -> None:
    """Atomically write one independent private JSON state file."""
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if path.exists():
        atomic_replace(path, content)
    else:
        atomic_write_new(path, content)


def write_collector_state(context: CaptureContext, state: CollectorSnapshot) -> None:
    """Durably publish cursor, gap, and run metadata after source ACKs."""
    root = _state_root(context)
    files_root = root / "files"
    gaps_root = root / "gaps"
    ensure_private_dir(files_root)
    ensure_private_dir(gaps_root)
    for item in state.files:
        _write_flat(files_root / f"{item.member_id}.json", _file_value(item))
    for item in state.gaps:
        gap_id = hashlib.sha256(json.dumps(
            _gap_value(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        _write_flat(gaps_root / f"{gap_id}.json", _gap_value(item))
    _write_flat(root / "meta.json", {
        "schema_version": 1,
        "updated_at": state.updated_at,
        "last_run_at": state.last_run_at,
        "last_run_status": state.last_run_status,
    })
