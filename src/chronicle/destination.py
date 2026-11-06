"""Fail-closed Linux local destination validation and initialization."""

import json
import os
from pathlib import Path
import platform
import re
import stat

from chronicle.atomic import atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.schema import parse_json_object

_ALLOWED_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs"})
_SYNC_NAMES = frozenset({
    "google drive", "drivefs", "dropbox", "onedrive", "nextcloud", "icloud drive",
    "syncthing", ".dropbox.cache",
})
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mount_path(value: str) -> str:
    """Decode Linux mountinfo octal escapes."""
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _existing_ancestor(path: Path) -> Path:
    """Find the nearest existing path without resolving through symlinks."""
    cursor = path
    while not cursor.exists():
        if cursor.parent == cursor:
            raise ChronicleError("unsupported_destination", "no existing destination ancestor")
        cursor = cursor.parent
    return cursor


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symbolic-link component in the destination path."""
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ChronicleError("destination_error", error.strerror or "path inspection failed") from error
        if stat.S_ISLNK(mode):
            raise ChronicleError("unsupported_destination", "symbolic-link destination is not supported")


def _mount_info(path: Path) -> tuple[Path, str]:
    """Resolve the nearest mountpoint and filesystem type from Linux mountinfo."""
    target = _existing_ancestor(path)
    try:
        entries = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ChronicleError("unsupported_destination", "mount type cannot be verified") from error
    matches: list[tuple[int, Path, str]] = []
    for entry in entries:
        before, separator, after = entry.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        filesystem = after.split()
        if len(fields) < 5 or not filesystem:
            continue
        mountpoint = Path(_decode_mount_path(fields[4]))
        if target == mountpoint or mountpoint in target.parents:
            matches.append((len(mountpoint.parts), mountpoint, filesystem[0]))
    if not matches:
        raise ChronicleError("unsupported_destination", "mounted filesystem cannot be identified")
    _depth, mountpoint, filesystem_type = max(matches, key=lambda match: match[0])
    return mountpoint, filesystem_type


def validate_destination(path: Path) -> Path:
    """Validate a single-owner Linux local directory without writing to it."""
    if platform.system() != "Linux":
        raise ChronicleError("unsupported_host", "only Linux local filesystems are supported")
    expanded = path.expanduser()
    absolute = Path(os.path.abspath(expanded))
    _reject_symlink_components(absolute)
    if any(part.casefold() in _SYNC_NAMES for part in absolute.parts):
        raise ChronicleError("unsupported_concurrency", "known synchronized folder name is rejected")
    existing = _existing_ancestor(absolute)
    if not existing.is_dir():
        raise ChronicleError("unsupported_destination", "destination ancestor is not a directory")
    if existing.stat().st_uid != os.getuid():
        raise ChronicleError("unsupported_concurrency", "destination is not owned by this user")
    mountpoint, filesystem = _mount_info(absolute)
    home_mountpoint, _home_filesystem = _mount_info(Path.home())
    if mountpoint != home_mountpoint:
        raise ChronicleError("unsupported_concurrency", "destination is on a separate mountpoint")
    if filesystem not in _ALLOWED_FILESYSTEMS:
        raise ChronicleError("unsupported_concurrency", f"filesystem is not allowlisted: {filesystem}")
    if absolute.exists() and not absolute.is_dir():
        raise ChronicleError("unsupported_destination", "destination exists and is not a directory")
    if absolute.exists() and absolute.stat().st_uid != os.getuid():
        raise ChronicleError("unsupported_concurrency", "destination is not owned by this user")
    return absolute


def _require_private_owner(path: Path) -> None:
    """Require Chronicle-owned metadata to be user-owned and mode 0600/0700."""
    if path.is_symlink() or not path.exists():
        raise ChronicleError("unsafe_metadata", "Chronicle metadata is missing or a symbolic link")
    file_stat = path.lstat()
    expected_type = stat.S_ISDIR(file_stat.st_mode) or stat.S_ISREG(file_stat.st_mode)
    if file_stat.st_uid != os.getuid() or not expected_type or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ChronicleError("unsafe_metadata", "Chronicle metadata owner or permissions are unsafe")


def _read_manifest(path: Path) -> dict[str, str | int]:
    """Parse the installation manifest without accepting arbitrary fields."""
    _require_private_owner(path)
    try:
        loaded = parse_json_object(path.read_text(encoding="utf-8"), "vault manifest")
    except (OSError, UnicodeDecodeError, ChronicleError) as error:
        raise ChronicleError("invalid_manifest", "vault manifest is unreadable") from error
    host_id = loaded.get("host_id")
    source_instance_id = loaded.get("source_instance_id")
    owner_uid = loaded.get("owner_uid")
    schema_version = loaded.get("schema_version")
    if not isinstance(host_id, str) or not isinstance(source_instance_id, str):
        raise ChronicleError("invalid_manifest", "vault identity fields are invalid")
    if (
        not isinstance(owner_uid, int) or isinstance(owner_uid, bool)
        or not isinstance(schema_version, int) or isinstance(schema_version, bool)
    ):
        raise ChronicleError("invalid_manifest", "vault owner/version fields are invalid")
    if schema_version != 1:
        raise ChronicleError("invalid_manifest", "unsupported vault manifest schema")
    return {"host_id": host_id, "source_instance_id": source_instance_id, "owner_uid": owner_uid, "schema_version": schema_version}


def initialize_vault(path: Path, host_id: str, source_instance_id: str) -> Path:
    """Create Chronicle metadata without overwriting existing vault content."""
    vault = validate_destination(path)
    ensure_private_dir(vault)
    metadata_dir = vault / ".chronicle"
    if metadata_dir.exists():
        _require_private_owner(metadata_dir)
        if not metadata_dir.is_dir():
            raise ChronicleError("invalid_manifest", "Chronicle metadata path is not a directory")
    manifest_path = metadata_dir / "manifest.json"
    if manifest_path.is_symlink():
        raise ChronicleError("unsafe_metadata", "vault manifest may not be a symbolic link")
    if manifest_path.exists():
        manifest = _read_manifest(manifest_path)
        if manifest["host_id"] != host_id or manifest["source_instance_id"] != source_instance_id:
            raise ChronicleError("unsupported_concurrency", "vault manifest belongs to another installation")
        if manifest["owner_uid"] != os.getuid():
            raise ChronicleError("unsupported_concurrency", "vault manifest belongs to another OS owner")
    else:
        if metadata_dir.exists() and any(metadata_dir.iterdir()):
            raise ChronicleError("invalid_manifest", "existing .chronicle directory has no Chronicle manifest")
        ensure_private_dir(metadata_dir)
        payload = json.dumps(
            {"schema_version": 1, "host_id": host_id, "source_instance_id": source_instance_id, "owner_uid": os.getuid()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_write_new(manifest_path, payload)
    for relative in (
        "80_Events", "90_System/Checkpoints", "90_System/KnowledgeCandidates",
        "90_System/KnowledgeDecisions", "10_Projects", "50_Daily", "60_Reviews",
    ):
        directory = vault / relative
        ensure_private_dir(directory)
        _require_private_owner(directory)
    return vault


def verify_vault(path: Path, host_id: str, source_instance_id: str) -> Path:
    """Revalidate the local target and its manifest before each operation."""
    vault = validate_destination(path)
    _require_private_owner(vault / ".chronicle")
    manifest = _read_manifest(vault / ".chronicle/manifest.json")
    if manifest["host_id"] != host_id or manifest["source_instance_id"] != source_instance_id:
        raise ChronicleError("unsupported_concurrency", "vault installation identity changed")
    if manifest["owner_uid"] != os.getuid():
        raise ChronicleError("unsupported_concurrency", "vault owner changed")
    return vault
