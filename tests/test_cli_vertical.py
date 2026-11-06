"""Synthetic end-to-end CLI flows kept separate from parser and daemon tests."""

from pathlib import Path

from chronicle.event_format import read_capture
from tests.test_cli import (
    CliTestCase,
    FIXTURE,
    decode_json,
    json_array as _array,
    json_object as _object,
    json_text as _text,
)


class CliVerticalTests(CliTestCase):
    def test_full_local_vertical_mvp_through_real_cli(self) -> None:
        initialized = _object(self.checked_json("init", "--vault", str(self.vault)))
        self.assertEqual(initialized["status"], "initialized")
        service_plan = _object(self.checked_json("service", "plan"))
        self.assertEqual(service_plan["status"], "plan_only")
        service_preview = _object(self.checked_json("service", "install", "--preview"))
        self.assertEqual(service_preview["status"], "install_preview")
        open_preview = _object(self.checked_json("open", "--today", "--preview"))
        self.assertFalse(open_preview["editor_launched"])
        migration_target = self.root / "migration-target"
        migration = _object(self.checked_json(
            "storage", "migrate", str(migration_target), "--dry-run",
        ))
        self.assertFalse(migration["copy_performed"])
        self.assertFalse(migration_target.exists())
        privacy_fixture = self.root / "synthetic-privacy.txt"
        _ = privacy_fixture.write_text(
            "api_key=FAKE-SYNTHETIC-SECRET-0001\nsafe synthetic line\n", encoding="utf-8",
        )
        privacy_result = self.run_cli("privacy", "inspect", "--file", str(privacy_fixture))
        self.assertEqual(privacy_result.returncode, 0, privacy_result.stderr)
        self.assertNotIn("FAKE-SYNTHETIC-SECRET-0001", privacy_result.stdout)
        privacy_report = _object(decode_json(privacy_result.stdout))
        self.assertEqual(privacy_report["sensitive_line_count"], 1)
        arguments = (
            "capture", "--file", str(FIXTURE), "--source-event-id", "cli-event-001",
            "--session-id", "cli-session", "--title", "한글 체크포인트 복구",
            "--project", "chronicle-cli-test", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:30:00+09:00", "--event-type", "error",
        )
        committed = _object(self.checked_json(*arguments))
        self.assertEqual(committed["status"], "committed")
        event_id = _text(committed["event_id"])
        replayed = _object(self.checked_json(*arguments))
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["event_id"], event_id)

        alias = _object(self.checked_json(
            "project", "alias", "add", "--project-id", "chronicle-cli-test", "--alias", "Chronicle Demo",
        ))
        self.assertEqual(alias["status"], "alias_added")
        alias_rows = _array(self.checked_json("project", "alias", "list"))
        self.assertEqual(_text(_object(alias_rows[0])["project_id"]), "chronicle-cli-test")
        search = _array(self.checked_json(
            "search", "--query", "체크포인트", "--date", "2026-11-06", "--author", "Dex",
            "--session", "cli-session", "--event-type", "error", "--project", "Chronicle Demo",
        ))
        matching = [row for row in search if _text(_object(row)["event_id"]) == event_id]
        self.assertEqual(len(matching), 2)
        self.assertEqual(_text(_object(matching[0])["event_type"]), "error")
        daily = _object(self.checked_json("daily", "--date", "2026-11-06"))
        self.assertEqual(daily["event_count"], 1)
        home = _object(self.checked_json("home"))
        self.assertEqual(home["event_count"], 1)
        sessions_report = _object(self.checked_json("sessions", "--session", "cli-session"))
        sessions = _array(sessions_report["items"])
        self.assertEqual(_text(_object(sessions[0])["project_id"]), "chronicle-cli-test")
        documents_report = _object(self.checked_json("documents", "--project", "chronicle-cli-test"))
        self.assertEqual(len(_array(documents_report["items"])), 1)

        quote = "Markdown 체크포인트를 원장 커밋 경계로 사용하고, SQLite를 제거한 뒤에도 동일 event ID와 본문을 재구축한다."
        candidate = _object(self.checked_json(
            "knowledge", "propose", "--event-id", event_id,
            "--title", "원장 재구축 규칙", "--claim", "체크포인트 후 SQLite를 재구축한다.",
            "--quote", quote, "--created-by", "Dex", "--type", "procedure",
        ))
        candidate_id = _text(candidate["candidate_id"])
        backlog = _object(self.checked_json("review", "--due", "--limit", "1"))
        backlog_items = _array(backlog["items"])
        self.assertEqual(_text(_object(backlog_items[0])["status"]), "pending_review")
        preview = _object(self.checked_json("knowledge", "preview", candidate_id))
        approved = _object(self.checked_json(
            "knowledge", "approve", candidate_id,
            "--confirm-digest", _text(preview["approval_digest"]), "--reviewer", "Declan",
        ))
        self.assertEqual(approved["status"], "approved")
        found = _array(self.checked_json(
            "knowledge", "search", "--query", "체크포인트", "--project", "chronicle-cli-test",
        ))
        self.assertEqual(len(found), 1)
        context_preview = _object(self.checked_json(
            "context", "--query", "체크포인트", "--project", "chronicle-cli-test",
            "--preview", "--max-chars", "1024", "--save",
        ))
        saved_bundle_id = _text(context_preview["saved_bundle_id"])
        saved_check = _object(self.checked_json("context", "--saved-bundle", saved_bundle_id))
        self.assertEqual(_text(saved_check["status"]), "current")
        other_candidate = _object(self.checked_json(
            "knowledge", "propose", "--event-id", event_id,
            "--title", "검토 전 대안", "--claim", "체크포인트 검토 전에 복구한다.",
            "--quote", quote, "--created-by", "Dex", "--type", "question",
        ))
        relation = _object(self.checked_json(
            "knowledge", "relation", "create", "--first-candidate-id", candidate_id,
            "--second-candidate-id", _text(other_candidate["candidate_id"]),
            "--type", "contradicts", "--reason", "Synthetic contradiction evidence",
        ))
        relation_id = _text(relation["relation_id"])
        self.assertEqual(_text(relation["status"]), "relation_pending_review")
        self.assertEqual(_array(self.checked_json(
            "knowledge", "search", "--query", "체크포인트", "--project", "chronicle-cli-test",
        )), [])
        self.assertEqual(_text(_object(self.checked_json(
            "context", "--saved-bundle", saved_bundle_id,
        ))["status"]), "stale")
        _ = self.checked_json(
            "knowledge", "relation", "review", relation_id, "--decision", "dismiss",
            "--reviewer", "Declan", "--reason", "Synthetic relation reviewed",
        )
        self.assertEqual(_text(_object(self.checked_json(
            "context", "--saved-bundle", saved_bundle_id,
        ))["status"]), "current")
        ask = _object(self.checked_json(
            "ask", "--query", "체크포인트", "--project", "chronicle-cli-test",
        ))
        self.assertIsNone(ask["generated_answer"])
        self.assertEqual(ask["mode"], "unsupported_answer_mode")

        _ = self.checked_json(
            "knowledge", "retract", candidate_id, "--reviewer", "Declan", "--reason", "synthetic regression",
        )
        self.assertEqual(_array(self.checked_json(
            "knowledge", "search", "--query", "체크포인트", "--project", "chronicle-cli-test",
        )), [])
        database = self.root / "state/projection.sqlite3"
        database.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        rebuilt = _object(self.checked_json("rebuild"))
        self.assertEqual(rebuilt["committed_events"], 1)
        self.assertEqual(rebuilt["uncheckpointed_event_ids"], [])
        relation_rows = _array(self.checked_json("knowledge", "relation", "list"))
        self.assertEqual(_text(_object(relation_rows[0])["status"]), "dismissed")
        rebuilt_alias_search = _array(self.checked_json(
            "search", "--query", "체크포인트", "--project", "Chronicle Demo",
        ))
        self.assertEqual(len(rebuilt_alias_search), 2)
        removed_alias = _object(self.checked_json(
            "project", "alias", "remove", "--project-id", "chronicle-cli-test", "--alias", "Chronicle Demo",
        ))
        self.assertEqual(removed_alias["status"], "alias_removed")
        self.assertEqual(_array(self.checked_json("project", "alias", "list")), [])

    def test_classification_is_append_only_and_survives_projection_rebuild(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        captured = _object(self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "classify-event-001",
            "--session-id", "classify-session", "--title", "분류 회귀",
            "--project", "original-project", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:30:00+09:00", "--event-type", "user_message",
        ))
        event_id = _text(captured["event_id"])
        event_paths = [
            path for path in (self.vault / "80_Events").rglob("*.md")
            if read_capture(path)[0].get("event_id") == event_id
        ]
        self.assertEqual(len(event_paths), 1)
        source_bytes = event_paths[0].read_bytes()
        correction = _object(self.checked_json(
            "classify", event_id, "--project", "corrected-project",
            "--event-type", "test_result", "--reviewer", "Dex",
            "--reason", "synthetic classification correction",
        ))
        self.assertEqual(correction["status"], "corrected")
        self.assertEqual(event_paths[0].read_bytes(), source_bytes)
        search = _array(self.checked_json(
            "search", "--query", "체크포인트", "--project", "corrected-project",
            "--event-type", "test_result",
        ))
        self.assertEqual(len(search), 2)
        self.assertEqual(_array(self.checked_json(
            "search", "--query", "체크포인트", "--project", "original-project",
        )), [])
        unchanged = _object(self.checked_json(
            "classify", event_id, "--project", "corrected-project",
            "--event-type", "test_result", "--reviewer", "Dex",
            "--reason", "repeat synthetic correction",
        ))
        self.assertEqual(unchanged["status"], "unchanged")
        _ = self.checked_json("rebuild")
        rebuilt_search = _array(self.checked_json(
            "search", "--query", "체크포인트", "--project", "corrected-project",
            "--event-type", "test_result",
        ))
        self.assertEqual(len(rebuilt_search), 2)
