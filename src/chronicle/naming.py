"""Stable, path-safe Unicode names and fenced Markdown blocks."""

import hashlib
import re
import unicodedata

_RESERVED = re.compile(r"[\x00-\x1f/\\:*?\"<>|]+")


def safe_slug(value: str, max_bytes: int = 80) -> str:
    """Create a bounded display slug without allowing path separators."""
    normalized = unicodedata.normalize("NFC", value).strip()
    slug = _RESERVED.sub("_", normalized).replace("..", "_").strip(" .")
    if not slug:
        slug = "untitled"
    while len(slug.encode("utf-8")) > max_bytes:
        slug = slug[:-1]
    return slug or "untitled"


def fenced(text: str, language: str = "text") -> str:
    """Fence arbitrary user text with a delimiter longer than its backtick runs."""
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}{'' if text.endswith(chr(10)) else chr(10)}{fence}\n"


def sha256_text(text: str) -> str:
    """Return the lowercase SHA-256 of strict UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
