"""Deterministic daily Markdown view with user-region conflict preservation."""

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from chronicle.atomic import atomic_replace, atomic_write_new, ensure_private_dir
from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.event_format import read_capture
from chronicle.errors import ChronicleError
from chronicle.schema import text_field
from chronicle.daily_revisions import DailyRevision
from chronicle.daily_revision_archive import (
    parse_latest_revision,
    verify_daily_revision_chain,
)
from chronicle.daily_revision_archive_publish import (
    DailyRevisionInput,
    archive_legacy_daily,
    create_daily_revision,
)

_START = re.compile(r"<!-- chronicle:auto:start sha256=([0-9a-f]{64}) -->\n")
_END = "<!-- chronicle:auto:end -->"


@dataclass(frozen=True, slots=True)
class DailyResult:
    """Observable daily-note generation result."""

    path: Path
    event_count: int
    conflict_copy: Path | None
    revision_id: str | None
    previous_revision_id: str | None
    snapshot_path: Path | None


def _generated_section(vault: Path, day: date, timezone: ZoneInfo) -> tuple[str, int]:
    """Render only committed events belonging to one local calendar day."""
    snapshot = verify_checkpoint_chain(vault)
    event_ids = {reference.event_id for reference in snapshot.references}
    rows: list[tuple[str, str, str, str, str]] = []
    event_root = vault / "80_Events"
    if event_root.is_symlink() or not event_root.is_dir():
        raise ChronicleError(
            "unsafe_path", "event directory is missing or a symbolic link"
        )
    for path in event_root.rglob("*.md"):
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "symbolic link found in event ledger")
        metadata, _content = read_capture(path)
        if text_field(metadata, "event_id") not in event_ids:
            continue
        event_day = datetime.fromisoformat(
            text_field(metadata, "occurred_at")
        ).astimezone(timezone).date()
        if event_day == day:
            rows.append((
                text_field(metadata, "occurred_at"),
                text_field(metadata, "title"),
                text_field(metadata, "author_name"),
                text_field(metadata, "event_id"),
                path.relative_to(vault).as_posix(),
            ))
    rows.sort()
    lines = ["## Automatically collected events\n\n"]
    note_directory = vault / "50_Daily" / day.strftime("%Y/%m")
    for occurred_at, title, author, event_id, relative_event_path in rows:
        event_path = vault / relative_event_path
        link_target = Path(os.path.relpath(event_path, start=note_directory)).as_posix()
        link_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        line = (
            f"- {occurred_at} — [{link_title}](<{link_target}>) · "
            + f"{author} · event `{event_id}`\n"
        )
        lines.append(line)
    if not rows:
        lines.append("- No committed events.\n")
    return "".join(lines), len(rows)


def _marked_section(section: str) -> str:
    """Add a content digest around the machine-owned daily region."""
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    return f"<!-- chronicle:auto:start sha256={digest} -->\n{section}{_END}\n"


def _replace_region(existing: str, section: str) -> tuple[str, bool]:
    """Return a document with an updated auto-region and its conflict verdict."""
    starts = list(_START.finditer(existing))
    if not starts:
        if _END in existing:
            raise ChronicleError(
                "daily_conflict", "daily note has an end marker without a start marker"
            )
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return f"{existing}{separator}\n{_marked_section(section)}", False
    if len(starts) != 1 or existing.count(_END) != 1:
        raise ChronicleError(
            "daily_conflict", "daily note has ambiguous auto-region markers"
        )
    start = starts[0]
    end = existing.find(_END, start.end())
    if end < 0:
        raise ChronicleError("daily_conflict", "daily note auto-region is incomplete")
    current = existing[start.end() : end]
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != start.group(1):
        return existing, True
    end_after = end + len(_END)
    if end_after < len(existing) and existing[end_after] == "\n":
        end_after += 1
    updated = (
        existing[:start.start()]
        + _marked_section(section)
        + existing[end_after:]
    )
    return updated, False


def update_daily(
    vault: Path,
    day: date | None = None,
    *,
    timezone_name: str,
) -> DailyResult:
    """Refresh one local calendar day and publish a new immutable Markdown view."""
    timezone = ZoneInfo(timezone_name)
    selected_day = day or datetime.now(timezone).date()
    section, count = _generated_section(vault, selected_day, timezone)
    path = (
        vault
        / "50_Daily"
        / selected_day.strftime("%Y/%m")
        / f"{selected_day.isoformat()}.md"
    )
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise ChronicleError("unsafe_path", "daily note is a symbolic link")

    existing_bytes: bytes | None = None
    current_revision: DailyRevision | None = None
    if path.exists():
        try:
            existing_bytes = path.read_bytes()
            existing_text = existing_bytes.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as error:
            raise ChronicleError(
                "daily_conflict", "existing daily note is unreadable"
            ) from error
        current_revision, existing_body = parse_latest_revision(
            existing_text, path, selected_day
        )
        if current_revision is not None:
            verified = verify_daily_revision_chain(
                path, selected_day, current_revision.revision_id
            )
            if verified != current_revision:
                raise ChronicleError(
                    "daily_conflict", "current daily revision differs from its archive"
                )
    else:
        existing_text = ""
        existing_body = f"# {selected_day.isoformat()}\n\n"

    updated_body, conflict = _replace_region(existing_body, section)
    if conflict:
        conflict_hash = hashlib.sha256(section.encode()).hexdigest()[:8]
        conflict_path = path.with_name(
            f"{selected_day.isoformat()}.conflict-{conflict_hash}.md"
        )
        conflict_document = (
            f"{existing_text.rstrip()}\n\n"
            f"## Chronicle regeneration conflict — review only\n\n{section}"
        )
        if conflict_path.exists():
            try:
                conflict_text = conflict_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ChronicleError(
                    "daily_conflict", "daily conflict copy is unreadable"
                ) from error
            if conflict_text != conflict_document:
                raise ChronicleError(
                    "daily_conflict", "daily conflict copy already differs"
                )
        else:
            atomic_write_new(conflict_path, conflict_document.encode("utf-8"))
        return DailyResult(
            path,
            count,
            conflict_path,
            current_revision.revision_id if current_revision else None,
            current_revision.previous_revision_id if current_revision else None,
            current_revision.snapshot_path if current_revision else None,
        )

    if current_revision is not None and updated_body == existing_body:
        return DailyResult(
            path,
            count,
            None,
            current_revision.revision_id,
            current_revision.previous_revision_id,
            current_revision.snapshot_path,
        )

    previous_revision_id = current_revision.revision_id if current_revision else None
    if current_revision is None and existing_bytes is not None:
        legacy_revision = archive_legacy_daily(path, selected_day, existing_bytes)
        previous_revision_id = legacy_revision.revision_id
    revision, latest_bytes = create_daily_revision(DailyRevisionInput(
        path,
        selected_day,
        updated_body,
        previous_revision_id,
    ))
    if existing_bytes is None:
        atomic_write_new(path, latest_bytes)
    else:
        atomic_replace(path, latest_bytes)
    return DailyResult(
        path,
        count,
        None,
        revision.revision_id,
        revision.previous_revision_id,
        revision.snapshot_path,
    )
