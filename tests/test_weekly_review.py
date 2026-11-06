"""Tests for read-only weekly backlogs and user-edit preservation."""

from datetime import date

from chronicle.weekly_review import publish_weekly_review
from tests.test_cli import CliTestCase, json_object, json_text


class WeeklyReviewTests(CliTestCase):
    def _candidate(self) -> str:
        _ = self.checked_json("init", "--vault", str(self.vault))
        source = self.root / "weekly-source.md"
        quote = "An explicit approval remains reviewable."
        _ = source.write_text(quote + "\n", encoding="utf-8")
        capture = json_object(self.checked_json(
            "capture", "--file", str(source), "--source-event-id", "weekly-review-event",
            "--session-id", "weekly-review-session", "--title", "Weekly source",
            "--project", "weekly-project", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:00:00+09:00",
        ))
        proposed = json_object(self.checked_json(
            "knowledge", "propose", "--event-id", json_text(capture["event_id"]),
            "--title", "Explicitly supported routine", "--claim", quote, "--quote", quote,
            "--created-by", "Dex", "--type", "lesson",
        ))
        return json_text(proposed["candidate_id"])

    def test_weekly_cli_creates_a_local_snapshot_without_reviewing_items(self) -> None:
        candidate_id = self._candidate()
        result = json_object(self.checked_json("review", "--weekly"))
        self.assertFalse(result["reviewed"])
        self.assertFalse(result["external_delivery"])
        self.assertEqual(result["item_count"], 1)
        paths = list((self.vault / "60_Reviews/Weekly").glob("*.md"))
        self.assertEqual(len(paths), 1)
        contents = paths[0].read_text(encoding="utf-8")
        self.assertIn(candidate_id, contents)
        self.assertIn("pending-item list", contents)
        self.assertIn("## User notes", contents)
        self.assertFalse("approved" in contents.lower())

    def test_weekly_refresh_preserves_user_notes_and_conflicts_on_edited_generated_region(self) -> None:
        candidate_id = self._candidate()
        selected_week = date(2040, 1, 2)
        older_folder = self.vault / "60_Reviews/Weekly"
        older_folder.mkdir(parents=True, exist_ok=True)
        _ = older_folder.chmod(0o700)
        older_path = older_folder / "2039-W52.md"
        _ = older_path.write_text("# Prior week placeholder\n", encoding="utf-8")
        _ = older_path.chmod(0o600)
        first = publish_weekly_review(self.vault, selected_week)
        self.assertEqual(first.status, "created")
        self.assertFalse(first.caught_up)
        original = first.path.read_text(encoding="utf-8")
        self.assertNotIn("Daemon catch-up restored the current backlog", original)
        self.assertIn("This is a pending-item list", original)
        _ = first.path.write_text(original + "Owner note: revisit after source change.\n", encoding="utf-8")

        refreshed = publish_weekly_review(self.vault, selected_week)
        self.assertEqual(refreshed.status, "unchanged")
        self.assertIn("Owner note: revisit after source change.", first.path.read_text(encoding="utf-8"))

        edited = first.path.read_text(encoding="utf-8").replace(
            "pending-item list", "user-edited generated text", 1,
        )
        _ = first.path.write_text(edited, encoding="utf-8")
        conflict = publish_weekly_review(self.vault, selected_week)
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNotNone(conflict.conflict_copy)
        self.assertEqual(first.path.read_text(encoding="utf-8"), edited)
        assert conflict.conflict_copy is not None
        self.assertIn(candidate_id, conflict.conflict_copy.read_text(encoding="utf-8"))
