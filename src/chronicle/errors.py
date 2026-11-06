"""Typed application errors shared by Chronicle boundaries."""

from typing import override


class ChronicleError(Exception):
    code: str
    detail: str

    """Expected user-visible failure; mutable traceback state is owned by Python."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code, detail)

    @override
    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"
