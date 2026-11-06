"""Synthetic tests for timezone-aware daily-note catch-up."""

from datetime import datetime
import json
from zoneinfo import ZoneInfo

from chronicle.daily import update_daily
from chronicle.daily_schedule import catch_up_daily_notes
from chronicle.errors import ChronicleError
from tests.support import ChronicleTestCase


class DailyScheduleTests(ChronicleTestCase):
    def test_catches_up_elapsed_dates_and_is_idempotent(self) -> None:
        zone = ZoneInfo(self.config.timezone)
        _ = update_daily(self.vault, datetime(2026, 11, 3, tzinfo=zone).date(), timezone_name=self.config.timezone)

        result = catch_up_daily_notes(
            self.context, datetime(2026, 11, 6, 0, 6, tzinfo=zone),
        )
        self.assertEqual(result.status, "caught_up")
        self.assertEqual(result.first_date, "2026-11-04")
        self.assertEqual(result.last_date, "2026-11-05")
        self.assertEqual(result.days_attempted, 2)
        self.assertEqual(result.conflict_count, 0)
        self.assertFalse(result.as_json()["reviewed"])
        for selected in ("2026-11-04", "2026-11-05"):
            path = self.vault / "50_Daily" / selected[:4] / selected[5:7] / f"{selected}.md"
            self.assertTrue(path.is_file())
        repeated = catch_up_daily_notes(
            self.context, datetime(2026, 11, 6, 0, 6, tzinfo=zone),
        )
        self.assertEqual(repeated.status, "current")
        self.assertEqual(repeated.days_attempted, 0)

    def test_grace_period_defers_yesterdays_note_and_preserves_user_text(self) -> None:
        zone = ZoneInfo(self.config.timezone)
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        _ = cursor.parent.chmod(0o700)
        _ = cursor.write_text(json.dumps({
            "schema_version": 1,
            "last_attempted_date": "2026-11-03",
        }), encoding="utf-8")
        _ = cursor.chmod(0o600)
        existing = update_daily(self.vault, datetime(2026, 11, 4, tzinfo=zone).date(), timezone_name=self.config.timezone)
        _ = existing.path.write_text(
            existing.path.read_text(encoding="utf-8") + "Owner note: keep this wording.\n",
            encoding="utf-8",
        )

        before_grace = catch_up_daily_notes(
            self.context, datetime(2026, 11, 6, 0, 4, tzinfo=zone),
        )
        self.assertEqual(before_grace.last_date, "2026-11-04")
        self.assertEqual(before_grace.days_attempted, 1)
        self.assertIn("Owner note: keep this wording.", existing.path.read_text(encoding="utf-8"))
        self.assertFalse(
            (self.vault / "50_Daily/2026/11/2026-11-05.md").exists()
        )

    def test_oversized_historical_gap_fails_before_writing(self) -> None:
        zone = ZoneInfo(self.config.timezone)
        _ = update_daily(self.vault, datetime(2020, 1, 1, tzinfo=zone).date(), timezone_name=self.config.timezone)
        with self.assertRaises(ChronicleError) as raised:
            _ = catch_up_daily_notes(
                self.context, datetime(2026, 11, 6, 12, 0, tzinfo=zone),
            )
        self.assertEqual(raised.exception.code, "daily_catchup_too_large")
        self.assertFalse((self.vault / "90_System/Schedules/daily-catchup.json").exists())
