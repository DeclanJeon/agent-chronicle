"""Bounded, no-follow schedule cursor I/O relative to pinned directory descriptors."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import stat
from typing import Final

from chronicle.errors import ChronicleError

_CURSOR_NAME: Final = "daily-catchup.json"
_MAX_CURSOR_BYTES: Final = 4096
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _io_error(error: OSError) -> ChronicleError:
    """Classify filesystem failures without reflecting selected paths or payloads."""
    code = "unsafe_path" if error.errno in (errno.ELOOP, errno.ENOTDIR) else "daily_schedule_error"
    return ChronicleError(code, "daily cursor storage cannot be safely accessed")


def _check_file(info: os.stat_result) -> None:
    """Require a bounded, private, single-link regular file owned by this user."""
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_nlink != 1
        or info.st_size > _MAX_CURSOR_BYTES
    ):
        raise ChronicleError("unsafe_permissions", "daily catch-up cursor is unsafe")


def _check_bindings(bindings: tuple[tuple[int, str, os.stat_result], ...]) -> None:
    """Validate names against pinned inodes even when the cursor directory is absent."""
    try:
        for parent, name, expected in bindings:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise ChronicleError("unsafe_path", "daily cursor directory binding changed")
    except OSError as error:
        raise _io_error(error) from error


@dataclass(frozen=True, slots=True)
class DailyCursorStore:
    """Borrow one schedule directory FD only during open_cursor_store's context."""

    directory_fd: int
    bindings: tuple[tuple[int, str, os.stat_result], ...]

    def check_binding(self) -> None:
        """Reject renamed/rebound components while continuing to use only pinned FDs."""
        _check_bindings(self.bindings)

    def read_text(self) -> str | None:
        """Read at most one bounded private leaf without following links or FIFOs."""
        self.check_binding()
        try:
            descriptor = os.open(
                _CURSOR_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _io_error(error) from error
        try:
            with os.fdopen(descriptor, "rb") as stream:
                _check_file(os.fstat(stream.fileno()))
                content = stream.read(_MAX_CURSOR_BYTES + 1)
            if len(content) > _MAX_CURSOR_BYTES:
                raise ChronicleError("unsafe_permissions", "daily cursor exceeds its size limit")
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ChronicleError("daily_schedule_error", "daily cursor is not UTF-8") from error
        except OSError as error:
            raise _io_error(error) from error

    def replace(self, payload: bytes) -> None:
        """Publish private bytes with file/parent fsync using only the pinned FD."""
        self.check_binding()
        if len(payload) > _MAX_CURSOR_BYTES:
            raise ChronicleError("daily_schedule_error", "daily cursor exceeds its size limit")
        temporary = f".daily-catchup.{secrets.token_hex(12)}.tmp"
        created = False
        renamed = False
        try:
            try:
                previous = os.stat(_CURSOR_NAME, dir_fd=self.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                _check_file(previous)
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=self.directory_fd,
            )
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                written = stream.write(payload)
                if written != len(payload):
                    raise ChronicleError("durability_error", "daily cursor write was incomplete")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary, _CURSOR_NAME,
                src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd,
            )
            renamed = True
            os.fsync(self.directory_fd)
        except OSError as error:
            raise ChronicleError("durability_error", "daily cursor publication failed") from error
        finally:
            if created and not renamed:
                try:
                    os.unlink(temporary, dir_fd=self.directory_fd)
                except FileNotFoundError:
                    created = False  # A removed temporary already satisfies cleanup.
                except OSError as error:
                    raise _io_error(error) from error


@contextmanager
def open_cursor_store(vault: Path, *, create: bool = False) -> Generator[DailyCursorStore | None, None, None]:
    """Open/create relative to pinned parents; read-only callers never mkdir/fsync."""
    with ExitStack() as resources:
        missing = False
        bindings: list[tuple[int, str, os.stat_result]] = []
        try:
            descriptor = os.open(vault.anchor, _DIRECTORY_FLAGS)
            _ = resources.callback(os.close, descriptor)
            for component in vault.absolute().parts[1:]:
                parent = descriptor
                descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                _ = resources.callback(os.close, descriptor)
                bindings.append((parent, component, os.fstat(descriptor)))
            for component in ("90_System", "Schedules"):
                parent = descriptor
                try:
                    descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        missing = True
                        break
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                    descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                _ = resources.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                bindings.append((parent, component, info))
                if (
                    info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or stat.S_IMODE(info.st_mode) & 0o700 != 0o700
                ):
                    raise ChronicleError("unsafe_permissions", "daily cursor directory is unsafe")
        except OSError as error:
            raise _io_error(error) from error
        pinned = tuple(bindings)
        _check_bindings(pinned)
        yield None if missing else DailyCursorStore(descriptor, pinned)
