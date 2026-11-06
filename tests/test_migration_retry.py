"""Regression tests for retrying the v2-to-month daily archive transition."""

from __future__ import annotations

from datetime import date
import unittest
from typing import TYPE_CHECKING, override

from chronicle.daily import update_daily
from chronicle.daily_revision_archive import parse_latest_revision
from chronicle.errors import ChronicleError

if TYPE_CHECKING:
    from tests.test_daily_revision_links import DailyRevisionLinkTests


class DailyMigrationRetryTests(unittest.TestCase):
    """Exercise v2 daily archives after an injected latest-pointer failure."""

    case: DailyRevisionLinkTests | None = None

    def _fixture(self) -> DailyRevisionLinkTests:
        if self.case is None:
            raise AssertionError("migration test fixture was not initialized")
        return self.case

    @override
    def setUp(self) -> None:
        super().setUp()
        from tests.test_daily_revision_links import DailyRevisionLinkTests

        case = DailyRevisionLinkTests(
            "test_v2_archive_migration_preserves_every_existing_file_and_links"
        )
        case.setUp()
        self.case = case
        self.addCleanup(case.doCleanups)

    def test_v2_transition_retry_preserves_parent_and_publishes_one_new_revision(
        self,
    ) -> None:
        case = self._fixture()
        day, v2_revision, v2_latest, daily_path, month_archive = (
            case.simulate_v2_pointer_failure()
        )
        old_archive = v2_revision.record_path.parent
        old_archive_before = {
            path.name: path.read_bytes() for path in old_archive.iterdir()
        }
        alias_record = month_archive / f"{v2_revision.revision_id}.md"
        alias_snapshot = month_archive / f"{v2_revision.revision_id}.snapshot.md"
        self.assertEqual(
            alias_record.read_bytes(), v2_revision.record_path.read_bytes()
        )
        self.assertEqual(alias_snapshot.read_bytes(), v2_latest)
        self.assertEqual(len(tuple(month_archive.glob("*.md"))), 4)

        retried = update_daily(case.vault, day, timezone_name=case.config.timezone)

        self.assertEqual(retried.event_count, 2)
        self.assertEqual(retried.previous_revision_id, v2_revision.revision_id)
        current_revision, _body = parse_latest_revision(
            daily_path.read_text(encoding="utf-8"), daily_path, day
        )
        if current_revision is None:
            self.fail("retry must publish a versioned latest marker")
        self.assertNotEqual(current_revision.revision_id, v2_revision.revision_id)
        self.assertEqual(current_revision.previous_revision_id, v2_revision.revision_id)
        self.assertEqual(current_revision.record_path.parent, month_archive)
        self.assertEqual(current_revision.snapshot_path, retried.snapshot_path)
        self.assertEqual(len(tuple(month_archive.glob("*.md"))), 4)
        self.assertEqual(
            {path.name: path.read_bytes() for path in old_archive.iterdir()},
            old_archive_before,
        )
        self.assertEqual(v2_revision.snapshot_path.read_bytes(), v2_latest)

    def test_shared_month_directory_allows_legacy_knowledge_section_migration(self) -> None:
        case = self._fixture()
        day_one = date(2026, 12, 1)
        day_two = date(2026, 12, 2)
        first_event = case.capture(
            "shared-month-day-one", "Legacy day one", "2026-12-01T10:00:00+09:00"
        )
        _first_revision, _first_latest = case.seed_v2_daily(first_event, day_one)
        second_event = case.capture(
            "shared-month-day-two", "Legacy day two", "2026-12-02T10:00:00+09:00"
        )
        second_revision, second_latest = case.seed_v2_daily(second_event, day_two)
        _ = case.capture(
            "shared-month-day-one-next", "New day one", "2026-12-01T11:00:00+09:00"
        )
        _ = update_daily(case.vault, day_one, timezone_name=case.config.timezone)

        month_archive = case.vault / "50_Daily/2026/12_Revisions"
        day_two_path = case.vault / "50_Daily/2026/12/2026-12-02.md"
        old_archive = second_revision.record_path.parent
        old_archive_before = {
            path.name: path.read_bytes() for path in old_archive.iterdir()
        }
        self.assertTrue(month_archive.is_dir())
        self.assertFalse((month_archive / f"{second_revision.revision_id}.md").exists())

        result = update_daily(case.vault, day_two, timezone_name=case.config.timezone)

        self.assertEqual(result.event_count, 1)
        self.assertNotEqual(result.revision_id, second_revision.revision_id)
        self.assertEqual(result.previous_revision_id, second_revision.revision_id)
        current_revision, current_body = parse_latest_revision(
            day_two_path.read_text(encoding="utf-8"), day_two_path, day_two,
        )
        if current_revision is None:
            self.fail("basic-profile migration must publish a versioned daily note")
        self.assertEqual(current_revision.revision_id, result.revision_id)
        self.assertEqual(current_revision.previous_revision_id, second_revision.revision_id)
        self.assertNotIn("Knowledge and review", current_body)
        self.assertEqual(
            {path.name: path.read_bytes() for path in old_archive.iterdir()},
            old_archive_before,
        )
        self.assertEqual(second_revision.snapshot_path.read_bytes(), second_latest)
        self.assertTrue((month_archive / f"{second_revision.revision_id}.md").is_file())

    def test_shared_month_directory_allows_unmigrated_day_new_event_transition(
        self,
    ) -> None:
        case = self._fixture()
        day_one = date(2026, 12, 1)
        day_two = date(2026, 12, 2)
        first_event = case.capture(
            "shared-month-transition-day-one",
            "Legacy day one",
            "2026-12-01T10:00:00+09:00",
        )
        _first_revision, _first_latest = case.seed_v2_daily(first_event, day_one)
        second_event = case.capture(
            "shared-month-transition-day-two",
            "Legacy day two",
            "2026-12-02T10:00:00+09:00",
        )
        second_revision, second_latest = case.seed_v2_daily(second_event, day_two)
        _ = case.capture(
            "shared-month-transition-day-one-next",
            "New day one",
            "2026-12-01T11:00:00+09:00",
        )
        _ = update_daily(case.vault, day_one, timezone_name=case.config.timezone)
        _ = case.capture(
            "shared-month-transition-day-two-next",
            "New day two",
            "2026-12-02T11:00:00+09:00",
        )

        result = update_daily(case.vault, day_two, timezone_name=case.config.timezone)

        month_archive = case.vault / "50_Daily/2026/12_Revisions"
        day_two_path = case.vault / "50_Daily/2026/12/2026-12-02.md"
        current_revision, _body = parse_latest_revision(
            day_two_path.read_text(encoding="utf-8"), day_two_path, day_two
        )
        self.assertEqual(result.event_count, 2)
        self.assertEqual(result.previous_revision_id, second_revision.revision_id)
        if current_revision is None:
            self.fail("converted day must point to a month-layout revision")
        self.assertEqual(
            current_revision.previous_revision_id, second_revision.revision_id
        )
        self.assertEqual(current_revision.record_path.parent, month_archive)
        alias_record = month_archive / f"{second_revision.revision_id}.md"
        alias_snapshot = month_archive / f"{second_revision.revision_id}.snapshot.md"
        self.assertEqual(
            alias_record.read_bytes(), second_revision.record_path.read_bytes()
        )
        self.assertEqual(alias_snapshot.read_bytes(), second_latest)

    def test_v2_transition_retry_rejects_missing_or_modified_month_alias(self) -> None:
        case = self._fixture()
        day, v2_revision, v2_latest, daily_path, month_archive = (
            case.simulate_v2_pointer_failure()
        )
        alias_snapshot = month_archive / f"{v2_revision.revision_id}.snapshot.md"
        original_alias = alias_snapshot.read_bytes()

        _ = alias_snapshot.write_bytes(b"tampered snapshot")
        with self.assertRaisesRegex(ChronicleError, "daily_conflict"):
            _ = update_daily(case.vault, day, timezone_name=case.config.timezone)
        self.assertEqual(daily_path.read_bytes(), v2_latest)

        _ = alias_snapshot.write_bytes(original_alias)
        _ = alias_snapshot.unlink()
        with self.assertRaisesRegex(ChronicleError, "daily_conflict"):
            _ = update_daily(case.vault, day, timezone_name=case.config.timezone)
        self.assertEqual(daily_path.read_bytes(), v2_latest)

    def test_shared_month_archive_symlink_is_rejected_for_unmigrated_day(self) -> None:
        case = self._fixture()
        day = date(2026, 12, 2)
        event = case.capture(
            "unsafe-month-archive", "Legacy day", "2026-12-02T10:00:00+09:00"
        )
        _revision, latest = case.seed_v2_daily(event, day)
        daily_path = case.vault / "50_Daily/2026/12/2026-12-02.md"
        month_archive = case.vault / "50_Daily/2026/12_Revisions"
        outside_archive = case.root / "outside-month-archive"
        outside_archive.mkdir(mode=0o700)
        _ = month_archive.symlink_to(outside_archive, target_is_directory=True)

        with self.assertRaisesRegex(ChronicleError, "daily_conflict"):
            _ = update_daily(case.vault, day, timezone_name=case.config.timezone)

        self.assertEqual(daily_path.read_bytes(), latest)
        self.assertEqual(tuple(outside_archive.iterdir()), ())
