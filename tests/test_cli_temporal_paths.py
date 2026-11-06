"""CLI regressions for configured-timezone archive names."""

from chronicle.event_format import read_capture
from tests.test_cli import (
    CliTestCase,
    FIXTURE,
    json_array,
    json_object,
    json_text,
)


class ConfiguredTimezonePathCliTests(CliTestCase):
    def test_kst_archive_names_use_configured_zone_at_month_and_year_boundaries(self) -> None:
        self.env["TZ"] = "UTC"
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        inputs = (
            (
                "utc-offset", "2026-11-30T15:30:00+00:00", "20261201_003000_",
                ("80_Events", "2026", "12"),
            ),
            (
                "kst-offset", "2026-12-01T00:30:00+09:00", "20261201_003000_",
                ("80_Events", "2026", "12"),
            ),
            (
                "year-boundary", "2026-12-31T15:30:00+00:00", "20270101_003000_",
                ("80_Events", "2027", "01"),
            ),
        )

        for source_id, occurred_at, filename_prefix, folder in inputs:
            capture = json_object(self.checked_json(
                "capture", "--file", str(FIXTURE), "--source-event-id", source_id,
                "--session-id", "configured-zone-paths", "--title", source_id,
                "--project", "timezone-paths", "--author", "Dex",
                "--occurred-at", occurred_at,
            ))
            event_id = json_text(capture["event_id"])
            event_path = next(
                path for path in (self.vault / "80_Events").rglob("*.md")
                if path.name.endswith(f"_{event_id[:8]}.md")
            )
            self.assertEqual(event_path.relative_to(self.vault).parts[:3], folder)
            self.assertTrue(event_path.name.startswith(filename_prefix))
            metadata, _content = read_capture(event_path)
            self.assertEqual(metadata["occurred_at"], occurred_at)

            document_id = json_text(metadata["document_id"])
            revisions = tuple(
                (self.vault / "10_Projects" / "timezone-paths" / "05_Deliverables" / document_id)
                .glob("*.md")
            )
            self.assertEqual(len(revisions), 1)
            self.assertTrue(revisions[0].name.startswith(filename_prefix))

    def test_replay_reuses_legacy_revision_path_and_next_revision_uses_configured_zone(
        self,
    ) -> None:
        self.env["TZ"] = "UTC"
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        original_capture = (
            "capture", "--file", str(FIXTURE), "--source-event-id", "upgrade-replay",
            "--session-id", "upgrade-session", "--title", "Timezone replay",
            "--project", "timezone-upgrade", "--author", "Dex",
            "--occurred-at", "2026-11-30T15:30:00+00:00",
        )
        first = json_object(self.checked_json(*original_capture))
        event_id = json_text(first["event_id"])
        event_path = next(
            path for path in (self.vault / "80_Events").rglob("*.md")
            if path.name.endswith(f"_{event_id[:8]}.md")
        )
        event_metadata, _content = read_capture(event_path)
        document_id = json_text(event_metadata["document_id"])
        revision_dir = (
            self.vault / "10_Projects/timezone-upgrade/05_Deliverables" / document_id
        )
        original_revisions = tuple(revision_dir.glob("*.md"))
        self.assertEqual(len(original_revisions), 1)
        original_revision = original_revisions[0]
        self.assertTrue(original_revision.name.startswith("20261201_003000_"))
        legacy_name = original_revision.name.replace(
            "20261201_003000_", "20261130_153000_", 1
        )
        legacy_revision = original_revision.with_name(legacy_name)
        self.assertNotEqual(original_revision, legacy_revision)
        original_bytes = original_revision.read_bytes()
        _ = original_revision.replace(legacy_revision)

        daily_archive = self.vault / "50_Daily/2026/12_Revisions"
        daily_files_before = tuple(sorted(daily_archive.glob("*.md")))
        replay = json_object(self.checked_json(*original_capture))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["revision_id"], first["revision_id"])
        self.assertEqual(replay["checkpoint_id"], first["checkpoint_id"])
        self.assertEqual(replay["daily_revision_id"], first["daily_revision_id"])
        self.assertEqual(legacy_revision.read_bytes(), original_bytes)
        self.assertEqual(tuple(revision_dir.glob("*.md")), (legacy_revision,))
        self.assertEqual(tuple(sorted(daily_archive.glob("*.md"))), daily_files_before)

        rebuilt = json_object(self.checked_json("rebuild"))
        self.assertEqual(rebuilt["committed_events"], 1)
        results = json_array(self.checked_json(
            "search", "--query", "Timezone replay", "--date", "2026-12-01",
        ))
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {json_text(json_object(row)["event_id"]) for row in results}, {event_id}
        )

        follow_up = json_object(self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "upgrade-next",
            "--session-id", "upgrade-session", "--title", "Timezone follow-up",
            "--project", "timezone-upgrade", "--author", "Dex",
            "--occurred-at", "2026-12-01T02:00:00+09:00", "--document-id", document_id,
        ))
        self.assertNotEqual(follow_up["event_id"], first["event_id"])
        self.assertEqual(
            follow_up["daily_previous_revision_id"], first["daily_revision_id"]
        )
        updated_revisions = tuple(sorted(revision_dir.glob("*.md")))
        self.assertEqual(len(updated_revisions), 2)
        revision_numbers: set[int] = set()
        for path in updated_revisions:
            metadata, _content = read_capture(path)
            revision_number = metadata["document_revision"]
            self.assertIsInstance(revision_number, int)
            if isinstance(revision_number, int):
                revision_numbers.add(revision_number)
        self.assertEqual(revision_numbers, {1, 2})
        next_revision = next(
            path for path in updated_revisions
            if path.name.endswith(f"_{json_text(follow_up['revision_id'])[:8]}.md")
        )
        self.assertTrue(next_revision.name.startswith("20261201_020000_"))
        self.assertEqual(legacy_revision.read_bytes(), original_bytes)

    def test_replay_fails_closed_when_revision_id_is_already_duplicated(self) -> None:
        _ = self.checked_json(
            "init", "--vault", str(self.vault), "--timezone", "Asia/Seoul"
        )
        capture_arguments = (
            "capture", "--file", str(FIXTURE), "--source-event-id", "duplicate-replay",
            "--session-id", "duplicate-session", "--title", "Duplicate replay",
            "--project", "timezone-upgrade", "--author", "Dex",
            "--occurred-at", "2026-11-30T15:30:00+00:00",
        )
        capture = json_object(self.checked_json(*capture_arguments))
        event_id = json_text(capture["event_id"])
        event_path = next(
            path for path in (self.vault / "80_Events").rglob("*.md")
            if path.name.endswith(f"_{event_id[:8]}.md")
        )
        event_metadata, _content = read_capture(event_path)
        document_id = json_text(event_metadata["document_id"])
        revision_dir = (
            self.vault / "10_Projects/timezone-upgrade/05_Deliverables" / document_id
        )
        original_revision = next(revision_dir.glob("*.md"))
        duplicate_revision = original_revision.with_name(
            original_revision.name.replace("20261201_003000_", "20261130_153000_", 1)
        )
        original_bytes = original_revision.read_bytes()
        _ = duplicate_revision.write_bytes(original_bytes)
        existing_paths = tuple(sorted(revision_dir.glob("*.md")))

        replay = self.run_cli(*capture_arguments)

        self.assertEqual(replay.returncode, 2)
        self.assertIn("revision_conflict", replay.stderr)
        self.assertEqual(tuple(sorted(revision_dir.glob("*.md"))), existing_paths)
        self.assertEqual(original_revision.read_bytes(), original_bytes)
        self.assertEqual(duplicate_revision.read_bytes(), original_bytes)

    def test_utc_archive_names_ignore_kst_process_and_source_offset(self) -> None:
        self.env["TZ"] = "Asia/Seoul"
        _ = self.checked_json("init", "--vault", str(self.vault), "--timezone", "UTC")
        capture = json_object(self.checked_json(
            "capture", "--file", str(FIXTURE), "--source-event-id", "utc-install-zone",
            "--session-id", "configured-zone-paths", "--title", "UTC install zone",
            "--project", "timezone-paths", "--author", "Dex",
            "--occurred-at", "2026-12-01T00:30:00+09:00",
        ))

        event_id = json_text(capture["event_id"])
        event_path = next(
            path for path in (self.vault / "80_Events").rglob("*.md")
            if path.name.endswith(f"_{event_id[:8]}.md")
        )
        self.assertEqual(
            event_path.relative_to(self.vault).parts[:3],
            ("80_Events", "2026", "11"),
        )
        self.assertTrue(event_path.name.startswith("20261130_153000_"))
        metadata, _content = read_capture(event_path)
        self.assertEqual(metadata["occurred_at"], "2026-12-01T00:30:00+09:00")

        document_id = json_text(metadata["document_id"])
        revisions = tuple(
            (self.vault / "10_Projects" / "timezone-paths" / "05_Deliverables" / document_id)
            .glob("*.md")
        )
        self.assertEqual(len(revisions), 1)
        self.assertTrue(revisions[0].name.startswith("20261130_153000_"))
