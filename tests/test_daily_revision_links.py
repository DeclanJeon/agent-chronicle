"""Navigation checks for immutable daily revision archives."""

import hashlib
import os
from datetime import date
from pathlib import Path
import re
from unittest.mock import patch

from chronicle.capture_input import load_capture_input
from chronicle.daily import update_daily
from chronicle.daily_revisions import DailyRevision, create_daily_revision
from chronicle.event_format import read_capture
from chronicle.knowledge_daily import daily_knowledge_section
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.markdown import parse_frontmatter
from chronicle.models import CaptureOptions, JournalCommit
from chronicle.schema import text_field
from tests.support import ChronicleTestCase


_POINTER = re.compile(
    r"\A<!-- chronicle:daily-revision revision_id=([0-9a-f-]{36}) "
    + r"previous_revision_id=(none|[0-9a-f-]{36}) record=([^\s]+) "
    + r"snapshot=([^\s]+) -->\Z"
)
_MARKDOWN_LINK = re.compile(r"\]\(<([^>]+)>\)")


def _markdown_targets(path: Path) -> tuple[Path, ...]:
    """Resolve generated Markdown links from the document that contains them."""
    source = path.read_text(encoding="utf-8")
    targets: list[Path] = []
    for match in _MARKDOWN_LINK.finditer(source):
        target = match.group(1)
        targets.append(Path(os.path.normpath(path.parent / target)))
    return tuple(targets)


def _event_ids_from(path: Path) -> set[str]:
    """Read every event reached by a snapshot's Markdown evidence links."""
    event_ids: set[str] = set()
    for target in _markdown_targets(path):
        if not target.is_file():
            raise AssertionError(f"broken Markdown link in {path}: {target}")
        metadata, _content = read_capture(target)
        event_id = metadata.get("event_id")
        if not isinstance(event_id, str):
            raise AssertionError(f"snapshot link is not an event: {target}")
        event_ids.add(event_id)
    return event_ids


def _daily_body(day: date, daily_path: Path, event: JournalCommit) -> str:
    """Create a v2-shaped generated region for the compatibility fixture."""
    metadata, _content = read_capture(event.event_path)
    event_id = text_field(metadata, "event_id")
    title = text_field(metadata, "title")
    occurred_at = text_field(metadata, "occurred_at")
    relative_event_path = Path(
        os.path.relpath(event.event_path, start=daily_path.parent)
    ).as_posix()
    section = (
        "## Automatically collected events\n\n"
        f"- {occurred_at} — [{title}](<{relative_event_path}>) · Dex · "
        f"event `{event_id}`\n"
    )
    section += daily_knowledge_section(
        event.event_path.parents[3], daily_path, day, "Asia/Seoul",
    )
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    return (
        f"# {day.isoformat()}\n\n"
        f"<!-- chronicle:auto:start sha256={digest} -->\n"
        f"{section}<!-- chronicle:auto:end -->\n"
    )


def _pointer_targets(path: Path) -> tuple[str, str | None, Path, Path]:
    """Resolve a daily revision header from the note or snapshot location."""
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    match = _POINTER.fullmatch(first_line)
    if match is None:
        raise AssertionError(f"missing daily revision pointer in {path}")
    previous_id = None if match.group(2) == "none" else match.group(2)
    return (
        match.group(1),
        previous_id,
        Path(os.path.normpath(path.parent / match.group(3))),
        Path(os.path.normpath(path.parent / match.group(4))),
    )


class DailyRevisionLinkTests(ChronicleTestCase):
    def capture(self, event_id: str, title: str, occurred_at: str) -> JournalCommit:
        """Commit one explicit synthetic event to this test's private vault."""
        input_path = self.write_input(f"{event_id}.md", f"Synthetic event {event_id}\n")
        options = CaptureOptions(
            input_path,
            event_id,
            "daily-link-session",
            title,
            "daily-link-test",
            "Dex",
            occurred_at,
            None,
            False,
        )
        request = load_capture_input(options, self.context)
        return capture_event(self.context, request)

    def seed_v2_daily(self, event: JournalCommit, day: date) -> tuple[DailyRevision, bytes]:
        """Write an authentic v2-layout revision to exercise in-place migration."""
        daily_path = self.vault / "50_Daily" / day.strftime("%Y/%m") / f"{day.isoformat()}.md"
        daily_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = _daily_body(day, daily_path, event)
        revision, latest_bytes = create_daily_revision(daily_path, day, body, None)
        _ = daily_path.write_bytes(latest_bytes)
        return revision, latest_bytes

    def test_v2_archive_migration_preserves_every_existing_file_and_links(self) -> None:
        day = date(2026, 12, 1)
        first_event = self.capture(
            "daily-link-first", "First archived event", "2026-12-01T00:30:00+09:00"
        )
        v2_revision, v2_latest = self.seed_v2_daily(first_event, day)
        old_archive = v2_revision.record_path.parent
        old_archive_bytes = {
            path.name: path.read_bytes() for path in old_archive.iterdir()
        }
        daily_path = self.vault / "50_Daily" / "2026/12/2026-12-01.md"

        second_event = self.capture(
            "daily-link-second", "Second event", "2026-12-01T01:30:00+09:00"
        )
        updated = update_daily(self.vault, day, timezone_name=self.config.timezone)

        month_archive = daily_path.parent.parent / "12_Revisions"
        self.assertTrue(old_archive_bytes.keys() <= {
            path.name for path in old_archive.iterdir()
        })
        self.assertEqual(old_archive_bytes, {
            path.name: path.read_bytes()
            for path in old_archive.iterdir()
            if path.name in old_archive_bytes
        })
        self.assertEqual(v2_revision.snapshot_path.read_bytes(), v2_latest)
        self.assertEqual(updated.previous_revision_id, v2_revision.revision_id)

        revision_id, previous_id, record_path, snapshot_path = _pointer_targets(daily_path)
        self.assertEqual(record_path.parent, month_archive)
        self.assertEqual(snapshot_path.parent, month_archive)
        self.assertEqual(revision_id, updated.revision_id)
        self.assertEqual(previous_id, v2_revision.revision_id)
        self.assertEqual(snapshot_path, updated.snapshot_path)
        self.assertTrue(record_path.is_file())
        self.assertTrue(snapshot_path.is_file())

        snapshot_revision_id, snapshot_previous_id, snapshot_record, snapshot_copy = (
            _pointer_targets(snapshot_path)
        )
        self.assertEqual(snapshot_revision_id, revision_id)
        self.assertEqual(snapshot_previous_id, previous_id)
        self.assertEqual(snapshot_record, record_path)
        self.assertEqual(snapshot_copy, snapshot_path)

        current_metadata, _body, _normalized = parse_frontmatter(record_path.read_bytes())
        record_targets = _markdown_targets(record_path)
        self.assertIn(snapshot_path, record_targets)
        previous_path = month_archive / f"{v2_revision.revision_id}.md"
        self.assertIn(previous_path, record_targets)
        self.assertEqual(current_metadata["previous_revision_id"], v2_revision.revision_id)
        self.assertTrue(previous_path.is_file())

        previous_targets = _markdown_targets(previous_path)
        previous_snapshot = month_archive / f"{v2_revision.revision_id}.snapshot.md"
        self.assertIn(previous_snapshot, previous_targets)
        self.assertEqual(previous_snapshot.read_bytes(), v2_latest)
        first_metadata, _first_content = read_capture(first_event.event_path)
        second_metadata, _second_content = read_capture(second_event.event_path)
        first_id = first_metadata.get("event_id")
        second_id = second_metadata.get("event_id")
        if not isinstance(first_id, str) or not isinstance(second_id, str):
            self.fail("captured event IDs are missing")
        self.assertEqual(_event_ids_from(previous_snapshot), {first_id})
        self.assertEqual(_event_ids_from(snapshot_path), {first_id, second_id})

    def test_legacy_snapshot_relative_event_link_survives_archiving(self) -> None:
        day = date(2026, 12, 1)
        event = self.capture(
            "daily-link-legacy", "Legacy evidence event", "2026-12-01T00:30:00+09:00"
        )
        daily_path = self.vault / "50_Daily" / "2026/12/2026-12-01.md"
        daily_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        relative_event_path = Path(
            os.path.relpath(event.event_path, start=daily_path.parent)
        ).as_posix()
        legacy_bytes = (
            f"# {day.isoformat()}\n\n"
            f"Evidence: [source](<{relative_event_path}>)\n"
        ).encode("utf-8")
        _ = daily_path.write_bytes(legacy_bytes)

        updated = update_daily(self.vault, day, timezone_name=self.config.timezone)

        if updated.previous_revision_id is None:
            self.fail("the legacy snapshot must remain in the revision chain")
        _latest_id, previous_id, latest_record, _latest_snapshot = _pointer_targets(
            daily_path
        )
        if previous_id != updated.previous_revision_id:
            self.fail("daily pointer and update result disagree on the previous revision")
        legacy_record = latest_record.parent / f"{previous_id}.md"
        record_metadata, _body, _normalized = parse_frontmatter(legacy_record.read_bytes())
        legacy_snapshot = legacy_record.parent / text_field(
            record_metadata, "snapshot_file"
        )
        self.assertEqual(record_metadata["snapshot_mode"], "legacy")
        self.assertEqual(legacy_snapshot.read_bytes(), legacy_bytes)
        record_targets = _markdown_targets(legacy_record)
        self.assertIn(legacy_snapshot, record_targets)
        event_metadata, _content = read_capture(event.event_path)
        event_id = event_metadata.get("event_id")
        if not isinstance(event_id, str):
            self.fail("captured event ID is missing")
        self.assertEqual(_event_ids_from(legacy_snapshot), {event_id})

    def simulate_v2_pointer_failure(
        self,
    ) -> tuple[date, DailyRevision, bytes, Path, Path]:
        """Create a v2 daily head, publish aliases, and fail latest replacement."""
        day = date(2026, 12, 1)
        first_event = self.capture(
            "retry-first", "First retry event", "2026-12-01T00:30:00+09:00"
        )
        v2_revision, v2_latest = self.seed_v2_daily(first_event, day)
        daily_path = self.vault / "50_Daily/2026/12/2026-12-01.md"
        _ = self.capture(
            "retry-second", "Second retry event", "2026-12-01T01:30:00+09:00"
        )
        with patch(
            "chronicle.daily.atomic_replace",
            side_effect=ChronicleError("durability_error", "injected pointer failure"),
        ), self.assertRaisesRegex(ChronicleError, "durability_error"):
            _ = update_daily(self.vault, day, timezone_name=self.config.timezone)
        self.assertEqual(daily_path.read_bytes(), v2_latest)
        self.assertEqual(v2_revision.snapshot_path.read_bytes(), v2_latest)
        month_archive = daily_path.parent.parent / "12_Revisions"
        self.assertTrue(month_archive.is_dir())
        return day, v2_revision, v2_latest, daily_path, month_archive

