"""Safe user-unit file creation and inspection primitives."""

import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Final

from chronicle.paths import environment_paths
from chronicle.atomic import fsync_directory
from chronicle.errors import ChronicleError

SERVICE_NAME: Final = "agent-chronicle.service"
_MAX_UNIT_BYTES: Final = 65536


def _quote_unit_argument(value: str, *, command: bool = False) -> str:
    """Quote a unit value; ExecStart also escapes systemd's environment expansion."""
    if "\n" in value or "\r" in value:
        raise ChronicleError("invalid_service_plan", "service command contains a line break")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if command:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def render_user_unit() -> str:
    """Pin the current runtime/profile without depending on a future login shell."""
    paths = environment_paths()
    source_root = Path(__file__).absolute().parents[1]
    environment = {
        "PYTHONPATH": str(source_root),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CHRONICLE_CONFIG_DIR": str(paths.config_dir),
        "CHRONICLE_STATE_DIR": str(paths.state_dir),
        "CHRONICLE_CACHE_DIR": str(paths.cache_dir),
    }
    environment_lines = "".join(
        f"Environment={_quote_unit_argument(f'{key}={value}')}\n"
        for key, value in environment.items()
    )
    command = " ".join(_quote_unit_argument(value, command=True) for value in (
        sys.executable, "-m", "chronicle", "daemon",
    ))
    return (
        "[Unit]\n"
        "Description=Agent Chronicle local foreground collector\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=notify\n"
        "NotifyAccess=main\n"
        f"{environment_lines}"
        f"ExecStart={command}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStartSec=120\n"
        "TimeoutStopSec=30\n"
        "UMask=0077\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
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
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ChronicleError("service_unit_error", "existing service unit cannot be opened") from error
    try:
        entry = os.fstat(descriptor)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) & 0o022
            or entry.st_nlink != 1
            or entry.st_size > _MAX_UNIT_BYTES
        ):
            raise ChronicleError("unsafe_permissions", "existing service unit is not safely owned")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(_MAX_UNIT_BYTES + 1)
            if len(content) > _MAX_UNIT_BYTES:
                raise ChronicleError("unsafe_permissions", "existing service unit exceeds its size bound")
            return content
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
