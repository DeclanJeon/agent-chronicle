"""Rule-only, read-only extraction from user-marked knowledge zones."""

from collections import OrderedDict
from pathlib import Path
import re

from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.models import JsonValue
from chronicle.privacy import contains_sensitive_pattern, normalize_text, reject_sensitive_metadata
from chronicle.schema import text_field

_SECTIONS = {
    "배운 점": ("lesson", "배운 점"),
    "배운점": ("lesson", "배운 점"),
    "결정": ("decision", "결정"),
    "문제와 해결": ("lesson", "문제와 해결"),
    "문제/해결": ("lesson", "문제와 해결"),
    "아이디어": ("idea", "아이디어"),
    "남은 질문": ("question", "남은 질문"),
    "미해결 질문": ("question", "남은 질문"),
}
_TAGS = {
    "concept": ("concept", "concept"),
    "lesson": ("lesson", "lesson"),
    "decision": ("decision", "decision"),
    "procedure": ("procedure", "procedure"),
    "idea": ("idea", "idea"),
    "question": ("question", "question"),
    "preference": ("preference", "preference"),
}
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_TAG = re.compile(r"^\s{0,3}#\s*(?:knowledge[:/])?([a-z]+)\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


def _zone(line: str) -> tuple[str, str] | None:
    """Return an explicit section or tag type and inert display label."""
    heading = _HEADING.fullmatch(line)
    candidate = heading.group(1).strip().rstrip("# ").strip() if heading else line
    folded = candidate.casefold()
    if folded in _SECTIONS:
        return _SECTIONS[folded]
    tag = _TAG.fullmatch(line)
    if tag is not None:
        return _TAGS.get(tag.group(1).casefold())
    if heading is not None:
        return None
    return None


def _marked_blocks(content: str) -> list[tuple[str, str, str]]:
    """Extract exact paragraphs or list items only from marked zones."""
    lines = normalize_text(content).splitlines()
    selected: list[tuple[str, str, list[str]]] = []
    active: tuple[str, str] | None = None
    current: list[str] = []
    in_fence = False
    fence_char = ""
    fence_length = 0

    def flush() -> None:
        nonlocal current
        quote = normalize_text("\n".join(current).strip())
        if active is not None and quote:
            selected.append((active[0], active[1], [quote]))
        current = []

    for line in lines:
        fence = _FENCE.match(line)
        if fence is not None:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                in_fence = False
            if active is not None:
                current.append(line)
            continue
        if in_fence:
            if active is not None:
                current.append(line)
            continue
        heading = _HEADING.fullmatch(line)
        tagged_zone = _zone(line)
        if heading is not None or tagged_zone is not None:
            flush()
            active = tagged_zone
            continue
        if active is None:
            continue
        if not line.strip():
            flush()
        elif _LIST_ITEM.match(line) and current:
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return [
        (knowledge_type, label, blocks[0])
        for knowledge_type, label, blocks in selected
        if blocks
    ]


def _event_rows(vault: Path, session_id: str) -> list[tuple[str, str, str, str, str]]:
    """Read matching events only through verified committed checkpoint references."""
    snapshot = verify_checkpoint_chain(vault)
    rows: list[tuple[str, str, str, str, str]] = []
    for reference in snapshot.references:
        path = vault / reference.relative_path
        metadata, content = read_capture(path)
        if text_field(metadata, "source_session_id") != session_id:
            continue
        event_id = text_field(metadata, "event_id")
        revision_id = text_field(metadata, "revision_id")
        reject_sensitive_metadata(event_id, revision_id)
        rows.append((
            text_field(metadata, "occurred_at"), event_id, revision_id,
            text_field(metadata, "project_id"), content,
        ))
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def preview_session_distillation(vault: Path, session_id: str) -> dict[str, JsonValue]:
    """Return grouped, exact-quote proposals without writing candidates or calling models."""
    selected_session = session_id.strip()
    reject_sensitive_metadata(selected_session)
    if not selected_session or len(selected_session) > 256:
        raise ChronicleError("invalid_input", "session ID must be bounded non-empty text")
    grouped: OrderedDict[tuple[str, str, str], list[JsonValue]] = OrderedDict()
    group_labels: dict[tuple[str, str, str], set[str]] = {}
    withheld: list[JsonValue] = []
    event_rows = _event_rows(vault, selected_session)
    for _occurred_at, event_id, revision_id, project_id, content in event_rows:
        for knowledge_type, label, quote in _marked_blocks(content):
            source: dict[str, JsonValue] = {
                "event_id": event_id,
                "revision_id": revision_id,
                "locator": f"event:{event_id}#captured-content",
                "zone": label,
                "project_id": project_id,
            }
            if contains_sensitive_pattern(quote):
                withheld.append({
                    "knowledge_type": knowledge_type,
                    "status": "withheld_sensitive_content",
                    "source_event_id": event_id,
                    "zone": label,
                    "quote_returned": False,
                })
                continue
            group_key = (project_id, knowledge_type, quote)
            grouped.setdefault(group_key, []).append(source)
            group_labels.setdefault(group_key, set()).add(label)
    items: list[JsonValue] = []
    for (project_id, knowledge_type, quote), sources in grouped.items():
        labels = sorted(group_labels[(project_id, knowledge_type, quote)]) or ["knowledge"]
        items.append({
            "knowledge_type": knowledge_type,
            "project_id": project_id,
            "title": f"{labels[0]} candidate",
            "claim": quote,
            "exact_quote": quote,
            "sources": sources,
            "duplicate_evidence_count": len(sources),
            "status": "candidate_preview_only",
            "persisted": False,
        })
    return {
        "status": "preview_only" if items or withheld else "no_marked_zones",
        "session_id": selected_session,
        "committed_events_scanned": len(event_rows),
        "extractor": "marked-zones-v1",
        "items": items,
        "privacy_withheld": withheld,
        "writes": 0,
        "model_calls": 0,
        "external_transmission": False,
    }
