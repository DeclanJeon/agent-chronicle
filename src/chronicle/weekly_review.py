"""User-edit-safe Markdown snapshots of the current weekly review backlog."""

from dataclasses import dataclass
from datetime import date
import hashlib
import html
import os
from pathlib import Path
import re
import stat
import uuid

from chronicle.atomic import atomic_replace, atomic_write_new, ensure_private_dir
from chronicle.errors import ChronicleError
from chronicle.knowledge_review import all_due_review_items
from chronicle.models import JsonValue

_START = re.compile(r"<!-- chronicle:weekly:start sha256=([0-9a-f]{64}) -->\n")
_END = "<!-- chronicle:weekly:end -->"


@dataclass(frozen=True, slots=True)
class WeeklyReviewResult:
    """The generated current-week backlog and any preserved-user-edit conflict."""

    path: Path
    item_count: int
    status: str
    conflict_copy: Path | None
    caught_up: bool


def _escape(value: str) -> str:
    """Escape generated text for a Markdown list without interpreting source content."""
    escaped = html.escape(value, quote=False)
    return escaped.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("\n", " ")


def _candidate_link(vault: Path, week_path: Path, candidate_id: str) -> str:
    """Return a relative link only for a canonical UUID candidate identity."""
    try:
        if str(uuid.UUID(candidate_id)) != candidate_id:
            raise ValueError
    except ValueError:
        return "[invalid candidate identifier withheld]"
    target = vault / "90_System/KnowledgeCandidates" / f"{candidate_id}.md"
    relative = Path(os.path.relpath(target, start=week_path.parent)).as_posix()
    return f"[`{candidate_id}`](<{relative}>)"


def _render_backlog(
    vault: Path,
    path: Path,
    week: str,
    rows: list[dict[str, JsonValue]],
    caught_up: bool,
) -> str:
    """Render a complete read-only backlog snapshot with no reviewed-state claims."""
    lines = [f"## Backlog snapshot — {week}\n\n"]
    lines.append("This is a pending-item list, not evidence that any item was reviewed.\n\n")
    if caught_up:
        lines.append(
            "Daemon catch-up restored the current backlog; skipped weeks are not marked reviewed.\n\n"
        )
    if not rows:
        lines.append("- No items currently require review.\n")
    for row in rows:
        candidate_value = row.get("candidate_id")
        candidate_id = candidate_value if isinstance(candidate_value, str) else "unknown"
        title_value = row.get("title")
        title = title_value if isinstance(title_value, str) else "[record needs attention]"
        status_value = row.get("status")
        status = status_value if isinstance(status_value, str) else "unknown"
        priority_value = row.get("priority")
        priority = str(priority_value) if isinstance(priority_value, int) else "unknown"
        due_value = row.get("review_due_at")
        due = f"; due {due_value}" if isinstance(due_value, str) else ""
        relation_value = row.get("relation_id")
        try:
            canonical_relation = str(uuid.UUID(relation_value)) if isinstance(relation_value, str) else ""
        except ValueError:
            canonical_relation = ""
        relation = f"; relation `{canonical_relation}`" if canonical_relation and canonical_relation == relation_value else ""
        list_item = f"- P{priority} **{status}** — {_escape(title)} — "
        list_item += f"{_candidate_link(vault, path, candidate_id)}{due}{relation}\n"
        lines.append(list_item)
    return "".join(lines)


def _marked(section: str) -> str:
    """Add an integrity marker to the machine-owned weekly list region."""
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    return f"<!-- chronicle:weekly:start sha256={digest} -->\n{section}{_END}\n"


def _conflict(path: Path, section: str) -> Path:
    """Publish the fresh list separately while leaving a user-edited file untouched."""
    encoded = ("# Weekly review refresh conflict\n\n" + _marked(section)).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    conflict_path = path.with_name(f"{path.stem}.conflict-{digest}.md")
    if conflict_path.exists():
        if conflict_path.is_symlink() or conflict_path.read_bytes() != encoded:
            raise ChronicleError("review_conflict", "weekly conflict copy already differs")
    else:
        atomic_write_new(conflict_path, encoded)
    return conflict_path


def _update(path: Path, section: str) -> tuple[str, Path | None]:
    """Replace a verified generated region or preserve edits in a conflict copy."""
    marked = _marked(section)
    if not path.exists():
        document = f"# Weekly knowledge review\n\n{marked}\n## User notes\n\n"
        atomic_write_new(path, document.encode("utf-8"))
        return "created", None
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ChronicleError("unsafe_path", "weekly review file must be private and user-owned")
        existing = path.read_bytes().decode("utf-8", errors="strict")
    except ChronicleError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ChronicleError("review_conflict", "weekly review file cannot be read safely") from error
    starts = list(_START.finditer(existing))
    if len(starts) != 1 or existing.count(_END) != 1:
        return "conflict", _conflict(path, section)
    start = starts[0]
    end = existing.find(_END, start.end())
    current = existing[start.end():end]
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != start.group(1):
        return "conflict", _conflict(path, section)
    end_after = end + len(_END)
    if end_after < len(existing) and existing[end_after] == "\n":
        end_after += 1
    updated = existing[:start.start()] + marked + existing[end_after:]
    if updated == existing:
        return "unchanged", None
    atomic_replace(path, updated.encode("utf-8"))
    return "updated", None


def publish_weekly_review(vault: Path, week_start: date) -> WeeklyReviewResult:
    """Write or refresh this ISO week’s local backlog without marking work reviewed."""
    year, week_number, _weekday = week_start.isocalendar()
    week = f"{year}-W{week_number:02d}"
    folder = vault / "60_Reviews/Weekly"
    ensure_private_dir(folder)
    path = folder / f"{week}.md"
    # A previous week file proves nothing about daemon execution or recovery.
    # No scheduler provenance is supplied by this local refresh command.
    caught_up = False
    rows = all_due_review_items(vault)
    section = _render_backlog(vault, path, week, rows, caught_up)
    status, conflict_copy = _update(path, section)
    return WeeklyReviewResult(path, len(rows), status, conflict_copy, caught_up)
