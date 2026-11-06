"""Markdown serialization for immutable event and artifact revision records."""

from pathlib import Path
import re

from chronicle.errors import ChronicleError
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import JsonValue
from chronicle.naming import fenced
from chronicle.schema import integer_field

_FENCE = re.compile(r"^(`{3,})text\n$")


def capture_body(title: str, content: str, source_name: str) -> str:
    """Render one explicit capture with a non-executable source locator."""
    return (
        f"# {title}\n\n## Captured content\n\n{fenced(content)}"
        f"\n## Source locator\n\nManual file: `{source_name}`\n"
    )


def extract_capture_content(body: str, content_chars: int) -> str:
    """Extract exact captured text from our fenced Markdown representation."""
    lines = body.splitlines(keepends=True)
    marker_index = next((i for i, line in enumerate(lines) if line == "## Captured content\n"), -1)
    if marker_index < 0 or marker_index + 2 >= len(lines) or lines[marker_index + 1] != "\n":
        raise ChronicleError("invalid_event", "captured-content section is missing")
    opening = _FENCE.fullmatch(lines[marker_index + 2])
    if opening is None:
        raise ChronicleError("invalid_event", "captured-content fence is missing")
    fence = opening.group(1)
    closing_index = next(
        (i for i in range(marker_index + 3, len(lines)) if lines[i].rstrip("\n") == fence),
        -1,
    )
    if closing_index < 0:
        raise ChronicleError("invalid_event", "captured-content fence is incomplete")
    text = "".join(lines[marker_index + 3 : closing_index])
    if len(text) < content_chars:
        raise ChronicleError("invalid_event", "captured-content length is shorter than metadata")
    return text[:content_chars]


def read_capture(path: Path) -> tuple[dict[str, JsonValue], str]:
    """Read one event or revision and verify its declared content length."""
    metadata, body, _normalized = parse_frontmatter(path.read_bytes())
    content = extract_capture_content(body, integer_field(metadata, "content_chars"))
    return metadata, content


def render_capture(metadata: dict[str, JsonValue], title: str, content: str, source_name: str) -> str:
    """Serialize an event or revision as full Markdown with its exact source text."""
    return render_markdown(metadata, capture_body(title, content, source_name))
