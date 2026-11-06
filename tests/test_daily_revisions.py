"""Crash safety, legacy migration, and tamper checks for daily revisions."""

from datetime import date
import hashlib
from unittest.mock import patch

from chronicle.capture_input import load_capture_input
from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.markdown import parse_frontmatter
from chronicle.models import CaptureOptions
from tests.support import ChronicleTestCase


class DailyRevisionTests(ChronicleTestCase):
    def commit_event(self, event_id: str, title: str, occurred_at: str) -> None:
        input_path = self.write_input(f"{event_id}.md", f"Synthetic event {event_id}\n")
        options = CaptureOptions(
            input_path,
            event_id,
            "daily-revision-session",
            title,
            "daily-revision-test",
            "Dex",
            occurred_at,
            None,
            False,
        )
        parsed = load_capture_input(options, self.context)
        _ = capture_event(self.context, parsed)

    def test_replace_failure_keeps_view_and_checkpoint(self) -> None:
        self.commit_event(
            "daily-crash-first", "First event", "2026-11-06T12:00:00+09:00"
        )
        first = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )
        if first.snapshot_path is None or first.revision_id is None:
            self.fail("initial daily update must publish an immutable revision")
        previous_view = first.path.read_bytes()
        self.commit_event(
            "daily-crash-second", "Second event", "2026-11-06T13:00:00+09:00"
        )

        with patch(
            "chronicle.daily.atomic_replace",
            side_effect=ChronicleError(
                "durability_error", "injected daily pointer failure"
            ),
        ):
            with self.assertRaisesRegex(ChronicleError, "durability_error"):
                _ = update_daily(
                    self.vault,
                    date(2026, 11, 6),
                    timezone_name=self.config.timezone,
                )

        self.assertEqual(first.path.read_bytes(), previous_view)
        self.assertEqual(len(verify_checkpoint_chain(self.vault).references), 2)
        revisions = first.path.parent.parent / "11_Revisions"
        files_before_retry = tuple(sorted(path.name for path in revisions.iterdir()))
        self.assertEqual(len(files_before_retry), 4)

        retry = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )

        self.assertEqual(retry.event_count, 2)
        self.assertEqual(retry.previous_revision_id, first.revision_id)
        self.assertEqual(
            tuple(sorted(path.name for path in revisions.iterdir())), files_before_retry
        )
        self.assertEqual(first.snapshot_path.read_bytes(), previous_view)

    def test_legacy_daily_is_preserved_byte_for_byte(self) -> None:
        day = date(2026, 11, 6)
        daily_path = self.vault / "50_Daily/2026/11/2026-11-06.md"
        daily_path.parent.mkdir(mode=0o700, parents=True)
        previous_bytes = b"# 2026-11-06\n\nUser-authored legacy note.\n"
        _ = daily_path.write_bytes(previous_bytes)
        self.commit_event(
            "daily-legacy-event", "Legacy migration", "2026-11-06T12:00:00+09:00"
        )

        updated = update_daily(self.vault, day, timezone_name=self.config.timezone)

        if updated.previous_revision_id is None:
            self.fail("the first versioned view must link to its preserved legacy note")
        revisions = daily_path.parent.parent / "11_Revisions"
        snapshots = tuple(revisions.glob("*.snapshot.md"))
        legacy_snapshot = next(
            (path for path in snapshots if path.read_bytes() == previous_bytes), None
        )
        self.assertIsNotNone(legacy_snapshot)
        if legacy_snapshot is None:
            self.fail("legacy snapshot was not retained")
        record_path = revisions / f"{updated.previous_revision_id}.md"
        metadata, _body, _normalized = parse_frontmatter(record_path.read_bytes())
        self.assertEqual(metadata["snapshot_mode"], "legacy")
        self.assertEqual(
            metadata["snapshot_sha256"], hashlib.sha256(previous_bytes).hexdigest()
        )

    def test_tampered_archive_blocks_daily_replacement(self) -> None:
        self.commit_event(
            "daily-tamper-first", "First event", "2026-11-06T12:00:00+09:00"
        )
        first = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )
        if first.snapshot_path is None:
            self.fail("initial daily update must publish a snapshot")
        previous_view = first.path.read_bytes()
        _ = first.snapshot_path.write_bytes(
            first.snapshot_path.read_bytes() + b"tampered\n"
        )
        self.commit_event(
            "daily-tamper-second", "Second event", "2026-11-06T13:00:00+09:00"
        )

        with self.assertRaisesRegex(ChronicleError, "daily_conflict"):
            _ = update_daily(
                self.vault,
                date(2026, 11, 6),
                timezone_name=self.config.timezone,
            )

        self.assertEqual(first.path.read_bytes(), previous_view)


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
