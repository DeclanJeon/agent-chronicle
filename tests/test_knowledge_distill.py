"""CLI tests for rule-only, preview-only knowledge distillation."""

from tests.test_cli import CliTestCase, json_array, json_object, json_text


class KnowledgeDistillCliTests(CliTestCase):
    def test_distill_extracts_marked_zones_groups_duplicates_and_never_writes(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        content = (
            "Unmarked introduction is ignored.\n\n"
            "## 배운 점\n- Checkpointed Markdown remains the recovery source.\n\n"
            "## 결정\nKeep this workflow local and explicit.\n\n"
            "#knowledge/question\nWhat happens when a source changes?\n\n"
            "## Unmarked notes\nThis text is not a candidate.\n\n"
            "```markdown\n## 아이디어\nA heading inside a code block is inert.\n```\n"
        )
        source = self.root / "marked-session.md"
        _ = source.write_text(content, encoding="utf-8")
        captured: list[str] = []
        for source_event_id in ("distill-session-part-1", "distill-session-part-2"):
            result = json_object(self.checked_json(
                "capture", "--file", str(source), "--source-event-id", source_event_id,
                "--session-id", "distill-synthetic-session", "--title", "Marked session",
                "--project", "distill-project", "--author", "Dex",
                "--occurred-at", "2026-11-06T12:00:00+09:00",
            ))
            captured.append(json_text(result["event_id"]))

        preview = json_object(self.checked_json(
            "knowledge", "distill", "--session", "distill-synthetic-session", "--preview",
        ))
        items = [json_object(item) for item in json_array(preview["items"])]
        self.assertEqual(preview["status"], "preview_only")
        self.assertEqual(preview["committed_events_scanned"], 2)
        self.assertEqual(preview["writes"], 0)
        self.assertEqual(preview["model_calls"], 0)
        self.assertFalse(preview["external_transmission"])
        self.assertEqual([item["knowledge_type"] for item in items], ["lesson", "decision", "question"])
        lesson = items[0]
        self.assertEqual(lesson["exact_quote"], "- Checkpointed Markdown remains the recovery source.")
        self.assertEqual(lesson["duplicate_evidence_count"], 2)
        sources = [json_object(item) for item in json_array(lesson["sources"])]
        self.assertEqual({json_text(item["event_id"]) for item in sources}, set(captured))
        self.assertEqual(
            json_text(items[2]["exact_quote"]),
            "What happens when a source changes?",
        )
        self.assertTrue(all(item["persisted"] is False for item in items))
        self.assertEqual(list((self.vault / "90_System/KnowledgeCandidates").glob("*.md")), [])

    def test_distill_requires_preview_and_ignores_an_unmarked_session(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source = self.root / "unmarked.md"
        _ = source.write_text("Plain text without a recognized zone.\n", encoding="utf-8")
        _ = self.checked_json(
            "capture", "--file", str(source), "--source-event-id", "distill-empty-session",
            "--session-id", "distill-empty", "--title", "Unmarked", "--project", "demo",
            "--author", "Dex", "--occurred-at", "2026-11-06T12:00:00+09:00",
        )
        missing_preview = self.run_cli("knowledge", "distill", "--session", "distill-empty")
        self.assertNotEqual(missing_preview.returncode, 0)
        self.assertIn("--preview", missing_preview.stderr)
        preview = json_object(self.checked_json(
            "knowledge", "distill", "--session", "distill-empty", "--preview",
        ))
        self.assertEqual(preview["status"], "no_marked_zones")
        self.assertEqual(json_array(preview["items"]), [])
