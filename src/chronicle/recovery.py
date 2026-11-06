"""Reconstruction of operational projections from immutable Markdown."""

from dataclasses import dataclass

from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.destination import verify_vault
from chronicle.event_format import read_capture
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter
from chronicle.models import CaptureContext
from chronicle.projection import rebuild_projection
from chronicle.schema import text_field


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Committed event count and uncheckpointed event identities."""

    committed_events: int
    orphan_event_ids: tuple[str, ...]


def rebuild_from_markdown(context: CaptureContext) -> RecoveryResult:
    """Validate checkpoint chains and rebuild SQLite from Markdown only."""
    config = context.config
    vault = verify_vault(config.vault, config.host_id, config.source_instance_id)
    snapshot = verify_checkpoint_chain(vault)
    committed_ids = {reference.event_id for reference in snapshot.references}
    event_rows: list[tuple[str, str, str, str]] = []
    for reference in snapshot.references:
        path = vault / reference.relative_path
        metadata, _content = read_capture(path)
        if text_field(metadata, "payload_hash") != reference.payload_hash:
            raise ChronicleError("checkpoint_conflict", "Markdown content hash differs from checkpoint")
        event_rows.append((
            reference.event_id,
            reference.idempotency_key,
            reference.payload_hash,
            str(path),
        ))
    orphan_ids: list[str] = []
    event_root = vault / "80_Events"
    if event_root.is_symlink() or not event_root.is_dir():
        raise ChronicleError("unsafe_path", "event directory is missing or a symbolic link")
    for path in sorted(event_root.rglob("*.md")):
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "event ledger contains a symbolic link")
        metadata, _body, _normalized = parse_frontmatter(path.read_bytes())
        if metadata.get("kind") == "event":
            event_id = text_field(metadata, "event_id")
            if event_id not in committed_ids:
                orphan_ids.append(event_id)
    checkpoint_rows = [(
        checkpoint.checkpoint_id,
        checkpoint.previous_id,
        checkpoint.source_instance_id,
        checkpoint.stream_id,
        checkpoint.path,
        checkpoint.next_position,
        checkpoint.created_at,
    ) for checkpoint in snapshot.checkpoints]
    rebuild_projection(context.paths, event_rows, checkpoint_rows)
    return RecoveryResult(len(event_rows), tuple(orphan_ids))
