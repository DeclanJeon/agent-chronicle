"""Private Markdown state, validation, and per-job locking for distillation."""

from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import cast
import uuid

from chronicle.atomic import atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.knowledge_job_identity import job_id, job_root
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureContext, JsonValue
from chronicle.naming import sha256_text
from chronicle.schema import text_field


def _read_state(path: Path) -> tuple[dict[str, JsonValue], str]:
    """Read one bounded, private immutable state record without following links."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ChronicleError("unsafe_path", "distillation job state cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 128 * 1024
        ):
            raise ChronicleError("unsafe_path", "distillation job state is unsafe")
        chunks: list[bytes] = []
        remaining = 128 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > 128 * 1024:
            raise ChronicleError("unsafe_path", "distillation job state exceeds its size limit")
    finally:
        os.close(descriptor)
    metadata, body, _normalized = parse_frontmatter(raw)
    return metadata, body


def verify_private_directory(path: Path) -> None:
    """Require an existing local job directory to remain private and user-owned."""
    if path.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge job directory is a symbolic link")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ChronicleError("unsafe_permissions", "knowledge job directory must be private")


def _state_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash one state event without including its own digest field."""
    payload = {key: value for key, value in metadata.items() if key != "state_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def job_history(vault: Path, selected_job_id: str) -> list[tuple[Path, dict[str, JsonValue]]]:
    """Read and verify one linear immutable job-state chain."""
    system = vault / "90_System"
    root = system / "KnowledgeJobs"
    if system.is_symlink() or root.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge job directory is a symbolic link")
    if system.exists():
        verify_private_directory(system)
    folder = job_root(vault, selected_job_id)
    if not root.exists():
        return []
    verify_private_directory(root)
    if folder.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge job path is a symbolic link")
    if not folder.exists():
        return []
    verify_private_directory(folder)
    files = sorted(folder.iterdir())
    if len(files) > 10_000:
        raise ChronicleError("invalid_distill_job", "distillation job history exceeds its record limit")
    if any(path.suffix != ".md" for path in files):
        raise ChronicleError("invalid_distill_job", "distillation job contains a non-Markdown entry")
    records: list[tuple[Path, dict[str, JsonValue]]] = []
    for path in files:
        metadata, body = _read_state(path)
        schema_version = metadata.get("schema_version")
        sequence = metadata.get("sequence")
        attempt = metadata.get("attempt")
        status = text_field(metadata, "status")
        input_digest = text_field(metadata, "input_digest")
        session_id = text_field(metadata, "session_id")
        source_event_ids_value = metadata.get("source_event_ids")
        candidate_ids_value = metadata.get("candidate_ids")
        error_code = metadata.get("error_code")
        if not isinstance(source_event_ids_value, list) or not isinstance(candidate_ids_value, list):
            raise ChronicleError("invalid_distill_job", "distillation job ID lists are malformed")
        source_event_ids = [item for item in source_event_ids_value if isinstance(item, str)]
        candidate_ids = [item for item in candidate_ids_value if isinstance(item, str)]
        if len(source_event_ids) != len(source_event_ids_value) or len(candidate_ids) != len(candidate_ids_value):
            raise ChronicleError("invalid_distill_job", "distillation job ID lists contain non-text values")
        event_id = text_field(metadata, "job_event_id")
        identity = json.dumps([
            selected_job_id, sequence, status, attempt, input_digest, source_event_ids,
            candidate_ids, error_code,
        ], separators=(",", ":"))
        expected_event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-chronicle/distill-state/v1/{identity}"))
        expected_body = f"# Distillation job {status}\n\nThis state records rule-based local work only.\n"
        if (
            not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1
            or text_field(metadata, "kind") != "knowledge_distill_job"
            or text_field(metadata, "job_id") != selected_job_id
            or event_id != expected_event_id
            or body != expected_body
            or text_field(metadata, "job_event_id") != path.stem.split("_", 1)[-1]
            or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1
            or not path.name.startswith(f"{sequence:06d}_")
            or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
            or status not in {"pending", "failed", "completed"}
            or len(input_digest) != 64 or any(ch not in "0123456789abcdef" for ch in input_digest)
            or job_id(session_id, input_digest) != selected_job_id
            or not source_event_ids or source_event_ids != sorted(set(source_event_ids))
            or candidate_ids != sorted(set(candidate_ids))
            or (status == "pending" and candidate_ids)
            or (status == "completed" and not candidate_ids)
            or (status == "failed" and not isinstance(error_code, str))
            or (status != "failed" and error_code is not None)
            or text_field(metadata, "state_digest") != _state_digest(metadata, body)
        ):
            raise ChronicleError("invalid_distill_job", "distillation job state identity or digest is invalid")
        records.append((path, metadata))
    previous: str | None = None
    previous_status: str | None = None
    previous_attempt = 0
    for index, (_path, metadata) in enumerate(records, start=1):
        status = text_field(metadata, "status")
        attempt = metadata.get("attempt")
        if (
            metadata.get("sequence") != index
            or (index > 1 and (
                metadata.get("session_id") != records[0][1].get("session_id")
                or metadata.get("input_digest") != records[0][1].get("input_digest")
                or metadata.get("source_event_ids") != records[0][1].get("source_event_ids")
            ))
            or metadata.get("previous_job_event_id") != previous
            or (index == 1 and (status != "pending" or attempt != 1))
            or (previous_status == "pending" and (status not in {"failed", "completed"} or attempt != previous_attempt))
            or (previous_status == "failed" and (status != "pending" or attempt != previous_attempt + 1))
            or previous_status == "completed"
        ):
            raise ChronicleError("invalid_distill_job", "distillation job state chain is invalid")
        previous = text_field(metadata, "job_event_id")
        previous_status = status
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            previous_attempt = attempt
    return records


def append_job_state(
    vault: Path,
    selected_job_id: str,
    session_id: str,
    input_digest: str,
    source_event_ids: list[str],
    status: str,
    attempt: int,
    candidate_ids: list[str],
    error_code: str | None = None,
) -> tuple[Path, dict[str, JsonValue]]:
    """Append one deterministic immutable pending, failed, or completed state."""
    history = job_history(vault, selected_job_id)
    previous_id = text_field(history[-1][1], "job_event_id") if history else None
    sequence = len(history) + 1
    identity = json.dumps([
        selected_job_id, sequence, status, attempt, input_digest, source_event_ids,
        candidate_ids, error_code,
    ], separators=(",", ":"))
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-chronicle/distill-state/v1/{identity}"))
    metadata: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": "knowledge_distill_job",
        "job_id": selected_job_id,
        "job_event_id": event_id,
        "previous_job_event_id": previous_id,
        "sequence": sequence,
        "session_id": session_id,
        "input_digest": input_digest,
        "source_event_ids": [cast(JsonValue, item) for item in source_event_ids],
        "status": status,
        "attempt": attempt,
        "candidate_ids": [cast(JsonValue, item) for item in candidate_ids],
        "error_code": error_code,
    }
    body = f"# Distillation job {status}\n\nThis state records rule-based local work only.\n"
    metadata["state_digest"] = _state_digest(metadata, body)
    encoded = render_markdown(metadata, body).encode("utf-8")
    path = job_root(vault, selected_job_id) / f"{sequence:06d}_{event_id}.md"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ChronicleError("distill_job_conflict", "job state path already contains different content")
    else:
        atomic_write_new(path, encoded)
    return path, metadata


@contextmanager
def job_lock(context: CaptureContext, selected_job_id: str) -> Generator[None, None, None]:
    """Serialize retries for one job without holding the Vault writer lock recursively."""
    lock_root = context.paths.state_dir / "knowledge-job-locks"
    ensure_private_dir(lock_root)
    lock_path = lock_root / f"{selected_job_id}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise ChronicleError("lock_error", "distillation job lock cannot be opened") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ChronicleError("unsafe_permissions", "distillation job lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ChronicleError("writer_busy", "this distillation job is already running") from error
        yield
    finally:
        os.close(descriptor)


def summarize_job(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return the safe current job state without source text or error details."""
    return {
        "job_id": text_field(metadata, "job_id"),
        "status": text_field(metadata, "status"),
        "attempt": metadata.get("attempt"),
        "source_event_ids": metadata.get("source_event_ids", []),
        "candidate_ids": metadata.get("candidate_ids", []),
        "error_code": metadata.get("error_code"),
    }

