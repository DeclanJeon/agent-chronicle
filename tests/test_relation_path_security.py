"""Synthetic CLI regressions for relation review path containment."""

import shutil

from tests.test_cli import CliTestCase, decode_json, json_object, json_text


class RelationPathSecurityCliTests(CliTestCase):
    def test_relation_review_rejects_traversal_without_writing_outside_vault(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source = self.root / "relation-source.md"
        _ = source.write_text("synthetic relation evidence\n", encoding="utf-8")
        capture = json_object(self.checked_json(
            "capture", "--file", str(source), "--source-event-id", "relation-source",
            "--session-id", "relation-session", "--title", "Relation source",
            "--project", "safe-project", "--author", "Safe",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        ))
        event_id = json_text(capture["event_id"])
        candidate_ids: list[str] = []
        for suffix in ("one", "two"):
            candidate = json_object(self.checked_json(
                "knowledge", "propose", "--event-id", event_id,
                "--title", f"Safe {suffix}", "--claim", f"Safe claim {suffix}",
                "--quote", "synthetic relation evidence", "--created-by", "Safe",
            ))
            candidate_ids.append(json_text(candidate["candidate_id"]))
        relation = json_object(self.checked_json(
            "knowledge", "relation", "create",
            "--first-candidate-id", candidate_ids[0],
            "--second-candidate-id", candidate_ids[1], "--type", "contradicts",
            "--reason", "Synthetic relation evidence",
        ))
        relation_id = json_text(relation["relation_id"])
        relation_file = self.vault / "90_System/KnowledgeRelations" / f"{relation_id}.md"
        outside = self.root / "outside"
        outside.mkdir()
        _ = shutil.copyfile(relation_file, outside / f"{relation_id}.md")
        traversal_id = f"../../../outside/{relation_id}"

        result = self.run_cli(
            "knowledge", "relation", "review", traversal_id,
            "--decision", "dismiss", "--reviewer", "Safe", "--reason", "synthetic test",
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        error = json_object(decode_json(result.stderr))
        self.assertIn("invalid_relation", json_text(error["error"]))
        self.assertFalse((outside / relation_id).exists())
        self.assertEqual(list((self.vault / "90_System/KnowledgeRelationDecisions").glob("**/*.md")), [])

