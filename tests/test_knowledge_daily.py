"""Tests that daily Markdown reflects explicit candidate and decision records."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tests.test_cli import CliTestCase, json_object, json_text


class KnowledgeDailyCliTests(CliTestCase):
    def test_daily_command_does_not_attach_advanced_knowledge_backlog(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        zone = ZoneInfo("Asia/Seoul")
        observed = datetime.now(zone)
        occurred_at = observed.isoformat()
        day = observed.date().isoformat()
        quote = "Which validation boundary should the next review cover?"
        source = self.root / "daily-knowledge-source.md"
        _ = source.write_text(quote + "\n", encoding="utf-8")
        capture = json_object(self.checked_json(
            "capture", "--file", str(source), "--source-event-id", "daily-question-event",
            "--session-id", "daily-question-session", "--title", "Daily question source",
            "--project", "daily-review-project", "--author", "Dex",
            "--occurred-at", occurred_at,
        ))
        candidate = json_object(self.checked_json(
            "knowledge", "propose", "--event-id", json_text(capture["event_id"]),
            "--title", "Next review boundary", "--claim", quote, "--quote", quote,
            "--created-by", "Dex", "--type", "question",
        ))
        preview = json_object(self.checked_json("knowledge", "preview", json_text(candidate["candidate_id"])))
        _ = self.checked_json(
            "knowledge", "approve", json_text(candidate["candidate_id"]),
            "--confirm-digest", json_text(preview["approval_digest"]), "--reviewer", "Dex",
        )

        _ = self.checked_json("daily", "--date", day)
        note = self.vault / "50_Daily" / day[:4] / day[5:7] / f"{day}.md"
        contents = note.read_text(encoding="utf-8")
        self.assertNotIn("## Knowledge and review", contents)
        self.assertNotIn("Next review boundary", contents)
        self.assertIn(json_text(capture["event_id"]), contents)
        self.assertTrue(
            (self.vault / "90_System/KnowledgeCandidates" / f"{json_text(candidate['candidate_id'])}.md").is_file()
        )
