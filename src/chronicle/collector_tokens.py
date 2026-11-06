"""Versioned collector tokens and checkpoint-based state-loss recovery."""

from datetime import datetime
import json
from typing import BinaryIO

from chronicle.checkpoints import RecoverySnapshot
from chronicle.errors import ChronicleError
from chronicle.schema import parse_json_object
from chronicle.source_models import CoverageGap, SourceRegistration

from chronicle.collector_files import prefix_digest


def _checkpoint_generation(
    snapshot: RecoverySnapshot,
    source: SourceRegistration,
    member_id: str,
) -> tuple[int, str | None]:
    """Find the latest durable generation token for this registered file."""
    prefix = f"chronicle-pi-v1:{source.source_id}:{member_id}:g"
    matches: list[tuple[int, str | None]] = []
    for checkpoint in snapshot.latest:
        if not checkpoint.stream_id.startswith(prefix):
            continue
        suffix = checkpoint.stream_id.removeprefix(prefix)
        if not suffix.isdigit():
            raise ChronicleError("checkpoint_corrupt", "collector stream generation is invalid")
        token = checkpoint.next_position.get("resume_token")
        if token is not None and not isinstance(token, str):
            raise ChronicleError("checkpoint_corrupt", "collector resume token is invalid")
        matches.append((int(suffix), token))
    if not matches:
        return 1, None
    return max(matches, key=lambda item: item[0])


def _token_fields(token: str) -> tuple[int, int, int, int, str, str, str]:
    """Parse one immutable collector token stored inside a durable checkpoint."""
    try:
        decoded = parse_json_object(token, "collector resume token")
    except ChronicleError as error:
        raise ChronicleError("checkpoint_corrupt", "collector resume token is invalid") from error
    version = decoded.get("version")
    device = decoded.get("device")
    inode = decoded.get("inode")
    generation = decoded.get("generation")
    cursor = decoded.get("cursor")
    session_id = decoded.get("session_id")
    member_id = decoded.get("member_id")
    prefix_sha256 = decoded.get("prefix_sha256")
    if (
        not isinstance(version, int) or isinstance(version, bool) or version != 1
        or not isinstance(device, int) or isinstance(device, bool) or device < 0
        or not isinstance(inode, int) or isinstance(inode, bool) or inode < 0
        or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1
        or not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0
        or not isinstance(session_id, str) or not isinstance(member_id, str)
        or not isinstance(prefix_sha256, str) or len(prefix_sha256) != 64
    ):
        raise ChronicleError("checkpoint_corrupt", "collector resume token fields are invalid")
    return device, inode, generation, cursor, session_id, member_id, prefix_sha256


def resume_after_state_loss(
    stream: BinaryIO,
    file_size: int,
    source: SourceRegistration,
    member_id: str,
    device: int,
    inode: int,
    snapshot: RecoverySnapshot,
    relative: str,
) -> tuple[int, CoverageGap | None]:
    """Validate checkpointed prefix after state loss, then replay from byte zero."""
    generation, token = _checkpoint_generation(snapshot, source, member_id)
    if token is None:
        return generation, None
    saved_device, saved_inode, saved_generation, cursor, _session_id, saved_member, prefix_hash = _token_fields(token)
    if (saved_device, saved_inode, saved_generation, saved_member) != (
        device, inode, generation, member_id,
    ):
        raise ChronicleError("checkpoint_corrupt", "collector token identity differs from its stream")
    if file_size < cursor or prefix_digest(stream, cursor) != prefix_hash:
        gap = CoverageGap(
            source.source_id, member_id, relative, cursor, file_size,
            "committed_prefix_changed_after_state_loss", datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        return generation + 1, gap
    return generation, None


def make_resume_token(
    device: int,
    inode: int,
    generation: int,
    cursor: int,
    session_id: str,
    member_id: str,
    prefix_sha256: str,
) -> str:
    """Serialize a checkpoint cursor without storing the source path."""
    return json.dumps({
        "version": 1, "device": device, "inode": inode,
        "generation": generation, "cursor": cursor, "session_id": session_id,
        "member_id": member_id, "prefix_sha256": prefix_sha256,
    }, sort_keys=True, separators=(",", ":"))
