"""Shared validation at the argparse trust boundary."""

from datetime import date

from chronicle.errors import ChronicleError


def required_text(value: str | None, name: str) -> str:
    """Validate a required parsed argument."""
    if value is None:
        raise ChronicleError("invalid_command", f"required argument was not parsed: {name}")
    return value


def optional_text(value: str | None) -> str | None:
    """Normalize optional display/filter text without fabricating an identifier."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ChronicleError("invalid_input", "optional text is too long or contains control characters")
    return cleaned


def parse_date(value: str | None) -> date | None:
    """Parse an optional ISO calendar date at the CLI boundary."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ChronicleError("invalid_input", "date must use YYYY-MM-DD") from error
