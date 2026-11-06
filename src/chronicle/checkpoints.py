"""Immutable Markdown checkpoints and fail-closed chain reconstruction."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import JsonValue
from chronicle.naming import sha256_text
from chronicle.schema import integer_field, list_field, mapping_field, optional_text, text_field


@dataclass(frozen=True, slots=True)
class CheckpointInput:
    """Verified event position to commit in one immutable checkpoint."""

    source_instance_id: str
    stream_id: str
    event_id: str
    idempotency_key: str
    payload_hash: str
    event_path: Path
    next_position: dict[str, JsonValue]
    completed_position: int
    adapter_version: str
    created_at: str
    coverage_status: str = "explicit_manual_capture_non_resumable"


@dataclass(frozen=True, slots=True)
class EventReference:
    """One committed event reference from a checkpoint."""

    event_id: str
    idempotency_key: str
    payload_hash: str
    markdown_hash: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """Validated immutable checkpoint metadata and source cursor."""

    checkpoint_id: str
    previous_id: str | None
    source_instance_id: str
    stream_id: str
    path: Path
    completed_position: int
    next_position: dict[str, JsonValue]
    created_at: str
    references: tuple[EventReference, ...]


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Verified event references and one leaf checkpoint per source stream."""

    checkpoints: tuple[CheckpointRecord, ...]
    latest: tuple[CheckpointRecord, ...]
    references: tuple[EventReference, ...]


def _checkpoint_digest(metadata: dict[str, JsonValue]) -> str:
    """Bind checkpoint fields against accidental edits; this is not a signature."""
    core = {key: value for key, value in metadata.items() if key != "checkpoint_digest"}
    serialized = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"agent-chronicle/checkpoint/v1\0" + serialized.encode("utf-8")).hexdigest()


def read_checkpoint(path: Path) -> CheckpointRecord:
    """Parse one checkpoint file and validate all required fields."""
    if path.is_symlink() or not path.is_file():
        raise ChronicleError("unsafe_path", "checkpoint must be a regular file, not a symbolic link")
    metadata, _body, _normalized = parse_frontmatter(path.read_bytes())
    if integer_field(metadata, "schema_version") != 2 or text_field(metadata, "kind") != "source_checkpoint":
        raise ChronicleError("invalid_checkpoint", "unsupported checkpoint schema")
    checkpoint_id = text_field(metadata, "checkpoint_id")
    previous_id = optional_text(metadata, "previous_checkpoint_id")
    source_instance_id = text_field(metadata, "source_instance_id")
    stream_id = text_field(metadata, "stream_id")
    completed_position = integer_field(metadata, "completed_record_position")
    next_position = mapping_field(metadata, "next_position")
    if next_position.get("record_end_byte_exclusive") != completed_position:
        raise ChronicleError("invalid_checkpoint", "checkpoint cursor differs from completed record position")
    created_at = text_field(metadata, "created_at")
    if text_field(metadata, "checkpoint_digest") != _checkpoint_digest(metadata):
        raise ChronicleError("checkpoint_corrupt", "checkpoint metadata digest is invalid")
    refs: list[EventReference] = []
    for raw in list_field(metadata, "event_refs"):
        if not isinstance(raw, dict):
            raise ChronicleError("invalid_checkpoint", "event_refs entries must be mappings")
        refs.append(EventReference(
            event_id=text_field(raw, "event_id"),
            idempotency_key=text_field(raw, "idempotency_key"),
            payload_hash=text_field(raw, "payload_hash"),
            markdown_hash=text_field(raw, "markdown_hash"),
            relative_path=text_field(raw, "relative_path"),
        ))
    if path.stem != checkpoint_id or not refs:
        raise ChronicleError("invalid_checkpoint", "checkpoint filename or references are invalid")
    return CheckpointRecord(
        checkpoint_id, previous_id, source_instance_id, stream_id, path,
        completed_position, next_position, created_at, tuple(refs)
    )


def publish_checkpoint(vault: Path, request: CheckpointInput) -> CheckpointRecord:
    """Append a checkpoint after event Markdown has been durably published."""
    snapshot = verify_checkpoint_chain(vault)
    same_stream = [
        checkpoint for checkpoint in snapshot.checkpoints
        if checkpoint.source_instance_id == request.source_instance_id
        and checkpoint.stream_id == request.stream_id
    ]
    for checkpoint in same_stream:
        for reference in checkpoint.references:
            if reference.event_id == request.event_id:
                return checkpoint
    leaves = [checkpoint for checkpoint in snapshot.latest
              if checkpoint.source_instance_id == request.source_instance_id
              and checkpoint.stream_id == request.stream_id]
    if len(leaves) > 1:
        raise ChronicleError("checkpoint_fork", "source stream has multiple latest checkpoints")
    previous_id = leaves[0].checkpoint_id if leaves else None
    checkpoint_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"agent-chronicle/checkpoint/v1/{request.source_instance_id}/{request.stream_id}/{request.event_id}",
    ))
    path = vault / "90_System/Checkpoints" / f"{checkpoint_id}.md"
    relative_path = request.event_path.relative_to(vault).as_posix()
    markdown_hash = hashlib.sha256(request.event_path.read_bytes()).hexdigest()
    metadata: dict[str, JsonValue] = {
        "schema_version": 2,
        "kind": "source_checkpoint",
        "checkpoint_id": checkpoint_id,
        "previous_checkpoint_id": previous_id,
        "source_instance_id": request.source_instance_id,
        "stream_id": request.stream_id,
        "source_position_version": 1,
        "adapter_version": request.adapter_version,
        "completed_record_position": request.completed_position,
        "next_position": request.next_position,
        "event_refs": [{
            "event_id": request.event_id,
            "idempotency_key": request.idempotency_key,
            "relative_path": relative_path,
            "payload_hash": request.payload_hash,
            "markdown_hash": markdown_hash,
        }],
        "coverage_status": request.coverage_status,
        "created_at": request.created_at,
    }
    metadata["checkpoint_digest"] = _checkpoint_digest(metadata)
    atomic_write_new(path, render_markdown(metadata, "# Source checkpoint\n").encode("utf-8"))
    return read_checkpoint(path)


def verify_checkpoint_chain(vault: Path) -> RecoverySnapshot:
    """Verify all checkpoint chains and their referenced event files."""
    root = vault / "90_System/Checkpoints"
    if root.is_symlink() or not root.is_dir():
        raise ChronicleError("unsafe_path", "checkpoint directory is missing or a symbolic link")
    checkpoints = tuple(read_checkpoint(path) for path in sorted(root.glob("*.md")))
    groups: dict[tuple[str, str], list[CheckpointRecord]] = defaultdict(list)
    for checkpoint in checkpoints:
        groups[(checkpoint.source_instance_id, checkpoint.stream_id)].append(checkpoint)
    latest: list[CheckpointRecord] = []
    references: list[EventReference] = []
    seen_events: dict[str, str] = {}
    for group in groups.values():
        by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in group}
        if len(by_id) != len(group):
            raise ChronicleError("checkpoint_fork", "checkpoint IDs are duplicated")
        child_ids = {checkpoint.previous_id for checkpoint in group if checkpoint.previous_id is not None}
        roots = [checkpoint for checkpoint in group if checkpoint.previous_id is None]
        leaves = [checkpoint for checkpoint in group if checkpoint.checkpoint_id not in child_ids]
        if len(roots) != 1 or len(leaves) != 1:
            raise ChronicleError("checkpoint_chain_invalid", "checkpoint chain has a gap or fork")
        ordered: list[CheckpointRecord] = []
        current: CheckpointRecord | None = roots[0]
        while current is not None:
            ordered.append(current)
            children = [item for item in group if item.previous_id == current.checkpoint_id]
            if len(children) > 1:
                raise ChronicleError("checkpoint_fork", "checkpoint chain branches")
            current = children[0] if children else None
        if len(ordered) != len(group):
            raise ChronicleError("checkpoint_chain_invalid", "checkpoint chain contains a gap or orphan")
        latest.append(leaves[0])
        for checkpoint in ordered:
            for reference in checkpoint.references:
                _verify_event_reference(vault, reference)
                prior_hash = seen_events.get(reference.event_id)
                if prior_hash is not None:
                    if prior_hash != reference.payload_hash:
                        raise ChronicleError("checkpoint_conflict", "event ID has conflicting payload hashes")
                    raise ChronicleError("checkpoint_conflict", "event ID is referenced more than once")
                seen_events[reference.event_id] = reference.payload_hash
                references.append(reference)
    return RecoverySnapshot(checkpoints, tuple(latest), tuple(references))


def _verify_event_reference(vault: Path, reference: EventReference) -> None:
    """Check an event path stays inside the vault and its durable identity matches."""
    relative = PurePosixPath(reference.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ChronicleError("checkpoint_path_invalid", "event reference escapes the vault")
    path = vault
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ChronicleError("checkpoint_path_invalid", "event reference traverses a symbolic link")
    if not path.is_file():
        raise ChronicleError("checkpoint_event_missing", "checkpoint references a missing event")
    raw_markdown = path.read_bytes()
    if hashlib.sha256(raw_markdown).hexdigest() != reference.markdown_hash:
        raise ChronicleError("checkpoint_conflict", "event Markdown hash differs from checkpoint")
    metadata, content = read_capture(path)
    if text_field(metadata, "event_id") != reference.event_id:
        raise ChronicleError("checkpoint_conflict", "event ID differs from checkpoint reference")
    if text_field(metadata, "idempotency_key") != reference.idempotency_key:
        raise ChronicleError("checkpoint_conflict", "idempotency key differs from checkpoint")
    if text_field(metadata, "payload_hash") != reference.payload_hash:
        raise ChronicleError("checkpoint_conflict", "event payload hash differs from checkpoint")
    if sha256_text(content) != reference.payload_hash:
        raise ChronicleError("checkpoint_conflict", "event content hash differs from checkpoint")
