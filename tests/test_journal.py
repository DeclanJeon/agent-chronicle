"""Durable journal, replay, revision, recovery, and daily-note regressions."""

from datetime import date
from pathlib import Path
import re
import sqlite3

from chronicle.capture_input import load_capture_input
from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.journal import capture_event
from chronicle.models import CaptureOptions, CapturedInput
from chronicle.recovery import rebuild_from_markdown
from chronicle.search import SearchRequest, search_documents
from chronicle.locking import writer_lock
from tests.support import ChronicleTestCase


class JournalTests(ChronicleTestCase):
    def make_capture(self, name: str, event_id: str, content: str, document_id: str) -> CapturedInput:
        path = self.write_input(name, content)
        options = CaptureOptions(
            path, event_id, "synthetic-session", "Checkpoint recovery", "demo-project",
            "Dex", "2026-11-06T12:30:00+09:00", document_id, False,
        )
        return load_capture_input(options, self.context)

    def test_replay_is_idempotent_and_new_event_creates_revision(self) -> None:
        document_id = "4efb7a7c-c3b5-4cd3-8ca6-b3d01d1db182"
        first = self.make_capture("first.md", "event-001", "검증 본문 한글\n", document_id)
        first_commit = capture_event(self.context, first)
        replay = capture_event(self.context, first)
        self.assertTrue(replay.replayed)
        self.assertEqual(first_commit.event_id, replay.event_id)
        second = self.make_capture("second.md", "event-002", "새 회차 본문\n", document_id)
        second_commit = capture_event(self.context, second)
        self.assertNotEqual(first_commit.event_id, second_commit.event_id)
        revision_metadata = read_capture(second_commit.revision_path)[0]
        self.assertEqual(revision_metadata["document_revision"], 2)
        snapshot = verify_checkpoint_chain(self.vault)
        self.assertEqual(len(snapshot.references), 2)
        revision_files = list((self.vault / "10_Projects/demo-project/05_Deliverables" / document_id).glob("*.md"))
        self.assertEqual(len(revision_files), 2)

    def test_writer_lock_rejects_symlinked_lock_file(self) -> None:
        external_lock = self.root / "external.lock"
        _ = external_lock.write_text("untouched", encoding="utf-8")
        lock_path = self.vault / ".chronicle/writer.lock"
        lock_path.symlink_to(external_lock)
        with self.assertRaises(ChronicleError):
            with writer_lock(self.vault):
                self.fail("symlinked writer lock must be rejected")
        self.assertEqual(external_lock.read_text(encoding="utf-8"), "untouched")

    def test_capture_rejects_symlinked_project_tree_before_committing_event(self) -> None:
        outside = self.root / "external-project"
        outside.mkdir(mode=0o700)
        project_tree = self.vault / "10_Projects/demo-project"
        project_tree.symlink_to(outside, target_is_directory=True)
        request = self.make_capture(
            "symlinked-project.md", "event-symlinked-project", "must stay outside the vault\n",
            "82c19fb3-c4b7-42cf-81e7-6744a5f4f004",
        )
        with self.assertRaises(ChronicleError):
            _ = capture_event(self.context, request)
        self.assertEqual(verify_checkpoint_chain(self.vault).references, ())
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflicting_replay_is_rejected_without_second_commit(self) -> None:
        request = self.make_capture("mutable.md", "event-conflict", "original text\n", "7c5fa40a-f446-4e3b-8fa4-12eb2c3b0634")
        first = capture_event(self.context, request)
        changed = self.make_capture("mutable.md", "event-conflict", "changed text\n", "7c5fa40a-f446-4e3b-8fa4-12eb2c3b0634")
        with self.assertRaisesRegex(ChronicleError, "idempotency_conflict"):
            _ = capture_event(self.context, changed)
        self.assertEqual(len(verify_checkpoint_chain(self.vault).references), 1)
        self.assertEqual(first.event_id, verify_checkpoint_chain(self.vault).references[0].event_id)

    def test_redacts_recognizable_credentials_before_durable_write(self) -> None:
        request = self.make_capture(
            "credential.md", "event-secret", "api_key=synthetic-secret-123\nBearer abcdefgh12345678\n",
            "0c1f4e5c-c94f-42bf-8746-85dc4f66b738",
        )
        result = capture_event(self.context, request)
        event_text = result.event_path.read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", event_text)
        self.assertNotIn("synthetic-secret-123", event_text)
        self.assertNotIn("abcdefgh12345678", event_text)

    def test_orphan_event_is_reported_and_replay_completes_checkpoint(self) -> None:
        request = self.make_capture("orphan.md", "event-orphan", "replay after crash\n", "a3b8dd1e-1aa1-4c0d-a1a9-53bda522c47f")
        with self.assertRaisesRegex(ChronicleError, "injected_failure"):
            _ = capture_event(self.context, request, fault_at="after_event")
        with writer_lock(self.vault):
            recovery = rebuild_from_markdown(self.context)
        self.assertEqual(recovery.committed_events, 0)
        self.assertEqual(len(recovery.orphan_event_ids), 1)
        committed = capture_event(self.context, request)
        self.assertTrue(committed.replayed)
        self.assertEqual(len(verify_checkpoint_chain(self.vault).references), 1)

    def test_checkpoint_commits_before_sqlite_and_recovery_rebuilds_projection(self) -> None:
        request = self.make_capture("after-checkpoint.md", "event-after-checkpoint", "durable checkpoint\n", "27235382-02c3-4e6b-95e0-2034f171e7fa")
        with self.assertRaisesRegex(ChronicleError, "injected_failure"):
            _ = capture_event(self.context, request, fault_at="after_checkpoint")
        with writer_lock(self.vault):
            recovered = rebuild_from_markdown(self.context)
        self.assertEqual(recovered.committed_events, 1)
        self.assertTrue((self.paths.state_dir / "projection.sqlite3").exists())
        retry = capture_event(self.context, request)
        self.assertTrue(retry.replayed)

    def test_sqlite_deletion_rebuilds_same_committed_identity(self) -> None:
        request = self.make_capture("rebuild.md", "event-rebuild", "reconstruct from Markdown\n", "02e3476f-aac3-42d6-a70e-01cff2540bc5")
        committed = capture_event(self.context, request)
        database = self.paths.state_dir / "projection.sqlite3"
        connection = sqlite3.connect(database)
        original = connection.execute("SELECT event_id, idempotency_key FROM events").fetchall()
        connection.close()
        database.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        with writer_lock(self.vault):
            rebuilt = rebuild_from_markdown(self.context)
        self.assertEqual(rebuilt.committed_events, 1)
        connection = sqlite3.connect(database)
        reconstructed = connection.execute("SELECT event_id, idempotency_key FROM events").fetchall()
        connection.close()
        self.assertEqual(original, reconstructed)
        self.assertEqual(original[0][0], committed.event_id)

    def test_corrupt_checkpoint_metadata_digest_stops_recovery(self) -> None:
        request = self.make_capture(
            "checkpoint-digest.md", "event-checkpoint-digest", "digest-bound metadata\n",
            "bc798ea6-6c1f-4d0d-8e1e-e8020b954d11",
        )
        _ = capture_event(self.context, request)
        checkpoint_path = verify_checkpoint_chain(self.vault).checkpoints[0].path
        original = checkpoint_path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^checkpoint_digest: ([a-f0-9]{64})$", original)
        if match is None:
            self.fail("checkpoint digest field not found")
        invalid_digest = f"g{'0' * 63}"
        altered = f"{original[:match.start(1)]}{invalid_digest}{original[match.end(1):]}"
        _ = checkpoint_path.write_text(altered, encoding="utf-8")
        with self.assertRaisesRegex(ChronicleError, "checkpoint_corrupt"):
            _ = verify_checkpoint_chain(self.vault)

    def test_search_rejects_artifact_revision_tampering(self) -> None:
        request = self.make_capture(
            "revision-search.md", "event-search-revision", "authoritative source text\n",
            "11ea19ce-bc5d-491d-bb67-a7e4c22f4900",
        )
        committed = capture_event(self.context, request)
        _ = committed.revision_path.write_text(
            committed.revision_path.read_text(encoding="utf-8").replace(
                "authoritative source text", "authoritative source test"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ChronicleError, "revision_conflict"):
            _ = search_documents(
                self.vault,
                SearchRequest("test", None, None, None),
                self.config.timezone,
            )

    def test_corrupt_checkpointed_markdown_stops_recovery(self) -> None:
        request = self.make_capture("corrupt.md", "event-corrupt", "original checkpoint body\n", "b3887a52-fc85-4ef3-a0a6-d3d6a6bdc177")
        committed = capture_event(self.context, request)
        _ = committed.event_path.write_text(
            committed.event_path.read_text(encoding="utf-8").replace("original checkpoint body", "altered body"),
            encoding="utf-8",
        )
        with writer_lock(self.vault), self.assertRaises(ChronicleError):
            _ = rebuild_from_markdown(self.context)

    def test_daily_note_preserves_user_area_and_emits_review_copy_on_auto_edit(self) -> None:
        request = self.make_capture("daily.md", "event-daily", "daily generated text\n", "66a1801e-1c15-4719-b506-7140f6c4e922")
        _ = capture_event(self.context, request)
        daily_result = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )
        daily_path = daily_result.path
        self.assertIsNone(daily_result.conflict_copy)
        original = daily_path.read_text(encoding="utf-8")
        self.assertIn("[Checkpoint recovery](<../../../80_Events/", original)
        _ = daily_path.write_text(
            original.replace(
                "<!-- chronicle:auto:start",
                "## User note\n\nKeep this\n\n<!-- chronicle:auto:start",
            ),
            encoding="utf-8",
        )
        _ = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )
        edited = daily_path.read_text(encoding="utf-8")
        self.assertIn("Keep this", edited)
        _ = daily_path.write_text(edited.replace("Checkpoint recovery", "user-edited generated area"), encoding="utf-8")
        daily_result = update_daily(
            self.vault, date(2026, 11, 6), timezone_name=self.config.timezone
        )
        self.assertIsNotNone(daily_result.conflict_copy)
        self.assertIn("user-edited generated area", daily_path.read_text(encoding="utf-8"))
        conflict_files = list(daily_path.parent.glob("*conflict*.md"))
        self.assertEqual(len(conflict_files), 1)
        self.assertIn("user-edited generated area", conflict_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
