"""Subprocess coverage for explicit, synthetic Pi session import."""

import json
from pathlib import Path

from chronicle.event_format import read_capture
from tests.test_cli import CliTestCase, json_object, json_text


class PiImportCliTests(CliTestCase):
    def _write_pi_session(self) -> Path:
        header = {
            "type": "session", "version": 3, "id": "synthetic-cli-session",
            "timestamp": "2026-11-06T01:00:00.000Z", "cwd": "/synthetic/project",
        }
        messages = [
            {
                "type": "message", "id": "synthetic-user-entry", "parentId": None,
                "timestamp": "2026-11-06T01:01:00.000Z",
                "message": {"role": "user", "content": "질문 jane@example.test", "timestamp": 1793926860000},
            },
            {
                "type": "message", "id": "synthetic-assistant-entry", "parentId": "synthetic-user-entry",
                "timestamp": "2026-11-06T01:01:01.000Z",
                "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "not imported"},
                    {"type": "text", "text": "합성 응답"},
                    {"type": "toolCall", "id": "synthetic-call", "name": "read", "arguments": {
                        "path": "src/example.py", "api_key": "synthetic-tool-secret",
                    }},
                ], "timestamp": 1793926861000},
            },
            {
                "type": "message", "id": "synthetic-tool-entry", "parentId": "synthetic-assistant-entry",
                "timestamp": "2026-11-06T01:01:02.000Z",
                "message": {"role": "toolResult", "toolCallId": "synthetic-call", "toolName": "read",
                            "isError": False,
                            "content": [{"type": "text", "text": "결과 api_key=synthetic-result-secret"}],
                            "timestamp": 1793926862000},
            },
            {
                "type": "thinking_level_change", "id": "synthetic-setting-entry",
                "parentId": "synthetic-tool-entry", "timestamp": "2026-11-06T01:01:03.000Z",
                "thinkingLevel": "high",
            },
        ]
        payload = b"".join(
            (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            for item in [header, *messages]
        )
        path = self.root / "synthetic-session.jsonl"
        _ = path.write_bytes(payload)
        return path

    def test_dry_run_import_redaction_and_idempotent_replay(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source = self._write_pi_session()
        vault_before = sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob("*"))
        dry_run = json_object(self.checked_json(
            "import", "--source", "pi-jsonl-v3", "--file", str(source),
            "--project", "pi-import-test", "--dry-run",
        ))
        self.assertEqual(dry_run["status"], "dry_run")
        self.assertEqual(dry_run["selected_records"], 3)
        self.assertEqual(dry_run["omitted_content_blocks"], 1)
        self.assertEqual(dry_run["writes"], 0)
        self.assertFalse((self.root / "state/projection.sqlite3").exists())
        self.assertEqual(vault_before, sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob("*")))

        imported = json_object(self.checked_json(
            "import", "--source", "pi-jsonl-v3", "--file", str(source),
            "--project", "pi-import-test",
        ))
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(imported["new_events"], 3)
        self.assertEqual(imported["replayed_events"], 0)
        event_paths = sorted((self.vault / "80_Events").rglob("*.md"))
        self.assertEqual(len(event_paths), 3)
        event_metadata = [read_capture(path)[0] for path in event_paths]
        self.assertEqual({json_text(item["event_type"]) for item in event_metadata}, {
            "user_message", "assistant_message", "tool_result",
        })
        self.assertTrue(all(item["source"] == "pi-jsonl" for item in event_metadata))
        self.assertTrue(all(item["source_version"] == "pi-jsonl-v3" for item in event_metadata))
        positions = {
            json_text(metadata["source_event_id"]): json_object(metadata["source_position"])
            for metadata in event_metadata
        }
        self.assertEqual(positions["synthetic-assistant-entry"]["parent_event_id"], "synthetic-user-entry")
        self.assertEqual(positions["synthetic-tool-entry"]["parent_event_id"], "synthetic-assistant-entry")
        self.assertEqual(positions["synthetic-tool-entry"]["source_call_id"], "synthetic-call")
        stored = "\n".join(path.read_text(encoding="utf-8") for path in self.vault.rglob("*.md"))
        self.assertNotIn("jane@example.test", stored)
        self.assertNotIn("synthetic-tool-secret", stored)
        self.assertNotIn("synthetic-result-secret", stored)
        self.assertNotIn("not imported", stored)
        self.assertIn("[REDACTED]", stored)

        replay = json_object(self.checked_json(
            "import", "--source", "pi-jsonl-v3", "--file", str(source),
            "--project", "pi-import-test",
        ))
        self.assertEqual(replay["new_events"], 0)
        self.assertEqual(replay["replayed_events"], 3)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 3)
        self.assertEqual(len(list((self.vault / "90_System/Checkpoints").glob("*.md"))), 3)

        _ = source.write_bytes(source.read_bytes().replace(
            b'"id":"synthetic-cli-session"', b'"id" : "synthetic-cli-session"', 1,
        ))
        moved_record = self.run_cli(
            "import", "--source", "pi-jsonl-v3", "--file", str(source),
            "--project", "pi-import-test",
        )
        self.assertEqual(moved_record.returncode, 2)
        self.assertIn("idempotency_conflict", moved_record.stderr)
        self.assertEqual(len(list((self.vault / "80_Events").rglob("*.md"))), 3)
