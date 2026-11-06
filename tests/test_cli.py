"""Actual subprocess-level CLI workflow and foreground daemon lock tests."""

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import BinaryIO, cast, override

from chronicle.models import JsonValue


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures/수직흐름_합성.md"


def _json_value(value: object) -> JsonValue:
    """Validate decoded subprocess JSON before tests inspect command results."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AssertionError("CLI emitted a non-finite JSON number")
        return value
    if isinstance(value, list):
        values = cast(list[object], value)
        return [_json_value(item) for item in values]
    if isinstance(value, dict):
        entries = cast(dict[object, object], value)
        parsed: dict[str, JsonValue] = {}
        for key, item in entries.items():
            if not isinstance(key, str):
                raise AssertionError("CLI emitted a non-string JSON key")
            parsed[key] = _json_value(item)
        return parsed
    raise AssertionError("CLI emitted a non-JSON value")


def decode_json(source: str) -> JsonValue:
    """Decode JSON from the process boundary and validate its shape recursively."""
    decoded: object = json.loads(source)  # pyright: ignore[reportAny]  # json.loads returns Any
    return _json_value(decoded)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    """Require one object-shaped CLI result."""
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    """Require one array-shaped CLI result."""
    if not isinstance(value, list):
        raise AssertionError("expected a JSON array")
    return value


def _text(value: JsonValue) -> str:
    """Require one text field in a CLI result."""
    if not isinstance(value, str):
        raise AssertionError("expected a string field")
    return value


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    """Public test helper for object-shaped CLI output."""
    return _object(value)


def json_array(value: JsonValue) -> list[JsonValue]:
    """Public test helper for array-shaped CLI output."""
    return _array(value)


def json_text(value: JsonValue) -> str:
    """Public test helper for text fields in CLI output."""
    return _text(value)


def _readline_with_timeout(stream: BinaryIO, timeout_seconds: float) -> bytes:
    """Read one child-pipe line without allowing missing readiness to hang a suite."""
    deadline = time.monotonic() + timeout_seconds
    collected = bytearray()
    with selectors.DefaultSelector() as selector:
        _ = selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError(
                    f"daemon readiness line did not arrive within {timeout_seconds:g}s"
                )
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                return bytes(collected)
            newline = chunk.find(b"\n")
            if newline >= 0:
                collected.extend(chunk[: newline + 1])
                return bytes(collected)
            collected.extend(chunk)


class CliTestCase(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.env: dict[str, str] = {}
        self.vault: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="chronicle-cli-", dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = os.environ.copy()
        self.env.update({
            "PYTHONPATH": str(PRODUCT_ROOT / "src"),
            "CHRONICLE_CONFIG_DIR": str(self.root / "config"),
            "CHRONICLE_STATE_DIR": str(self.root / "state"),
            "CHRONICLE_CACHE_DIR": str(self.root / "cache"),
        })
        self.vault = self.root / "vault"

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "chronicle", *arguments],
            cwd=PRODUCT_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def checked_json(self, *arguments: str) -> JsonValue:
        result = self.run_cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return decode_json(result.stdout)

class CliTests(CliTestCase):
    def test_foreground_daemon_holds_exclusive_writer_lock_and_releases_it(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-m", "chronicle", "daemon"],
            cwd=PRODUCT_ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        try:
            if process.stdout is None:
                self.fail("daemon stdout pipe was not created")
            try:
                ready = _readline_with_timeout(cast(BinaryIO, process.stdout), 5)
            except TimeoutError as error:
                self.fail(f"{error}; child_returncode={process.poll()}")
            self.assertIn(b'"status": "ready"', ready)
            competing = self.run_cli("daemon")
            self.assertEqual(competing.returncode, 2)
            self.assertIn("writer_busy", competing.stderr)
            process.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("daemon did not exit within 5s after its first SIGTERM")
            stderr_text = (stderr or b"").decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, stderr_text)
            self.assertIn(b'"status": "stopped"', stdout or b"")
            _ = self.checked_json("rebuild")
        finally:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    self.assertIsNotNone(process.poll(), "daemon vanished without an exit status")
                try:
                    _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.communicate(timeout=5)

    def test_foreground_daemon_collects_new_records_and_stops_cleanly(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "daemon-source"
        source_root.mkdir()
        session = source_root / "session.jsonl"
        header = {
            "type": "session", "version": 3, "id": "daemon-session",
            "timestamp": "2026-11-06T01:00:00.000Z", "cwd": "/synthetic/project",
        }

        def message(entry_id: str, content: str, timestamp_ms: int) -> bytes:
            value = {
                "type": "message", "id": entry_id, "parentId": None,
                "timestamp": "2026-11-06T01:00:01.000Z",
                "message": {"role": "user", "content": content, "timestamp": timestamp_ms},
            }
            return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

        header_bytes = (json.dumps(header, separators=(",", ":")) + "\n").encode("utf-8")
        first_message = message("daemon-first", "synthetic first", 1793926801000)
        _ = session.write_bytes(header_bytes + first_message)
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "daemon-project",
        )
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-m", "chronicle", "daemon"],
            cwd=PRODUCT_ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        try:
            if process.stdout is None:
                self.fail("daemon stdout pipe was not created")
            ready_line = _readline_with_timeout(cast(BinaryIO, process.stdout), 5)
            ready = _object(decode_json(ready_line.decode("utf-8")))
            self.assertEqual(ready["status"], "ready")
            first_summary = _object(ready["summary"])
            self.assertEqual(first_summary["new_events"], 1)
            with session.open("ab") as stream:
                _ = stream.write(message("daemon-second", "synthetic second", 1793926802000))
            next_line = _readline_with_timeout(cast(BinaryIO, process.stdout), 5)
            next_summary = _object(decode_json(next_line.decode("utf-8")))
            self.assertEqual(next_summary["new_events"], 1)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            stderr_text = (stderr or b"").decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, stderr_text)
            self.assertIn(b'"status": "stopped"', stdout or b"")
            self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 2)
        finally:
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    self.assertIsNotNone(process.poll(), "daemon vanished without an exit status")
                try:
                    _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.communicate(timeout=5)

    def test_store_root_overlapping_vault_fails_before_projection_write(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        environment = self.env.copy()
        environment["CHRONICLE_STATE_DIR"] = str(self.vault / "state")
        result = subprocess.run(
            [sys.executable, "-m", "chronicle", "rebuild"],
            cwd=PRODUCT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe_store_layout", result.stderr)
        self.assertFalse((self.vault / "state").exists())

    def test_unknown_command_returns_usage_error_without_traceback(self) -> None:
        result = self.run_cli("not-a-command")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
