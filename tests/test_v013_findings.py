"""Regression tests for Atlas's exact v012 integration findings."""

from datetime import date, datetime
import json
import shutil
from unittest.mock import patch
from zoneinfo import ZoneInfo

from chronicle.daily_schedule import catch_up_daily_notes
from chronicle.errors import ChronicleError
from chronicle.knowledge_daily import daily_knowledge_section
from chronicle.weekly_review import publish_weekly_review
from tests.support import ChronicleTestCase


class DailyCursorBoundaryTests(ChronicleTestCase):
    def test_symlink_parent_cursor_is_rejected_before_reading_external_future_value(self) -> None:
        outside = self.root / "outside-schedule"
        outside.mkdir(mode=0o700)
        external_cursor = outside / "daily-catchup.json"
        original = json.dumps({
            "schema_version": 1,
            "last_attempted_date": "2030-01-01",
        })
        _ = external_cursor.write_text(original, encoding="utf-8")
        _ = external_cursor.chmod(0o600)

        schedules = self.vault / "90_System/Schedules"
        if schedules.exists():
            shutil.rmtree(schedules)
        schedules.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ChronicleError) as raised:
            _ = catch_up_daily_notes(
                self.context,
                datetime(2026, 11, 6, 0, 6, tzinfo=ZoneInfo(self.config.timezone)),
            )
        self.assertIn(raised.exception.code, {"unsafe_path", "daily_schedule_error"})
        self.assertEqual(external_cursor.read_text(encoding="utf-8"), original)
        self.assertFalse((self.vault / "50_Daily/2026/11/2026-11-05.md").exists())

    def test_future_cursor_inside_vault_fails_closed_instead_of_reporting_current(self) -> None:
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        _ = cursor.parent.chmod(0o700)
        _ = cursor.write_text(json.dumps({
            "schema_version": 1,
            "last_attempted_date": "2030-01-01",
        }), encoding="utf-8")
        _ = cursor.chmod(0o600)

        with self.assertRaises(ChronicleError):
            _ = catch_up_daily_notes(
                self.context,
                datetime(2026, 11, 6, 0, 6, tzinfo=ZoneInfo(self.config.timezone)),
            )
        self.assertFalse((self.vault / "50_Daily/2026/11/2026-11-05.md").exists())


class DailyMarkdownWarningTests(ChronicleTestCase):
    def test_untrusted_review_title_is_inert_markdown_text(self) -> None:
        title = "Safe title [forged warning](https://example.invalid) <https://evil.invalid>"
        with patch("chronicle.knowledge_daily.all_due_review_items", return_value=[{
            "candidate_id": "safe-id",
            "title": title,
            "status": "review_due",
            "priority": 1,
            "review_due_at": None,
        }]):
            rendered = daily_knowledge_section(
                self.vault,
                self.vault / "50_Daily/2026/11/2026-11-06.md",
                date(2026, 11, 6),
                self.config.timezone,
            )
        self.assertIn(r"\[forged warning\](https://example.invalid)", rendered)
        self.assertNotIn("[forged warning]", rendered)
        self.assertNotIn("<https://evil.invalid>", rendered)


class WeeklyCatchupProvenanceTests(ChronicleTestCase):
    def test_prior_week_file_alone_does_not_claim_daemon_catchup(self) -> None:
        folder = self.vault / "60_Reviews/Weekly"
        folder.mkdir(parents=True, mode=0o700)
        prior = folder / "2026-W44.md"
        _ = prior.write_text("# Synthetic prior week\n", encoding="utf-8")
        _ = prior.chmod(0o600)

        result = publish_weekly_review(self.vault, date(2026, 11, 6))
        text = result.path.read_text(encoding="utf-8")
        self.assertFalse(result.caught_up)
        self.assertNotIn("Daemon catch-up restored the current backlog", text)
        self.assertIn("pending-item list", text)
