"""Persistent immutable membership records for exact-text overlap families."""

import json
import os
from pathlib import Path
import stat
import uuid

from chronicle.atomic import atomic_write_new
from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import JsonValue
from chronicle.naming import sha256_text
from chronicle.schema import text_field


def _canonical_id(value: str, label: str) -> str:
    """Validate a canonical UUID before using it in a Vault path."""
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise ChronicleError("invalid_candidate", f"{label} must be a UUID") from error
    if canonical != value:
        raise ChronicleError("invalid_candidate", f"{label} must be canonical")
    return canonical


def _membership_digest(metadata: dict[str, JsonValue], body: str) -> str:
    """Hash all immutable membership fields without self-reference."""
    payload = {key: value for key, value in metadata.items() if key != "membership_digest"}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n" + body)


def _private_directory(path: Path) -> None:
    """Require generated family and candidate directories to remain private."""
    if path.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge family path is a symbolic link")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ChronicleError("unsafe_permissions", "knowledge family directory must be private")


def _read_markdown(path: Path, maximum: int = 64 * 1024) -> tuple[bytes, dict[str, JsonValue], str]:
    """Read a bounded Markdown file without following a replaced symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ChronicleError("unsafe_path", "knowledge family record cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > maximum
        ):
            raise ChronicleError("unsafe_path", "knowledge family record is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise ChronicleError("unsafe_path", "knowledge family record exceeds its size limit")
    finally:
        os.close(descriptor)
    metadata, body, _normalized = parse_frontmatter(raw)
    return raw, metadata, body


def _path(vault: Path, family_id: str, candidate_id: str) -> Path:
    """Return one UUID-constrained family membership path."""
    family = _canonical_id(family_id, "candidate family ID")
    candidate = _canonical_id(candidate_id, "candidate ID")
    return vault / "90_System/KnowledgeFamilies" / family / f"{candidate}.md"


def record_family_member(
    vault: Path,
    family_id: str,
    candidate_id: str,
    revision_id: str,
    candidate_digest: str,
) -> Path:
    """Publish or verify one immutable candidate-to-family membership."""
    path = _path(vault, family_id, candidate_id)
    system = vault / "90_System"
    root = system / "KnowledgeFamilies"
    if system.is_symlink() or root.is_symlink() or path.parent.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge family path is a symbolic link")
    for directory in (system, root, path.parent):
        if directory.exists():
            _private_directory(directory)
    revision = _canonical_id(revision_id, "candidate revision ID")
    if len(candidate_digest) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_digest):
        raise ChronicleError("invalid_candidate", "candidate digest is malformed")
    metadata: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": "knowledge_family_member",
        "family_id": family_id,
        "candidate_id": candidate_id,
        "candidate_revision_id": revision,
        "candidate_digest": candidate_digest,
    }
    body = "# Persistent overlap-family membership\n\nThis record links one immutable candidate; it does not merge or approve claims.\n"
    metadata["membership_digest"] = _membership_digest(metadata, body)
    encoded = render_markdown(metadata, body).encode("utf-8")
    if path.exists() or path.is_symlink():
        raw, _existing_metadata, _existing_body = _read_markdown(path)
        if raw != encoded:
            raise ChronicleError("candidate_family_conflict", "family membership differs from its immutable record")
        return path
    atomic_write_new(path, encoded)
    return path


def family_member_ids(vault: Path, family_id: str) -> list[str]:
    """Read and validate stable candidate membership for one exact-text family."""
    family = _canonical_id(family_id, "candidate family ID")
    system = vault / "90_System"
    root = system / "KnowledgeFamilies"
    if system.is_symlink() or root.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge family directory is a symbolic link")
    if system.exists():
        _private_directory(system)
    folder = root / family
    if not root.exists():
        return []
    _private_directory(root)
    if folder.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge family path is a symbolic link")
    if not folder.exists():
        return []
    _private_directory(folder)
    files = sorted(folder.iterdir())
    if len(files) > 10_000:
        raise ChronicleError("invalid_candidate", "knowledge family exceeds its record limit")
    if any(path.suffix != ".md" for path in files):
        raise ChronicleError("invalid_candidate", "knowledge family contains a non-Markdown entry")
    candidate_root = system / "KnowledgeCandidates"
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise ChronicleError("unsafe_path", "knowledge candidate directory is missing or unsafe")
    _private_directory(candidate_root)
    members: list[str] = []
    for path in files:
        _raw, metadata, body = _read_markdown(path)
        candidate_id = _canonical_id(path.stem, "candidate ID")
        if candidate_id != path.stem:
            raise ChronicleError("invalid_candidate", "family candidate filename is not canonical")
        candidate_path = candidate_root / f"{candidate_id}.md"
        candidate_raw, candidate_metadata, _candidate_body = _read_markdown(candidate_path, 16 * 1024 * 1024)
        schema_version = metadata.get("schema_version")
        candidate_membership_version = candidate_metadata.get("family_membership_version")
        if (
            not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1
            or not isinstance(candidate_membership_version, int)
            or isinstance(candidate_membership_version, bool) or candidate_membership_version != 1
            or text_field(metadata, "kind") != "knowledge_family_member"
            or text_field(metadata, "family_id") != family
            or text_field(metadata, "candidate_id") != candidate_id
            or text_field(metadata, "membership_digest") != _membership_digest(metadata, body)
            or text_field(candidate_metadata, "candidate_id") != candidate_id
            or text_field(candidate_metadata, "candidate_family_id") != family
            or text_field(candidate_metadata, "revision_id") != text_field(metadata, "candidate_revision_id")
            or sha256_text(candidate_raw.decode("utf-8", errors="strict")) != text_field(metadata, "candidate_digest")
        ):
            raise ChronicleError("invalid_candidate", "knowledge family membership or candidate snapshot is invalid")
        members.append(candidate_id)
    return members
