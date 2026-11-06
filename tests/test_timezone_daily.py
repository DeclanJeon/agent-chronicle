"""CLI regressions for configured calendar days and immutable daily snapshots."""

import json
from pathlib import Path
from typing import cast, override

from tests.test_cli import (
    CliTestCase,
    FIXTURE,
    json_array,
    json_object,
    json_text,
)


class TimezoneAndDailyCliTests(CliTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.env["TZ"] = "Asia/Seoul"

    def test_search_filters_utc_occurrence_by_host_local_calendar_day(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        captured = json_object(self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "utc-date-event",
            "--session-id", "utc-date-session", "--title", "UTC 날짜 경계",
            "--project", "timezone-test", "--author", "Dex",
            "--occurred-at", "2026-11-05T15:30:00+00:00",
        ))
        event_id = json_text(captured["event_id"])

        local_date_results = json_array(self.checked_json(
            "search", "--query", "UTC 날짜 경계", "--date", "2026-11-06",
        ))
        self.assertTrue(
            any(
                json_text(json_object(row)["event_id"]) == event_id
                for row in local_date_results
            ),
            "UTC 15:30 must be searchable on the configured KST date 2026-11-06",
        )
        previous_date_results = json_array(self.checked_json(
            "search", "--query", "UTC 날짜 경계", "--date", "2026-11-05",
        ))
        self.assertEqual(
            previous_date_results,
            [],
            "the same event must not appear on the previous local date",
        )

    def test_daily_groups_utc_occurrence_by_host_local_calendar_day(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        _ = self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "utc-daily-event",
            "--session-id", "utc-daily-session", "--title", "UTC daily boundary",
            "--project", "timezone-test", "--author", "Dex",
            "--occurred-at", "2026-11-05T15:30:00+00:00",
        )

        local_day = json_object(self.checked_json("daily", "--date", "2026-11-06"))
        previous_day = json_object(self.checked_json("daily", "--date", "2026-11-05"))
        self.assertEqual(
            local_day["event_count"],
            1,
            "the KST calendar day must include the UTC occurrence",
        )
        self.assertEqual(
            previous_day["event_count"],
            0,
            "the previous local day must not include it",
        )

    def test_init_pins_the_process_timezone_when_not_overridden(self) -> None:
        initialized = json_object(self.checked_json("init", "--vault", str(self.vault)))

        self.assertEqual(initialized["timezone"], "Asia/Seoul")
        config = json_object_from_file(self.root / "config/config.json")
        self.assertEqual(config["timezone"], "Asia/Seoul")

    def test_init_migrates_legacy_config_and_preserves_identity(self) -> None:
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        config_path = self.root / "config/config.json"
        current = json_object_from_file(config_path)
        host_id = current["host_id"]
        source_instance_id = current["source_instance_id"]
        vault = current["vault"]
        if not isinstance(host_id, str):
            self.fail("initialized config is missing a host ID")
        if not isinstance(source_instance_id, str):
            self.fail("initialized config is missing a source instance ID")
        if not isinstance(vault, str):
            self.fail("initialized config is missing a vault path")
        legacy = {
            "schema_version": 1,
            "host_id": host_id,
            "source_instance_id": source_instance_id,
            "vault": vault,
        }
        _ = config_path.write_text(json.dumps(legacy), encoding="utf-8")

        _ = self.checked_json("init", "--vault", str(self.vault))

        migrated = json_object_from_file(config_path)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["timezone"], "Asia/Seoul")
        self.assertEqual(migrated["host_id"], host_id)
        self.assertEqual(migrated["source_instance_id"], source_instance_id)

    def test_init_persists_reports_and_pins_the_selected_timezone(self) -> None:
        initialized = json_object(self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul",
        ))
        self.assertEqual(initialized["timezone"], "Asia/Seoul")
        config = json_object_from_file(self.root / "config/config.json")
        self.assertEqual(config["timezone"], "Asia/Seoul")

        changed = self.run_cli("init", "--vault", str(self.vault), "--timezone", "UTC")
        self.assertEqual(changed.returncode, 2)
        self.assertIn("timezone_mismatch", changed.stderr)
        final_config = json_object_from_file(self.root / "config/config.json")
        self.assertEqual(final_config["timezone"], "Asia/Seoul")

    def test_cli_kst_midnight_boundary_and_equivalent_timestamp_offsets(self) -> None:
        initialized = json_object(self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul",
        ))
        self.assertEqual(initialized["timezone"], "Asia/Seoul")
        inputs = (
            ("before-midnight", "2026-11-05T14:59:00+00:00", "2026-11-05"),
            ("after-midnight", "2026-11-05T15:01:00+00:00", "2026-11-06"),
            ("utc-instant", "2026-11-05T15:30:00+00:00", "2026-11-06"),
            ("kst-same-instant", "2026-11-06T00:30:00+09:00", "2026-11-06"),
        )
        source_timestamps: dict[str, str] = {}
        expected_by_day: dict[str, set[str]] = {}
        for suffix, timestamp, expected_day in inputs:
            captured = json_object(self.checked_json(
                "capture", "--file", str(FIXTURE), "--source-event-id", f"tz-{suffix}",
                "--session-id", "timezone-session", "--title", f"TZ boundary {suffix}",
                "--project", "timezone-test", "--author", "Dex",
                "--occurred-at", timestamp,
            ))
            event_id = json_text(captured["event_id"])
            source_timestamps[event_id] = timestamp
            expected_by_day.setdefault(expected_day, set()).add(event_id)

        for day, expected_ids in expected_by_day.items():
            hits = json_array(self.checked_json(
                "search", "--query", "TZ boundary", "--date", day,
            ))
            observed_ids = {json_text(json_object(hit)["event_id"]) for hit in hits}
            self.assertEqual(
                observed_ids, expected_ids, f"wrong event set for configured date {day}"
            )
            self.assertEqual(
                {json_text(json_object(hit)["occurred_at"]) for hit in hits},
                {source_timestamps[event_id] for event_id in expected_ids},
                "local-day indexing must preserve each source timestamp and offset",
            )

        for day, expected_ids in expected_by_day.items():
            daily = json_object(self.checked_json("daily", "--date", day))
            self.assertEqual(daily["event_count"], len(expected_ids))
            daily_path = self.vault / f"50_Daily/{day[:4]}/{day[5:7]}/{day}.md"
            daily_markdown = daily_path.read_text(encoding="utf-8")
            self.assertIn("](<../../../80_Events/", daily_markdown)
            for event_id in expected_ids:
                self.assertIn(f"event `{event_id}`", daily_markdown)

    def test_changed_daily_view_preserves_previous_markdown_snapshot(self) -> None:
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        daily_path = self.vault / "50_Daily/2026/11/2026-11-06.md"
        common = (
            "capture", "--file", str(FIXTURE), "--session-id", "daily-history-session",
            "--project", "daily-history-test", "--author", "Dex",
        )
        first = json_object(self.checked_json(
            *common,
            "--source-event-id", "daily-history-first", "--title", "Daily first",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        ))
        previous_snapshot = daily_path.read_bytes()
        updated = json_object(self.checked_json(
            *common,
            "--source-event-id", "daily-history-second", "--title", "Daily second",
            "--occurred-at", "2026-11-06T13:30:00+09:00",
        ))
        self.assertEqual(updated["daily_event_count"], 2)
        self.assertEqual(
            updated["daily_previous_revision_id"], first["daily_revision_id"]
        )
        latest_markdown = daily_path.read_text(encoding="utf-8")
        self.assertIn("Daily first", latest_markdown)
        self.assertIn("Daily second", latest_markdown)

        revisions = daily_path.parent.parent / "11_Revisions"
        snapshots = tuple(revisions.glob("*.snapshot.md"))
        self.assertTrue(
            any(path.read_bytes() == previous_snapshot for path in snapshots),
            "a changed daily view must retain the exact previous Markdown snapshot",
        )

    def test_daily_noop_does_not_create_a_second_revision(self) -> None:
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        _ = self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "daily-noop-event",
            "--session-id", "daily-noop-session", "--title", "No-op daily",
            "--project", "daily-history-test", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        )
        revisions = self.vault / "50_Daily/2026/11_Revisions"
        original_files = tuple(sorted(path.name for path in revisions.iterdir()))

        _ = self.checked_json("daily", "--date", "2026-11-06")

        self.assertEqual(
            tuple(sorted(path.name for path in revisions.iterdir())), original_files
        )
        self.assertEqual(len(original_files), 2)

    def test_capture_replay_does_not_duplicate_daily_revision(self) -> None:
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        common = (
            "capture", "--file", str(FIXTURE), "--session-id", "daily-replay-session",
            "--project", "daily-history-test", "--author", "Dex",
        )
        first = (
            *common,
            "--source-event-id", "daily-replay-first", "--title", "Replay first",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        )
        _ = self.checked_json(*first)
        _ = self.checked_json(
            *common,
            "--source-event-id", "daily-replay-second", "--title", "Replay second",
            "--occurred-at", "2026-11-06T13:30:00+09:00",
        )
        revisions = self.vault / "50_Daily/2026/11_Revisions"
        previous_files = tuple(sorted(path.name for path in revisions.iterdir()))

        replay = json_object(self.checked_json(*first))

        self.assertTrue(replay["replayed"])
        self.assertEqual(
            tuple(sorted(path.name for path in revisions.iterdir())), previous_files
        )


def json_object_from_file(path: Path) -> dict[str, object]:
    """Decode one small test-only config object without relying on CLI helpers."""
    source = path.read_text(encoding="utf-8")
    decoded: object = json.loads(source)  # pyright: ignore[reportAny]
    if not isinstance(decoded, dict):
        raise AssertionError("expected configuration object")
    entries = cast(dict[object, object], decoded)
    result: dict[str, object] = {}
    for key, value in entries.items():
        if not isinstance(key, str):
            raise AssertionError("configuration keys must be strings")
        result[key] = value
    return result
