"""Regression checks for cursor path boundaries and truthful read-only diagnosis."""

from datetime import datetime
import os
from pathlib import Path
import shutil
from unittest.mock import patch
from zoneinfo import ZoneInfo

from chronicle.daily_schedule import catch_up_daily_notes
from chronicle.errors import ChronicleError
from tests.support import ChronicleTestCase
from tests.test_cli import CliTestCase, decode_json, json_object


class CursorPathSafetyTests(ChronicleTestCase):
    def test_parent_replaced_after_validation_is_rejected_before_external_read(self) -> None:
        # Given a valid outside cursor and an initially safe system directory.
        system = self.vault / "90_System"
        outside = self.root / "outside"
        _ = shutil.copytree(system, outside)
        (outside / "Schedules").mkdir(mode=0o700)
        external = outside / "Schedules/daily-catchup.json"
        original = b'{"schema_version":1,"last_attempted_date":"2026-11-05"}'
        _ = external.write_bytes(original)
        external.chmod(0o600)
        cursor = system / "Schedules/daily-catchup.json"
        external_reads: list[str] = []
        original_read = Path.read_text
        original_mkdir = os.mkdir
        swapped = False

        def swap_before_mkdir(path: str | os.PathLike[str], mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            nonlocal swapped
            if not swapped and Path(path).name == "Schedules":
                _ = system.rename(self.vault / "retired-system")
                system.symlink_to(outside, target_is_directory=True)
                swapped = True
            original_mkdir(path, mode, dir_fd=dir_fd)

        def observe_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == cursor:
                external_reads.append(str(path.resolve()))
            return original_read(path, encoding=encoding, errors=errors)

        # When the parent changes at the real validation/I/O boundary.
        with patch("os.mkdir", swap_before_mkdir):
            with patch.object(Path, "read_text", observe_read):
                with self.assertRaises(ChronicleError):
                    _ = catch_up_daily_notes(
                        self.context, datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Seoul")),
                    )

        # Then neither the outside input nor a daily note is consumed/published.
        self.assertEqual(external_reads, [])
        self.assertEqual(external.read_bytes(), original)
        self.assertFalse((self.vault / "50_Daily/2026/11/2026-11-06.md").exists())

    def test_leaf_symlink_is_rejected_without_touching_external_cursor(self) -> None:
        # Given an explicitly linked cursor leaf.
        outside = self.write_input("outside.json", '{"schema_version":1,"last_attempted_date":"2026-11-05"}')
        outside.chmod(0o600)
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(mode=0o700)
        cursor.symlink_to(outside)
        before = outside.read_bytes()
        # When the catch-up is attempted.
        with self.assertRaises(ChronicleError):
            _ = catch_up_daily_notes(
                self.context, datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Seoul")),
            )
        # Then the external object is unchanged.
        self.assertEqual(outside.read_bytes(), before)

    def test_fifo_cursor_fails_instead_of_blocking_on_read(self) -> None:
        # Given a non-regular cursor leaf.
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(mode=0o700)
        os.mkfifo(cursor, mode=0o600)
        # When the catch-up inspects the leaf.
        with self.assertRaises(ChronicleError):
            _ = catch_up_daily_notes(
                self.context, datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Seoul")),
            )
        # Then no generated daily note is published.
        self.assertFalse((self.vault / "50_Daily/2026/11/2026-11-06.md").exists())


class DoctorScheduleSafetyTests(CliTestCase):
    def _write_cursor(self, content: str) -> Path:
        """Create one private synthetic cursor after CLI initialization."""
        _ = self.checked_json("init", "--vault", str(self.vault))
        cursor = self.vault / "90_System/Schedules/daily-catchup.json"
        cursor.parent.mkdir(mode=0o700)
        _ = cursor.write_text(content, encoding="utf-8")
        cursor.chmod(0o600)
        return cursor

    def test_doctor_reports_future_cursor_attention_without_mutating_vault(self) -> None:
        # Given a cursor that is ahead of every currently eligible date.
        _ = self._write_cursor('{"schema_version":1,"last_attempted_date":"2099-01-01"}')
        before = tuple(sorted((str(p.relative_to(self.vault)), p.read_bytes()) for p in self.vault.rglob("*") if p.is_file()))
        # When the actual subprocess CLI is invoked.
        result = self.run_cli("doctor")
        report = json_object(decode_json(result.stdout))
        # Then failure is visible, with no repair or write side effects.
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(json_object(report["daily_schedule"])["error"], "daily_schedule_error")
        after = tuple(sorted((str(p.relative_to(self.vault)), p.read_bytes()) for p in self.vault.rglob("*") if p.is_file()))
        self.assertEqual(after, before)

    def test_doctor_reports_malformed_cursor_attention(self) -> None:
        # Given a complete but invalid stored JSON document.
        _ = self._write_cursor('{"schema_version":')
        # When the actual diagnostic command runs.
        result = self.run_cli("doctor")
        report = json_object(decode_json(result.stdout))
        # Then structural failure is not reported as healthy.
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["status"], "attention")

    def test_doctor_reports_linked_parent_attention(self) -> None:
        # Given a real cursor whose parent is replaced by a foreign-directory link.
        cursor = self._write_cursor('{"schema_version":1,"last_attempted_date":"2026-11-05"}')
        outside = self.root / "outside-schedules"
        _ = cursor.parent.rename(outside)
        cursor.parent.symlink_to(outside, target_is_directory=True)
        original = (outside / cursor.name).read_bytes()
        # When doctor inspects the configured cursor boundary.
        result = self.run_cli("doctor")
        report = json_object(decode_json(result.stdout))
        # Then it flags attention without following/repairing the selected input.
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(json_object(report["daily_schedule"])["error"], "unsafe_path")
        self.assertEqual((outside / cursor.name).read_bytes(), original)

    def test_doctor_does_not_create_a_missing_schedule_directory(self) -> None:
        # Given a clean initialization with no daily cursor yet.
        _ = self.checked_json("init", "--vault", str(self.vault))
        schedules = self.vault / "90_System/Schedules"
        self.assertFalse(schedules.exists())
        # When diagnosis runs.
        result = self.run_cli("doctor")
        # Then the normal initial state stays uninitialized and read-only.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(schedules.exists())
