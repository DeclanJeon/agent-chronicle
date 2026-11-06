"""Deterministic knowledge and review summaries for daily Markdown notes."""

from datetime import date, datetime
import html
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from chronicle.errors import ChronicleError
from chronicle.knowledge_common import decision_files
from chronicle.knowledge_review import all_due_review_items
from chronicle.markdown import parse_frontmatter
from chronicle.models import JsonValue
from chronicle.privacy import contains_sensitive_pattern, reject_sensitive_metadata
from chronicle.schema import list_field, text_field

_ALERT_STATUSES = frozenset({
    "approval_snapshot_changed", "source_missing", "source_policy_changed",
    "review_due", "review_due_date_missing", "review_due_date_invalid",
    "privacy_review", "relation_pending_review",
})


def _escape_markdown_text(value: str) -> str:
    """Render untrusted text as inert Markdown text, never as syntax."""
    escaped = html.escape(value, quote=False)
    return escaped.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("\n", " ")


def _link(note_directory: Path, target: Path, label: str) -> str:
    """Make a relative local link with inert escaped display text."""
    relative = Path(os.path.relpath(target, start=note_directory)).as_posix()
    escaped = _escape_markdown_text(label)
    return f"[{escaped}](<{relative}>)"


def _local_day(value: str, timezone: ZoneInfo) -> date:
    """Convert an aware metadata timestamp to the configured calendar date."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ChronicleError("invalid_candidate", "knowledge date is invalid") from error
    if parsed.tzinfo is None:
        raise ChronicleError("invalid_candidate", "knowledge timestamp must include a timezone")
    return parsed.astimezone(timezone).date()


def _decision_status(vault: Path, candidate_id: str) -> tuple[str, list[tuple[Path, dict[str, JsonValue]]]]:
    """Return current decision state and its verified immutable history."""
    history = decision_files(vault, candidate_id)
    return (text_field(history[-1][1], "decision") if history else "candidate"), history


def _candidate_rows(vault: Path, note_directory: Path, day: date, timezone: ZoneInfo) -> tuple[list[str], list[str], int]:
    """Collect today’s active candidates and unresolved questions without exposing tainted titles."""
    root = vault / "90_System/KnowledgeCandidates"
    if root.is_symlink() or not root.is_dir():
        raise ChronicleError("unsafe_path", "knowledge candidate directory is missing or unsafe")
    todays: list[str] = []
    questions: list[str] = []
    invalid = 0
    for path in sorted(root.glob("*.md")):
        try:
            if path.is_symlink() or not path.is_file():
                raise ChronicleError("unsafe_path", "candidate entry is not a regular file")
            metadata, _body, normalized = parse_frontmatter(path.read_bytes())
            candidate_id = text_field(metadata, "candidate_id")
            if candidate_id != path.stem or text_field(metadata, "kind") != "knowledge_note":
                raise ChronicleError("invalid_candidate", "candidate identity differs from its file")
            status, history = _decision_status(vault, candidate_id)
            for decision_path, decision in history:
                if _local_day(text_field(decision, "created_at"), timezone) == day:
                    action = text_field(decision, "decision")
                    safe_title = "[privacy review required]" if contains_sensitive_pattern(normalized) else text_field(metadata, "title")
                    todays.append(f"- {_link(note_directory, decision_path, safe_title)} — {action}\n")
            if status in ("reject", "retract"):
                continue
            if _local_day(text_field(metadata, "created_at"), timezone) == day:
                safe_title = "[privacy review required]" if contains_sensitive_pattern(normalized) else text_field(metadata, "title")
                knowledge_type = metadata.get("knowledge_type", "lesson")
                type_label = (
                    knowledge_type
                    if isinstance(knowledge_type, str) and knowledge_type in {
                        "concept", "lesson", "decision", "procedure", "idea", "question", "preference",
                    }
                    else "unknown"
                )
                reference = _link(note_directory, path, safe_title)
                source_rows = list_field(metadata, "sources")
                event_ids: list[str] = []
                for source in source_rows:
                    if not isinstance(source, dict):
                        continue
                    event_id = source.get("artifact_id")
                    if isinstance(event_id, str):
                        reject_sensitive_metadata(event_id)
                        event_ids.append(event_id)
                sources = ", ".join(f"`{event_id}`" for event_id in event_ids) or "source unavailable"
                todays.append(f"- {reference} — {type_label}, {status}; source event {sources}\n")
            if metadata.get("knowledge_type") == "question":
                safe_title = "[privacy review required]" if contains_sensitive_pattern(normalized) else text_field(metadata, "title")
                questions.append(f"- {_link(note_directory, path, safe_title)} — open question\n")
        except (ChronicleError, OSError, UnicodeDecodeError):
            invalid += 1
    return todays, questions, invalid


def _alert_rows(vault: Path) -> list[str]:
    """Expose only actionable current warning states, not all pending candidates."""
    warnings: list[str] = []
    for row in all_due_review_items(vault):
        status = row.get("status")
        if not isinstance(status, str) or status not in _ALERT_STATUSES:
            continue
        title = row.get("title")
        label = title if isinstance(title, str) else "Knowledge relation"
        escaped = _escape_markdown_text(label)
        warnings.append(f"- {escaped} — {status}\n")
    return warnings


def daily_knowledge_section(vault: Path, note_path: Path, day: date, timezone_name: str) -> str:
    """Render candidate, decision, question, reuse, and warning sections for one daily view."""
    timezone = ZoneInfo(timezone_name)
    todays, questions, invalid = _candidate_rows(vault, note_path.parent, day, timezone)
    warnings = _alert_rows(vault)
    lines = ["\n## Knowledge and review\n\n### Candidates and decisions\n"]
    lines.extend(todays[:50] or ["- None recorded for this date.\n"])
    if len(todays) > 50:
        lines.append(f"- {len(todays) - 50} additional entries omitted from this view.\n")
    lines.append("\n### Unresolved questions\n")
    lines.extend(questions[:50] or ["- None recorded.\n"])
    if len(questions) > 50:
        lines.append(f"- {len(questions) - 50} additional questions omitted from this view.\n")
    lines.extend([
        "\n### Knowledge reuse outcomes\n",
        "- No knowledge-reuse outcome events are currently recorded.\n",
        "\n### Current evidence and review warnings\n",
    ])
    lines.extend(warnings[:100] or ["- No actionable warning is currently listed.\n"])
    if len(warnings) > 100:
        lines.append(f"- {len(warnings) - 100} additional warnings remain in `review --due` pages.\n")
    if invalid:
        lines.append(f"\n- {invalid} malformed candidate record(s) were withheld from this summary.\n")
    lines.append("\nThis generated section is a review aid; it does not approve, resolve, or mark items reviewed.\n")
    return "".join(lines)
