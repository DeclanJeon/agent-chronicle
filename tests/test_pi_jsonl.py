"""Version-pinned Pi JSONL import parsing regressions."""

import json
from pathlib import Path
import tempfile
import unittest

from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.pi_jsonl import parse_pi_jsonl
from chronicle.pi_jsonl_models import PiSession


def _line(value: JsonValue) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _sample_session() -> tuple[bytes, list[bytes]]:
    header = _line({
        "type": "session", "version": 3, "id": "synthetic-session-01",
        "timestamp": "2026-11-06T01:00:00.000Z", "cwd": "/synthetic/project",
    })
    entries = [
        _line({
            "type": "message", "id": "entry-user-01", "parentId": None,
            "timestamp": "2026-11-06T01:01:00.000Z",
            "message": {"role": "user", "content": "합성 질문 jane@example.test", "timestamp": 1793926860000},
        }),
        _line({
            "type": "message", "id": "entry-assistant-01", "parentId": "entry-user-01",
            "timestamp": "2026-11-06T01:01:01.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "private reasoning is not imported"},
                {"type": "text", "text": "합성 답변"},
                {"type": "toolCall", "id": "call-01", "name": "read", "arguments": {
                    "path": "src/example.py", "api_key": "synthetic-tool-secret",
                }},
            ], "timestamp": 1793926861000},
        }),
        _line({
            "type": "message", "id": "entry-tool-01", "parentId": "entry-assistant-01",
            "timestamp": "2026-11-06T01:01:02.000Z",
            "message": {"role": "toolResult", "toolCallId": "call-01", "toolName": "read",
                        "isError": False, "content": [{"type": "text", "text": "합성 결과 api_key=synthetic-result-secret"}],
                        "timestamp": 1793926862000},
        }),
        _line({
            "type": "thinking_level_change", "id": "entry-setting-01", "parentId": "entry-tool-01",
            "timestamp": "2026-11-06T01:01:03.000Z", "thinkingLevel": "high",
        }),
    ]
    return header, entries


class PiJsonlParserTests(unittest.TestCase):
    def parse_bytes(self, raw: bytes, *, start: int = 0, end: int | None = None) -> PiSession:
        with tempfile.TemporaryDirectory(prefix="chronicle-pi-parser-") as temporary:
            path = Path(temporary) / "synthetic-session.jsonl"
            _ = path.write_bytes(raw)
            return parse_pi_jsonl(path, start_byte=start, end_byte=end)

    def test_parses_v3_messages_with_byte_provenance_and_skips_private_thinking(self) -> None:
        header, entries = _sample_session()
        session = self.parse_bytes(header + b"".join(entries))

        self.assertEqual(session.version, 3)
        self.assertEqual(session.session_id, "synthetic-session-01")
        self.assertEqual(len(session.records), 3)
        self.assertEqual(session.skipped_entries, 1)
        self.assertEqual(session.records[0].event_type, "user_message")
        self.assertEqual(session.records[1].event_type, "assistant_message")
        self.assertEqual(session.records[2].event_type, "tool_result")
        self.assertEqual(session.records[1].parent_event_id, "entry-user-01")
        self.assertEqual(session.records[2].source_call_id, "call-01")
        self.assertEqual(session.records[1].omitted_content_blocks, 1)
        self.assertEqual(session.omitted_content_blocks, 1)
        self.assertEqual(session.records[0].record_start_byte, len(header))
        self.assertEqual(session.records[0].record_end_byte_exclusive, len(header + entries[0]))
        self.assertNotIn("private reasoning", session.records[1].content)
        self.assertNotIn("synthetic-tool-secret", session.records[1].content)
        self.assertIn("[REDACTED]", session.records[1].content)
        self.assertEqual(session.pending_bytes, 0)

    def test_range_must_align_to_complete_record_boundaries(self) -> None:
        header, entries = _sample_session()
        raw = header + b"".join(entries)
        second_start = len(header + entries[0])
        second_end = len(header + entries[0] + entries[1])

        selected = self.parse_bytes(raw, start=second_start, end=second_end)
        self.assertEqual([item.source_event_id for item in selected.records], ["entry-assistant-01"])
        with self.assertRaisesRegex(ChronicleError, "invalid_import_range"):
            _ = self.parse_bytes(raw, start=second_start + 1)

    def test_incomplete_trailing_record_is_pending_not_imported(self) -> None:
        header, entries = _sample_session()
        partial = b'{"type":"message","id":"partial","secret":"not-for-output"'
        session = self.parse_bytes(header + b"".join(entries[:1]) + partial)

        self.assertEqual(len(session.records), 1)
        self.assertEqual(session.pending_bytes, len(partial))
        self.assertEqual(session.complete_end_byte, len(header + entries[0]))

    def test_rejects_unknown_session_version_without_echoing_input(self) -> None:
        header, entries = _sample_session()
        changed = header.replace(b'"version":3', b'"version":99')
        with self.assertRaisesRegex(ChronicleError, "unsupported_source_version") as raised:
            _ = self.parse_bytes(changed + b"".join(entries))
        self.assertNotIn("synthetic-session-01", str(raised.exception))

    def test_rejects_duplicate_json_keys_and_malformed_complete_records(self) -> None:
        header, _entries = _sample_session()
        for bad_line in (
            b'{"type":"message","type":"usage"}\n',
            b'{bad json}\n',
            b'{"type":"message","value":NaN}\n',
        ):
            with self.subTest(bad_line=bad_line[:16]), self.assertRaisesRegex(ChronicleError, "invalid_pi_session"):
                _ = self.parse_bytes(header + bad_line)


if __name__ == "__main__":
    _ = unittest.main()
