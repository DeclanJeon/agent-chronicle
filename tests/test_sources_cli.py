"""Synthetic end-to-end tests for opt-in source collection."""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from chronicle.collector import collect_once
from chronicle.collector_files import MAX_FILE_BYTES, MAX_LINE_BYTES
from chronicle.event_format import read_capture
from chronicle.models import CaptureContext, JsonValue
from chronicle.paths import environment_paths, load_config
from tests.test_cli import CliTestCase, decode_json, json_array, json_object, json_text


def _line(value: dict[str, JsonValue]) -> bytes:
    """Encode one synthetic JSONL record with a complete LF terminator."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _header(session_id: str) -> dict[str, JsonValue]:
    return {
        "type": "session", "version": 3, "id": session_id,
        "timestamp": "2026-11-06T01:00:00.000Z", "cwd": "/synthetic/project",
    }


def _message(entry_id: str, text: str, timestamp_ms: int) -> dict[str, JsonValue]:
    return {
        "type": "message", "id": entry_id, "parentId": None,
        "timestamp": "2026-11-06T01:00:01.000Z",
        "message": {"role": "user", "content": text, "timestamp": timestamp_ms},
    }


class SourceCollectorCliTests(CliTestCase):
    def test_registry_is_empty_until_explicit_enable_and_can_disable(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "selected-source"
        source_root.mkdir()
        _ = (source_root / "session.jsonl").write_bytes(_line(_header("registry-session")))

        self.assertEqual(json_array(self.checked_json("sources", "list")), [])
        enabled = json_object(self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        ))
        source_id = json_text(enabled["source_id"])
        self.assertEqual(enabled["status"], "enabled")
        self.assertEqual(len(json_array(self.checked_json("sources", "list"))), 1)

        disabled = json_object(self.checked_json("sources", "disable", source_id))
        self.assertEqual(disabled["status"], "disabled")
        rows = json_array(self.checked_json("sources", "list"))
        self.assertFalse(json_object(rows[0])["enabled"])

    def test_collector_holds_partial_utf8_record_and_resumes_after_restart(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "selected-source"
        source_root.mkdir()
        session = source_root / "session.jsonl"
        first = _line(_header("partial-session")) + _line(
            _message("first", "완료 레코드", 1793926801000)
        )
        second = _line(_message("second", "한글 이어쓰기", 1793926802000))
        partial = second[:-4]
        _ = session.write_bytes(first + partial)
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        )

        first_run = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(first_run["new_events"], 1)
        status = json_object(self.checked_json("status"))
        file_status = json_object(json_array(status["source_files"])[0])
        self.assertEqual(file_status["status"], "partial")
        self.assertEqual(file_status["pending_bytes"], len(partial))

        with session.open("ab") as stream:
            _ = stream.write(second[len(partial):])
        second_run = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(second_run["new_events"], 1)
        resumed = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(resumed["new_events"], 0)
        self.assertEqual(resumed["replayed_events"], 0)

        events = sorted((self.vault / "80_Events").rglob("*.md"))
        self.assertEqual(len(events), 2)
        contents = [read_capture(path)[1] for path in events]
        self.assertEqual(contents, ["완료 레코드", "한글 이어쓰기"])
        self.assertEqual(len(list((self.vault / "90_System/Checkpoints").glob("*.md"))), 2)
        self.assertEqual(json_object(self.checked_json("daily", "--date", "2026-11-06"))["event_count"], 2)

        shutil.rmtree(self.root / "state/collector-state")
        recovered = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(recovered["new_events"], 0)
        self.assertEqual(recovered["replayed_events"], 2)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 2)

        database = self.root / "state/projection.sqlite3"
        database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        rebuilt = json_object(self.checked_json("rebuild"))
        self.assertEqual(rebuilt["committed_events"], 2)

    def test_rotation_and_truncate_create_generations_and_report_gaps(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "rotating-source"
        source_root.mkdir()
        active = source_root / "session.jsonl"
        original = _line(_header("rotation-session")) + _line(
            _message("before-rotation", "회전 전", 1793926801000)
        )
        _ = active.write_bytes(original)
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        )
        initial = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(initial["new_events"], 1)

        rotated = source_root / "session.jsonl.1"
        _ = active.rename(rotated)
        _ = active.write_bytes(
            _line(_header("rotation-session-next")) + _line(
                _message("after-rotation", "회전 후", 1793926802000)
            )
        )
        rotated_run = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(rotated_run["new_events"], 1)

        same_inode = active.stat().st_ino
        _ = active.write_bytes(_line(_header("rotation-session-next")))
        self.assertEqual(active.stat().st_ino, same_inode)
        truncated_run = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(truncated_run["new_events"], 0)
        with active.open("ab") as stream:
            _ = stream.write(_line(_message("after-truncate", "truncate 재시작", 1793926803000)))
        resumed_run = json_object(self.checked_json("collect", "--once"))
        self.assertEqual(resumed_run["new_events"], 1)
        status = json_object(self.checked_json("status"))
        gaps = json_array(status["coverage_gaps"])
        self.assertTrue(any(json_object(item)["reason"] == "file_truncated" for item in gaps))
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 3)

    def test_record_limit_reports_backpressure_and_resumes_without_dropping_events(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "bounded-source"
        source_root.mkdir()
        session = source_root / "session.jsonl"
        records = [_line(_header("bounded-session"))]
        records.extend(
            _line(_message(f"bounded-{index}", f"record {index}", 1793926801000 + index))
            for index in range(5)
        )
        encoded = b"".join(records)
        _ = session.write_bytes(encoded)
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        )

        with patch.dict(os.environ, self.env):
            paths = environment_paths()
            context = CaptureContext(load_config(paths), paths)
            with patch("chronicle.collector_process.MAX_RECORDS_PER_FILE_PASS", 2):
                first = collect_once(context)
                self.assertEqual(first.new_events, 2)
                state = json_object(self.checked_json("status"))
                file_state = json_object(json_array(state["source_files"])[0])
                self.assertEqual(file_state["status"], "backpressure")
                cursor = file_state.get("cursor_byte")
                if not isinstance(cursor, int):
                    self.fail("collector cursor is not an integer")
                self.assertLess(cursor, len(encoded))
                second = collect_once(context)
                third = collect_once(context)
        self.assertEqual(second.new_events, 2)
        self.assertEqual(third.new_events, 1)
        self.assertEqual(third.pending_files, 0)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 5)

    def test_file_and_line_limits_fail_closed_without_acknowledging_content(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "limit-source"
        source_root.mkdir()
        oversized = source_root / "oversized.jsonl"
        _ = oversized.write_bytes(_line(_header("oversized-session")))
        with oversized.open("ab") as stream:
            _ = stream.truncate(MAX_FILE_BYTES + 1)
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        )
        file_run = self.run_cli("collect", "--once")
        self.assertEqual(file_run.returncode, 1)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 0)
        state = json_object(self.checked_json("status"))
        failed = json_object(json_array(state["source_files"])[0])
        self.assertEqual(failed["last_error"], "input_too_large")

        oversized.unlink()
        line_file = source_root / "long-line.jsonl"
        _ = line_file.write_bytes(
            _line(_header("long-line-session")) + b"{\"type\":\"message\",\"padding\":\""
            + (b"x" * (MAX_LINE_BYTES + 1)) + b"\"}\n"
        )
        line_run = self.run_cli("collect", "--once")
        self.assertEqual(line_run.returncode, 1)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 0)
        status = json_object(self.checked_json("status"))
        errors = [json_object(item).get("last_error") for item in json_array(status["source_files"])]
        self.assertIn("record_too_large", errors, str(status))

    def test_unsupported_source_failure_is_isolated_and_visible(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "mixed-source"
        source_root.mkdir()
        _ = (source_root / "good.jsonl").write_bytes(
            _line(_header("good-session")) + _line(_message("good", "정상", 1793926801000))
        )
        _ = (source_root / "unsupported.jsonl").write_bytes(
            _line({**_header("unsupported-session"), "version": 2})
        )
        _ = self.checked_json(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "synthetic-project",
        )

        result = self.run_cli("collect", "--once")
        self.assertEqual(result.returncode, 1)
        output = json_object(decode_json(result.stdout))
        self.assertEqual(output["new_events"], 1)
        status = json_object(self.checked_json("status"))
        self.assertTrue(any(json_object(item)["status"] == "failed" for item in json_array(status["source_files"])))
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 1)

    def test_source_symlink_is_rejected_before_registry_write(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        actual_root = self.root / "actual"
        actual_root.mkdir()
        link_root = self.root / "linked"
        os.symlink(actual_root, link_root)

        result = self.run_cli(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(link_root),
            "--project", "synthetic-project",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe_input", result.stderr)
        self.assertEqual(json_array(self.checked_json("sources", "list")), [])

