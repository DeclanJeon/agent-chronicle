"""Durable, no-overwrite Markdown publication for Linux local filesystems."""

import os
from pathlib import Path
import secrets
import stat

from chronicle.errors import ChronicleError


def ensure_private_dir(path: Path) -> None:
    """Create missing directories as 0700 and reject symlinks in every component."""
    absolute = path.absolute()
    missing: list[Path] = []
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        try:
            entry_stat = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            continue
        except OSError as error:
            raise ChronicleError("unsafe_path", error.strerror or "path inspection failed") from error
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise ChronicleError("unsafe_path", "directory path contains a symbolic link or non-directory")
    for directory in missing:
        try:
            directory.mkdir(mode=0o700)
            fsync_directory(directory.parent)
        except FileExistsError:
            try:
                entry_stat = directory.lstat()
            except OSError as error:
                raise ChronicleError("unsafe_path", "directory appeared during creation") from error
            if not stat.S_ISDIR(entry_stat.st_mode):
                raise ChronicleError("unsafe_path", "directory appeared as a symbolic link or non-directory")
        except OSError as error:
            raise ChronicleError("io_error", error.strerror or "mkdir failed") from error


def require_private_directory(path: Path) -> None:
    """Require one generated data directory to be user-owned and private."""
    directory_stat = path.stat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise ChronicleError("unsafe_permissions", "generated data directory must be user-owned and private")


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on a supported local filesystem."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ChronicleError("durability_error", error.strerror or "directory fsync failed") from error


def fsync_existing_file_and_parent(path: Path) -> None:
    """Re-establish file and directory durability before replay checkpointing."""
    ensure_private_dir(path.parent)
    require_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ChronicleError("unsafe_path", "event ledger entry is not a regular file")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(path.parent)
    except ChronicleError:
        raise
    except OSError as error:
        raise ChronicleError("durability_error", error.strerror or "file fsync failed") from error


def atomic_write_new(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Publish new bytes without replacing an existing name, then fsync parent."""
    ensure_private_dir(path.parent)
    require_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(content)
            if written != len(content):
                raise OSError("short write while publishing Chronicle Markdown")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        fsync_directory(path.parent)
    except FileExistsError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleError("collision", f"refusing to replace {path.name}") from error
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleError("durability_error", error.strerror or "file publication failed") from error


def atomic_replace(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Durably replace one previously verified generated file."""
    ensure_private_dir(path.parent)
    require_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(content)
            if written != len(content):
                raise OSError("short write while replacing Chronicle Markdown")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleError("durability_error", error.strerror or "file replacement failed") from error
