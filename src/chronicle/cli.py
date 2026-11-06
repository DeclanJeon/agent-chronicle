"""CLI process entry point for the local-only MVP."""

from collections.abc import Sequence
import json
import sys

from chronicle.cli_requests import parse_command
from chronicle.commands import execute
from chronicle.errors import ChronicleError


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning process status for the caller."""
    try:
        return execute(parse_command(argv))
    except ChronicleError as error:
        print(json.dumps({"error": error.code, "detail": error.detail}, ensure_ascii=True), file=sys.stderr)
        return 2
