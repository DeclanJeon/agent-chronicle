"""Synthetic source registry and collector path privacy regressions."""

import json
import os

from chronicle.models import JsonValue
from tests.test_cli import CliTestCase, decode_json, json_array, json_object, json_text


class SourcePathPrivacyCliTests(CliTestCase):
    def test_sensitive_scope_is_rejected_without_registry_mutation(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        sensitive_root = self.root / "qa-root@example.test"
        sensitive_root.mkdir()

        result = self.run_cli(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(sensitive_root),
            "--project", "safe-project",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("sensitive_metadata", result.stderr)
        self.assertNotIn("qa-root@example.test", result.stdout + result.stderr)
        self.assertEqual(json_array(self.checked_json("sources", "list")), [])
        registry = self.root / "config/sources"
        self.assertFalse(registry.exists())

    def test_legacy_sensitive_scope_remains_listable_without_path_disclosure(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        sensitive_root = self.root / "qa-root@example.test"
        sensitive_root.mkdir()
        source_id = "00000000-0000-5000-8000-000000000001"
        registry = self.root / "config/sources"
        registry.mkdir(mode=0o700)
        entry: dict[str, JsonValue] = {
            "schema_version": 1,
            "source_id": source_id,
            "adapter_id": "pi-jsonl-v3",
            "adapter_version": "3",
            "policy_version": "redact-omit-nontext-v1",
            "scope_kind": "root",
            "scope_path": str(sensitive_root),
            "project_id": "safe-project",
            "title": "synthetic source",
            "enabled": True,
            "created_at": "2026-11-06T00:00:00+00:00",
        }
        registry_file = registry / f"{source_id}.json"
        _ = registry_file.write_text(json.dumps(entry), encoding="utf-8")
        os.chmod(registry_file, 0o600)

        result = self.run_cli("sources", "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("qa-root@example.test", result.stdout + result.stderr)
        rows = json_array(decode_json(result.stdout))
        self.assertEqual(len(rows), 1)
        row = json_object(rows[0])
        self.assertEqual(json_text(row["scope"]), "[root scope withheld]")
        self.assertEqual(registry_file.read_text(encoding="utf-8"), json.dumps(entry))

    def test_unknown_registry_fields_fail_closed_without_rewriting_entry(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "safe-source"
        source_root.mkdir()
        source_id = "00000000-0000-5000-8000-000000000002"
        registry = self.root / "config/sources"
        registry.mkdir(mode=0o700)
        entry: dict[str, JsonValue] = {
            "schema_version": 1,
            "source_id": source_id,
            "adapter_id": "pi-jsonl-v3",
            "adapter_version": "3",
            "policy_version": "redact-omit-nontext-v1",
            "scope_kind": "root",
            "scope_path": str(source_root),
            "project_id": "safe-project",
            "title": "synthetic source",
            "enabled": True,
            "created_at": "2026-11-06T00:00:00+00:00",
            "unexpected": "qa-secret@example.test",
        }
        registry_file = registry / f"{source_id}.json"
        original = json.dumps(entry).encode("utf-8")
        _ = registry_file.write_bytes(original)
        os.chmod(registry_file, 0o600)

        result = self.run_cli("sources", "list")

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid_source_registry", result.stderr)
        self.assertNotIn("qa-secret@example.test", result.stdout + result.stderr)
        self.assertEqual(registry_file.read_bytes(), original)

    def test_sensitive_relative_filename_is_redacted_in_status_and_state(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source_root = self.root / "selected-source"
        source_root.mkdir()
        session = source_root / "qa-person@example.test.jsonl"
        payload = json.dumps({
            "type": "session", "version": 3, "id": "safe-session",
            "timestamp": "2026-11-06T01:00:00Z", "cwd": "/synthetic/project",
        })
        _ = session.write_text(payload + "\n", encoding="utf-8")
        enabled = self.run_cli(
            "sources", "enable", "--source", "pi-jsonl-v3", "--root", str(source_root),
            "--project", "safe-project",
        )
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertNotIn(str(source_root), enabled.stdout + enabled.stderr)
        self.assertEqual(self.run_cli("collect", "--once").returncode, 0)
        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn("qa-person@example.test", status.stdout + status.stderr)
        source_files = json_object(decode_json(status.stdout))["source_files"]
        row = json_object(json_array(source_files)[0])
        self.assertEqual(json_text(row["relative_path"]), "[REDACTED]")
        state_files = list((self.root / "state/collector-state/files").glob("*.json"))
        self.assertEqual(len(state_files), 1)
        self.assertNotIn("qa-person@example.test", state_files[0].read_text(encoding="utf-8"))
