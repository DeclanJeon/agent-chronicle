"""Safe user-unit file creation and inspection primitives."""

import os
from pathlib import Path
import secrets
import stat
import sys

from chronicle.atomic import fsync_directory
from chronicle.errors import ChronicleError

SERVICE_NAME = "agent-chronicle.service"


def _quote_unit_argument(value: str) -> str:
    """Quote a systemd ExecStart token and escape specifiers and delimiters."""
    if "\n" in value or "\r" in value:
        raise ChronicleError("invalid_service_plan", "service command contains a line break")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_user_unit() -> str:
    """Render a deterministic foreground daemon unit without installing it."""
    command = " ".join(_quote_unit_argument(value) for value in (
        sys.executable, "-m", "chronicle", "daemon",
    ))
    return (
        "[Unit]\n"
        "Description=Agent Chronicle local foreground collector\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _prepare_unit_directory(path: Path) -> None:
    """Create missing user service directories and reject symlink components."""
    absolute = path.absolute()
    missing: list[Path] = []
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        try:
            entry = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            continue
        except OSError as error:
            raise ChronicleError("service_unit_error", "service directory cannot be inspected") from error
        if not stat.S_ISDIR(entry.st_mode):
            raise ChronicleError("unsafe_path", "service directory contains a symlink or non-directory")
    for directory in missing:
        try:
            directory.mkdir(mode=0o700)
            fsync_directory(directory.parent)
        except FileExistsError:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ChronicleError("unsafe_path", "service directory appeared as a non-directory")
        except OSError as error:
            raise ChronicleError("service_unit_error", "service directory cannot be created") from error
    final_stat = path.stat()
    if final_stat.st_uid != os.getuid():
        raise ChronicleError("unsafe_permissions", "service directory must be user-owned")


def read_existing_unit(path: Path) -> bytes:
    """Read one owner-controlled regular unit without following symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ChronicleError("service_unit_error", "existing service unit cannot be opened") from error
    try:
        entry = os.fstat(descriptor)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) & 0o022
        ):
            raise ChronicleError("unsafe_permissions", "existing service unit is not safely owned")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except OSError as error:
        raise ChronicleError("service_unit_error", "existing service unit cannot be read") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_unit_new(path: Path, content: bytes) -> None:
    """Publish one unit file without replacing any existing name."""
    _prepare_unit_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(content)
            if written != len(content):
                raise OSError("short service unit write")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        fsync_directory(path.parent)
    except FileExistsError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleError("service_unit_conflict", "service unit already exists") from error
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ChronicleError("service_unit_error", "service unit could not be written") from error


def unit_path() -> Path:
    """Return the current user's explicit systemd unit destination."""
    return Path.home() / ".config/systemd/user" / SERVICE_NAME


def unit_installed(path: Path | None = None) -> bool:
    """Return whether a safe regular unit exists at the selected managed path."""
    target = path if path is not None else unit_path()
    try:
        entry = target.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(entry.st_mode)
        and entry.st_uid == os.getuid()
        and stat.S_IMODE(entry.st_mode) & 0o022 == 0
    )
