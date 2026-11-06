"""OS-level single-writer and foreground daemon lock."""

from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from chronicle.atomic import ensure_private_dir
from chronicle.errors import ChronicleError


@contextmanager
def collector_lock(state_root: Path) -> Generator[None, None, None]:
    """Hold one nonblocking foreground collector lock outside the Vault writer lock."""
    ensure_private_dir(state_root)
    lock_path = state_root / "collector.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise ChronicleError("lock_error", "collector lock cannot be opened") from error
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "collector lock owner or permissions are unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ChronicleError("writer_busy", "another foreground collector is running") from error
        yield
    finally:
        os.close(descriptor)


def collector_is_running(state_root: Path) -> bool:
    """Inspect the collector lock without waiting or changing collection state."""
    ensure_private_dir(state_root)
    lock_path = state_root / "collector.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise ChronicleError("lock_error", "collector lock cannot be inspected") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    except OSError as error:
        raise ChronicleError("lock_error", "collector lock cannot be inspected") from error
    finally:
        os.close(descriptor)


@contextmanager
def writer_lock(vault: Path) -> Generator[None, None, None]:
    """Hold the exclusive local writer lock until context exit."""
    lock_path = vault / ".chronicle/writer.lock"
    ensure_private_dir(lock_path.parent)
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise ChronicleError("lock_error", error.strerror or "cannot open writer lock") from error
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "writer lock owner or permissions are unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ChronicleError("writer_busy", "another Chronicle writer holds this vault") from error
        yield
    finally:
        os.close(descriptor)
