"""CLI regressions for the isolated basic-records profile."""

import errno
import json
import os
import pty
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from chronicle.event_format import read_capture
from chronicle.models import JsonValue
from tests.test_cli import CliTestCase, decode_json, json_array, json_object, json_text


def _readline_with_timeout(file_descriptor: int, timeout_seconds: float) -> bytes:
    """Read one flushed CLI JSON line without allowing a hung child process."""
    deadline = time.monotonic() + timeout_seconds
    collected = bytearray()
    with selectors.DefaultSelector() as selector:
        _ = selector.register(file_descriptor, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("foreground collector readiness timed out")
            chunk = os.read(file_descriptor, 4096)
            if not chunk:
                return bytes(collected)
            newline = chunk.find(b"\n")
            if newline >= 0:
                collected.extend(chunk[: newline + 1])
                return bytes(collected)
            collected.extend(chunk)


def _json_line(value: JsonValue) -> bytes:
    """Encode one synthetic, complete Pi JSONL v3 row."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class BasicRecordsProfileTests(CliTestCase):
    def test_empty_tty_init_uses_default_and_repeat_preserves_user_file(self) -> None:
        home = self.root / "synthetic-home"
        home.mkdir()
        environment = self.env.copy()
        environment["HOME"] = str(home)
        vault = home / "Documents/Obsidian/AgentHistory"
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-m", "chronicle", "init"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        _ = os.close(slave)
        transcript = bytearray()
        try:
            _ = os.write(master, b"\n")
            _ = process.wait(timeout=10)
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                transcript.extend(chunk)
            self.assertEqual(process.returncode, 0, transcript.decode("utf-8", errors="replace"))
            self.assertIn(b"Vault path [Enter for", transcript)
            self.assertTrue(vault.is_dir())
            user_file = vault / "OwnerNote.md"
            original = b"User-owned content\n"
            _ = user_file.write_bytes(original)

            repeated = subprocess.run(
                [sys.executable, "-m", "chronicle", "init", "--vault", str(vault)],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(user_file.read_bytes(), original)
        finally:
            _ = os.close(master)
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=5)

    def _replace_knowledge_directory_with_external_symlink(self) -> None:
        """Put an inert synthetic tree outside the Vault knowledge boundary."""
        knowledge = self.vault / "90_System/KnowledgeCandidates"
        _ = knowledge.rmdir()
        external = self.root / "synthetic-knowledge"
        external.mkdir()
        _ = (external / "marker.md").write_text("synthetic only\n", encoding="utf-8")
        _ = knowledge.symlink_to(external, target_is_directory=True)

    def test_daily_cli_does_not_read_the_knowledge_tree(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        self._replace_knowledge_directory_with_external_symlink()

        result = self.run_cli("daily", "--date", "2026-11-06")

        self.assertEqual(result.returncode, 0, result.stderr)
        note = self.vault / "50_Daily/2026/11/2026-11-06.md"
        self.assertTrue(note.is_file())
        self.assertNotIn("Knowledge and review", note.read_text(encoding="utf-8"))

    def test_foreground_collector_skips_knowledge_and_weekly_maintenance(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        self._replace_knowledge_directory_with_external_symlink()
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-m", "chronicle", "daemon"],
            cwd=Path(__file__).resolve().parents[1],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        try:
            if process.stdout is None:
                self.fail("daemon stdout pipe was not created")
            ready_line = _readline_with_timeout(process.stdout.fileno(), 5)
            ready = json_object(decode_json(ready_line.decode("utf-8")))
            self.assertEqual(ready["status"], "ready")
            maintenance = json_object(ready["maintenance"])
            self.assertNotEqual(json_object(maintenance["daily_catchup"])["status"], "failed")
            self.assertNotIn("weekly_review", maintenance)
            self.assertFalse((self.vault / "60_Reviews/Weekly").exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stderr or b"").decode("utf-8", errors="replace"))
            self.assertIn(b'"status": "stopped"', stdout or b"")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                _ = process.communicate(timeout=5)

    def test_foreground_daemon_stops_cleanly_on_interrupt(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, "-m", "chronicle", "daemon"],
            cwd=Path(__file__).resolve().parents[1],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        try:
            if process.stdout is None:
                self.fail("daemon stdout pipe was not created")
            ready = json_object(decode_json(
                _readline_with_timeout(process.stdout.fileno(), 5).decode("utf-8"),
            ))
            self.assertEqual(ready["status"], "ready")
            process.send_signal(signal.SIGINT)
            stopped = json_object(decode_json(
                _readline_with_timeout(process.stdout.fileno(), 5).decode("utf-8"),
            ))
            self.assertEqual(stopped["status"], "stopped")
            _stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stderr or b"").decode("utf-8", errors="replace"))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                _ = process.communicate(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_pi_v3_cli_capture_preserves_public_message_and_tool_content(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "synthetic-pi-source"
        source_root.mkdir()
        session = source_root / "session.jsonl"
        rows: list[JsonValue] = [
            {
                "type": "session", "version": 3, "id": "basic-profile-session",
                "timestamp": "2026-11-06T01:00:00.000Z", "cwd": "/synthetic/project",
            },
            {
                "type": "message", "id": "basic-user", "parentId": None,
                "timestamp": "2026-11-06T01:01:00.000Z",
                "message": {"role": "user", "content": "대화 입력: 기본 기록을 확인한다.", "timestamp": 1793926860000},
            },
            {
                "type": "message", "id": "basic-tool-call", "parentId": "basic-user",
                "timestamp": "2026-11-06T01:01:01.000Z",
                "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": "basic-call", "name": "synthetic_reader", "arguments": {
                        "path": "src/example.py", "api_key": "FAKE-SYNTHETIC-SECRET",
                    }},
                ], "timestamp": 1793926861000},
            },
            {
                "type": "message", "id": "basic-tool-result", "parentId": "basic-tool-call",
                "timestamp": "2026-11-06T01:01:02.000Z",
                "message": {"role": "toolResult", "toolCallId": "basic-call", "toolName": "synthetic_reader",
                            "isError": False, "content": [{"type": "text", "text": "합성 도구 결과"}],
                            "timestamp": 1793926862000},
            },
            {
                "type": "message", "id": "basic-tool-error", "parentId": "basic-tool-result",
                "timestamp": "2026-11-06T01:01:03.000Z",
                "message": {"role": "toolResult", "toolCallId": "basic-call-2", "toolName": "synthetic_reader",
                            "isError": True, "content": [{"type": "text", "text": "합성 오류 결과"}],
                            "timestamp": 1793926863000},
            },
            {
                "type": "message", "id": "basic-bash", "parentId": "basic-tool-error",
                "timestamp": "2026-11-06T01:01:04.000Z",
                "message": {"role": "bashExecution", "command": "printf synthetic", "output": "합성 명령 출력",
                            "exitCode": 0, "cancelled": False, "truncated": False,
                            "timestamp": 1793926864000},
            },
            {
                "type": "message", "id": "basic-assistant-edit", "parentId": "basic-bash",
                "timestamp": "2026-11-06T01:01:05.000Z",
                "message": {"role": "assistant", "content": "문서 수정: 초안 → 수정본", "timestamp": 1793926865000},
            },
        ]
        _ = session.write_bytes(b"".join(_json_line(row) for row in rows))
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "basic-project",
        )

        collected = json_object(self.checked_json("collect", "--once"))
        status = self.run_cli("status")

        self.assertEqual(collected["new_events"], 6)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn(str(source_root), status.stdout)
        event_files = sorted((self.vault / "80_Events").rglob("*.md"))
        self.assertEqual(len(event_files), 6)
        content = "\n".join(read_capture(path)[1] for path in event_files)
        self.assertIn("대화 입력", content)
        self.assertIn("synthetic_reader", content)
        self.assertIn("합성 도구 결과", content)
        self.assertIn("Tool error", content)
        self.assertIn("합성 오류 결과", content)
        self.assertIn("합성 명령 출력", content)
        self.assertIn("문서 수정: 초안 → 수정본", content)
        self.assertNotIn("FAKE-SYNTHETIC-SECRET", content)

    def test_search_returns_vault_relative_markdown_location(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source = self.root / "synthetic-private-source" / "conversation.md"
        source.parent.mkdir()
        _ = source.write_text("한글 검색 위치 fixture\n", encoding="utf-8")
        captured = json_object(self.checked_json(
            "capture", "--file", str(source), "--source-event-id", "basic-search-event",
            "--session-id", "basic-search-session", "--title", "위치 확인",
            "--project", "basic-project", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        ))

        result = self.run_cli("search", "--query", "검색 위치", "--date", "2026-11-06", "--project", "basic-project", "--author", "Dex")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(source), result.stdout)
        event_id = json_text(captured["event_id"])
        hits = [
            json_object(row) for row in json_array(decode_json(result.stdout))
            if json_object(row).get("event_id") == event_id
            and json_object(row).get("kind") == "event"
        ]
        self.assertEqual(len(hits), 1)
        markdown_path = json_text(hits[0]["markdown_path"])
        self.assertTrue(markdown_path.startswith("80_Events/"))
        self.assertTrue((self.vault / markdown_path).is_file())
