"""Capture-time masking and replay durability boundary regressions."""

import os
from pathlib import Path
from unittest.mock import patch

from chronicle.capture_input import load_capture_input
from chronicle.checkpoints import verify_checkpoint_chain
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.journal import capture_event
from chronicle.models import CaptureOptions, CapturedInput
from chronicle.search import SearchRequest, search_documents
from tests.support import ChronicleTestCase


class PrivacyAndDurabilityTests(ChronicleTestCase):
    def make_capture(
        self, name: str, event_id: str, content: str, document_id: str
    ) -> CapturedInput:
        path = self.write_input(name, content)
        options = CaptureOptions(
            path,
            event_id,
            "synthetic-session",
            "Capture privacy and durability",
            "privacy-test",
            "Dex",
            "2026-11-06T12:30:00+09:00",
            document_id,
            False,
        )
        return load_capture_input(options, self.context)

    def _leave_event_after_parent_fsync_failure(
        self, source_event_id: str
    ) -> tuple[CapturedInput, Path]:
        request = self.make_capture(
            f"{source_event_id}.md",
            source_event_id,
            "durability retry body\n",
            "bd098eb5-a4dd-4931-a90f-58dabf54013d",
        )
        event_parent = self.vault / "80_Events/2026/11"
        original_fsync = os.fsync
        failed = False

        def fail_event_parent_once(descriptor: int) -> None:
            nonlocal failed
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_path == event_parent and not failed:
                failed = True
                raise OSError("injected event parent fsync failure")
            original_fsync(descriptor)

        with patch("chronicle.atomic.os.fsync", side_effect=fail_event_parent_once):
            with self.assertRaisesRegex(ChronicleError, "durability_error"):
                _ = capture_event(self.context, request)

        self.assertTrue(failed)
        event_paths = tuple((self.vault / "80_Events").rglob("*.md"))
        self.assertEqual(len(event_paths), 1)
        checkpoint_paths = tuple((self.vault / "90_System/Checkpoints").glob("*.md"))
        self.assertEqual(checkpoint_paths, ())
        return request, event_paths[0]

    def test_cookie_session_and_email_are_masked_across_capture_outputs(self) -> None:
        sensitive_values = (
            "qa-cookie-secret",
            "qa-cookie-header-secret",
            "qa-session-secret",
            "qa-set-cookie-secret",
            "qa-person@example.test",
        )
        request = self.make_capture(
            "privacy.md",
            "event-privacy",
            """cookie=qa-cookie-secret; email=qa-person@example.test
Cookie: sid=qa-cookie-header-secret; theme=dark
session_id=qa-session-secret
Set-Cookie: sessionid=qa-set-cookie-secret; Path=/; HttpOnly
""",
            "cb030615-d680-4f16-a03d-65101e0e926d",
        )
        result = capture_event(self.context, request)
        event_text = result.event_path.read_bytes()
        revision_text = result.revision_path.read_bytes()
        projection_path = self.paths.state_dir / "projection.sqlite3"
        persisted_bytes = b"\n".join(
            path.read_bytes()
            for root in (self.vault, self.paths.state_dir)
            for path in root.rglob("*")
            if path.is_file()
        )
        hits = search_documents(
            self.vault, SearchRequest("[REDACTED]", None, None, None), self.config.timezone
        )
        search_output = repr(hits).encode("utf-8")

        self.assertIn(b"[REDACTED]", event_text)
        self.assertIn(b"[REDACTED]", revision_text)
        self.assertTrue(projection_path.is_file())
        self.assertEqual({hit.kind for hit in hits}, {"event", "artifact_revision"})
        self.assertEqual(
            tuple(value for value in sensitive_values if value.encode() in persisted_bytes), ()
        )
        self.assertEqual(
            tuple(value for value in sensitive_values if value.encode() in search_output), ()
        )
        for value in sensitive_values:
            self.assertEqual(
                search_documents(
                    self.vault, SearchRequest(value, None, None, None), self.config.timezone
                ),
                [],
            )

    def test_replay_reestablishes_event_durability_before_checkpoint(self) -> None:
        request, event_path = self._leave_event_after_parent_fsync_failure(
            "event-fsync-retry"
        )
        event_bytes = event_path.read_bytes()
        sync_order: list[Path] = []
        original_fsync = os.fsync

        def observe_fsync(descriptor: int) -> None:
            sync_order.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
            original_fsync(descriptor)

        with patch("chronicle.atomic.os.fsync", side_effect=observe_fsync):
            result = capture_event(self.context, request)

        checkpoint_dir = self.vault / "90_System/Checkpoints"
        self.assertTrue(result.replayed)
        self.assertEqual(result.event_id, read_capture(event_path)[0]["event_id"])
        self.assertEqual(event_path.read_bytes(), event_bytes)
        self.assertEqual(len(verify_checkpoint_chain(self.vault).references), 1)
        self.assertLess(sync_order.index(event_path), sync_order.index(event_path.parent))
        self.assertLess(
            sync_order.index(event_path.parent), sync_order.index(checkpoint_dir)
        )

    def test_retry_file_fsync_failure_does_not_checkpoint_or_advance_cursor(self) -> None:
        request, event_path = self._leave_event_after_parent_fsync_failure(
            "event-fsync-retry-file-fails"
        )
        event_bytes = event_path.read_bytes()
        original_fsync = os.fsync

        def fail_event_file_fsync(descriptor: int) -> None:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_path == event_path:
                raise OSError("injected retry event file fsync failure")
            original_fsync(descriptor)

        with patch("chronicle.atomic.os.fsync", side_effect=fail_event_file_fsync):
            with self.assertRaisesRegex(ChronicleError, "durability_error"):
                _ = capture_event(self.context, request)

        self.assertEqual(event_path.read_bytes(), event_bytes)
        self.assertEqual(verify_checkpoint_chain(self.vault).references, ())
        self.assertEqual(
            tuple((self.vault / "90_System/Checkpoints").glob("*.md")), ()
        )
        self.assertFalse((self.paths.state_dir / "projection.sqlite3").exists())

    def test_retry_parent_fsync_failure_does_not_checkpoint_or_advance_cursor(self) -> None:
        request, event_path = self._leave_event_after_parent_fsync_failure(
            "event-fsync-retry-parent-fails"
        )
        event_bytes = event_path.read_bytes()
        original_fsync = os.fsync

        def fail_event_parent_fsync(descriptor: int) -> None:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_path == event_path.parent:
                raise OSError("injected retry event parent fsync failure")
            original_fsync(descriptor)

        with patch("chronicle.atomic.os.fsync", side_effect=fail_event_parent_fsync):
            with self.assertRaisesRegex(ChronicleError, "durability_error"):
                _ = capture_event(self.context, request)

        self.assertEqual(event_path.read_bytes(), event_bytes)
        self.assertEqual(verify_checkpoint_chain(self.vault).references, ())
        self.assertEqual(
            tuple((self.vault / "90_System/Checkpoints").glob("*.md")), ()
        )
        self.assertFalse((self.paths.state_dir / "projection.sqlite3").exists())


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
