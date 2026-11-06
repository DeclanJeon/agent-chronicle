"""Explicit, conservative masking and rejection for local text."""

import re
import unicodedata
from typing import Final

from chronicle.errors import ChronicleError

_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|"
        + r"cookie|session(?:[_-]?(?:id|token|key))?)\b(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_COOKIE_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(set-cookie|cookie)([ \t]*:[ \t]*)[^\n]*"
)
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)


def normalize_text(text: str) -> str:
    """Apply the documented Unicode normalization without trimming content."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _redact_normalized(text: str) -> str:
    """Apply the explicit supported masking patterns to normalized text."""
    output = _COOKIE_HEADER_PATTERN.sub(r"\1\2[REDACTED]", text)
    output = _SECRET_PATTERNS[0].sub(r"\1\2[REDACTED]", output)
    output = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", output)
    output = _SECRET_PATTERNS[2].sub("[REDACTED]", output)
    return _EMAIL_PATTERN.sub("[REDACTED]", output)


def contains_sensitive_pattern(text: str) -> bool:
    """Return whether the existing explicit maskers would change normalized text."""
    normalized = normalize_text(text)
    return _redact_normalized(normalized) != normalized


def reject_sensitive_text(*values: str) -> None:
    """Reject known-sensitive knowledge text without reflecting field or value."""
    if any(contains_sensitive_pattern(value) for value in values):
        raise ChronicleError(
            "sensitive_knowledge",
            "knowledge content contains a recognized sensitive pattern",
        )


def reject_sensitive_metadata(*values: str) -> None:
    """Fail closed without echoing capture metadata matched by the supported maskers."""
    if any(contains_sensitive_pattern(value) for value in values):
        raise ChronicleError(
            "sensitive_metadata",
            "capture metadata contains a recognized sensitive pattern",
        )


def redact_text(text: str) -> str:
    """Mask recognizable credential assignments before durable capture."""
    return _redact_normalized(normalize_text(text))
