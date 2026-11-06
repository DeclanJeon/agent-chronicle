"""Real-filesystem regression cases for the pinned daily cursor store."""

from datetime import datetime
import errno
import os
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from chronicle.daily_cursor_store import open_cursor_store
from chronicle.daily_schedule import catch_up_daily_notes, inspect_daily_schedule
from chronicle.errors import ChronicleError
from tests.support import ChronicleTestCase


class PinnedCursorStoreTests(ChronicleTestCase):
    def make_cursor(self) -> Path:
        """Prepare an existing private cursor whose day is behind the fixed cutoff."""
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(mode=0o700)
        _ = cursor.write_bytes(b'{"schema_version":1,"last_attempted_date":"2026-11-05"}')
        cursor.chmod(0o600)
        return cursor

    def rebind_parent(self, cursor: Path) -> tuple[Path, Path, bytes]:
        """Replace a fixture parent name after its original FD has been acquired."""
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        external = outside / cursor.name
        original = b'{"schema_version":1,"last_attempted_date":"2026-11-01"}'
        _ = external.write_bytes(original)
        external.chmod(0o600)
        retired = cursor.parent.with_name("retired-schedule")
        _ = cursor.parent.rename(retired)
        cursor.parent.symlink_to(outside, target_is_directory=True)
        return retired, external, original

    def test_open_store_rejects_rebound_parent_before_reading(self) -> None:
        # Given a store with a pinned, verified directory.
        cursor = self.make_cursor()
        before = cursor.read_bytes()
        with open_cursor_store(self.vault) as store:
            if store is None:
                self.fail("store must exist for this fixture")
            # When its path name is rebound after acquisition.
            retired, external, original = self.rebind_parent(cursor)
            with self.assertRaises(ChronicleError):
                _ = store.read_text()
        # Then no object was consumed or modified through the rebound path.
        self.assertEqual((retired / cursor.name).read_bytes(), before)
        self.assertEqual(external.read_bytes(), original)

    def test_open_store_rejects_rebound_parent_before_writing(self) -> None:
        # Given a store with a pinned, verified directory.
        cursor = self.make_cursor()
        before = cursor.read_bytes()
        with open_cursor_store(self.vault) as store:
            if store is None:
                self.fail("store must exist for this fixture")
            # When a replacement name would otherwise redirect publication.
            retired, external, original = self.rebind_parent(cursor)
            with self.assertRaises(ChronicleError):
                store.replace(b'{"schema_version":1,"last_attempted_date":"2026-11-06"}')
        # Then neither the old nor the outside cursor is changed.
        self.assertEqual((retired / cursor.name).read_bytes(), before)
        self.assertEqual(external.read_bytes(), original)

    def test_failed_file_fsync_keeps_previous_cursor_and_removes_temporary(self) -> None:
        # Given a durable old cursor and a filesystem failure before replace.
        cursor = self.make_cursor()
        before = cursor.read_bytes()
        with open_cursor_store(self.vault) as store:
            if store is None:
                self.fail("fixture store is absent")
            # When the cursor's temporary file cannot be fsynced.
            with patch("chronicle.daily_cursor_store.os.fsync", side_effect=OSError(errno.ENOSPC, "synthetic full disk")):
                with self.assertRaises(ChronicleError) as raised:
                    store.replace(b'{"schema_version":1,"last_attempted_date":"2026-11-06"}')
        # Then there is no cursor advance or abandoned temp file.
        self.assertEqual(raised.exception.code, "durability_error")
        self.assertEqual(cursor.read_bytes(), before)
        self.assertEqual(list(cursor.parent.glob(".daily-catchup.*.tmp")), [])

    def test_read_limit_holds_when_file_grows_after_fstat(self) -> None:
        # Given an initially bounded file and a narrow concurrent-growth seam.
        cursor = self.make_cursor()
        target_inode = cursor.stat().st_ino
        real_fstat = os.fstat

        def grow_after_inspection(descriptor: int) -> os.stat_result:
            info = real_fstat(descriptor)
            if info.st_ino == target_inode:
                _ = cursor.write_bytes(b"x" * 5000)
            return info

        # When the file outgrows the previously observed stat result.
        with open_cursor_store(self.vault) as store:
            if store is None:
                self.fail("fixture store is absent")
            with patch("chronicle.daily_cursor_store.os.fstat", grow_after_inspection):
                with self.assertRaises(ChronicleError):
                    _ = store.read_text()
        # Then the input was bounded even across the stat/read race.
        self.assertEqual(cursor.stat().st_size, 5000)

    def test_broken_parent_link_is_not_treated_as_missing_initial_state(self) -> None:
        # Given a dangling schedule parent rather than a genuine absent directory.
        schedules = self.vault / "90_System/Schedules"
        schedules.symlink_to(self.root / "does-not-exist", target_is_directory=True)
        # When the read-only inspection runs.
        with self.assertRaises(ChronicleError) as raised:
            _ = inspect_daily_schedule(self.context)
        # Then it fails closed instead of returning an uninitialized cursor.
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_invalid_utf8_cursor_is_rejected(self) -> None:
        # Given a private file that cannot be decoded as JSON text.
        cursor = self.make_cursor()
        _ = cursor.write_bytes(b"\xff\xfe\n")
        # When a store reader consumes the bounded leaf.
        with open_cursor_store(self.vault) as store:
            if store is None:
                self.fail("fixture store is absent")
            with self.assertRaises(ChronicleError) as raised:
                _ = store.read_text()
        # Then decoding failure is explicit.
        self.assertEqual(raised.exception.code, "daily_schedule_error")

    def test_failed_daily_publication_does_not_advance_cursor(self) -> None:
        # Given a previous cursor and a failure at the real latest-note publish seam.
        cursor = self.make_cursor()
        before = cursor.read_bytes()
        # When the new daily latest view cannot be durably published.
        with patch("chronicle.daily.atomic_write_new", side_effect=ChronicleError("durability_error", "synthetic publication failure")):
            with self.assertRaises(ChronicleError):
                _ = catch_up_daily_notes(
                    self.context, datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Seoul")),
                )
        # Then the cursor still describes only the previously committed work.
        self.assertEqual(cursor.read_bytes(), before)
        self.assertFalse((self.vault / "50_Daily/2026/11/2026-11-06.md").exists())
