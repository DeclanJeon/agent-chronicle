"""Atomic rebuild of the disposable SQLite projection."""

from collections.abc import Iterable
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from contextlib import closing
from typing import cast

from chronicle.atomic import ensure_private_dir, fsync_directory
from chronicle.errors import ChronicleError
from chronicle.models import EnvironmentPaths, JsonValue

CheckpointRow = tuple[str, str | None, str, str, Path, dict[str, JsonValue], str]
EventRow = tuple[str, str, str, str]


def database_path(paths: EnvironmentPaths) -> Path:
    """Return the SQLite projection location outside the selected vault."""
    return paths.state_dir / "projection.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    """Open a local SQLite DB with durable WAL settings and projection schema."""
    ensure_private_dir(path.parent)
    directory_stat = path.parent.stat()
    if directory_stat.st_uid != os.getuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise ChronicleError("unsafe_permissions", "SQLite state directory must be private and user-owned")
    if path.exists():
        file_stat = path.stat()
        if path.is_symlink() or file_stat.st_uid != os.getuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ChronicleError("unsafe_permissions", "SQLite projection file permissions are unsafe")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=5.0)
    journal_mode = cast(
        tuple[object, ...] | None,
        connection.execute("PRAGMA journal_mode=WAL").fetchone(),
    )
    if journal_mode is None or not journal_mode or journal_mode[0] != "wal":
        raise ChronicleError("projection_error", "SQLite WAL mode could not be enabled")
    _ = connection.execute("PRAGMA synchronous=FULL")
    _ = connection.execute("PRAGMA foreign_keys=ON")
    schema = "\n".join((
        "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY,",
        "idempotency_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,",
        "markdown_path TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS checkpoints (checkpoint_id TEXT PRIMARY KEY,",
        "previous_checkpoint_id TEXT, source_instance_id TEXT NOT NULL,",
        "stream_id TEXT NOT NULL, markdown_path TEXT NOT NULL);",
        "CREATE TABLE IF NOT EXISTS cursors (source_instance_id TEXT NOT NULL,",
        "stream_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL,",
        "next_position_json TEXT NOT NULL, PRIMARY KEY (source_instance_id, stream_id));",
    ))
    _ = connection.executescript(schema)
    return connection


def _latest(rows: list[CheckpointRow]) -> list[CheckpointRow]:
    """Select the leaf checkpoint for each source stream."""
    parent_ids = {previous for _id, previous, _source, _stream, _path, _next, _created in rows if previous}
    return [row for row in rows if row[0] not in parent_ids]


def rebuild_projection(
    paths: EnvironmentPaths,
    events: Iterable[EventRow],
    checkpoints: Iterable[CheckpointRow],
) -> None:
    """Build a fresh WAL database from verified Markdown and atomically swap it in."""
    target = database_path(paths)
    ensure_private_dir(target.parent)
    if target.is_symlink():
        raise ChronicleError("unsafe_path", "SQLite projection path is a symbolic link")
    if target.exists():
        target_stat = target.stat()
        if (
            not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_uid != os.getuid()
            or stat.S_IMODE(target_stat.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_permissions", "SQLite projection file owner or permissions are unsafe")
    event_rows = list(events)
    checkpoint_rows = list(checkpoints)
    staging = target.with_name(f".projection.{secrets.token_hex(8)}.sqlite3")
    try:
        with closing(_connect(staging)) as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            _ = connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", event_rows)
            _ = connection.executemany(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
                ((checkpoint_id, previous_id, source_id, stream_id, str(path))
                 for checkpoint_id, previous_id, source_id, stream_id, path, _next, _created in checkpoint_rows),
            )
            _ = connection.executemany(
                "INSERT INTO cursors VALUES (?, ?, ?, ?)",
                ((source_id, stream_id, checkpoint_id,
                  json.dumps(next_position, ensure_ascii=False, sort_keys=True))
                 for checkpoint_id, _previous, source_id, stream_id, _path, next_position, _created
                 in _latest(checkpoint_rows)),
            )
            connection.commit()
            checkpoint_result = cast(
                tuple[object, ...] | None,
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(),
            )
            if checkpoint_result is None or not checkpoint_result or checkpoint_result[0] != 0:
                raise ChronicleError("projection_error", "SQLite WAL checkpoint did not complete")
        for suffix in ("-wal", "-shm"):
            Path(f"{staging}{suffix}").unlink(missing_ok=True)
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        os.replace(staging, target)
        fsync_directory(target.parent)
    except ChronicleError:
        staging.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        staging.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{staging}{suffix}").unlink(missing_ok=True)
        raise ChronicleError("projection_error", str(error)) from error
