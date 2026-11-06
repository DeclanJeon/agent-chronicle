"""Fail-closed vault selection and non-destructive initialization tests."""

from pathlib import Path
import os
import stat
import unittest
from unittest.mock import patch

from chronicle.atomic import atomic_write_new
from chronicle.destination import initialize_vault, validate_destination
from chronicle.errors import ChronicleError
from tests.support import ChronicleTestCase


class DestinationTests(ChronicleTestCase):
    def test_init_preserves_existing_files_and_creates_private_metadata(self) -> None:
        existing = self.root / "existing-vault"
        existing.mkdir()
        marker = existing / "keep.md"
        _ = marker.write_text("user content stays", encoding="utf-8")
        initialized = initialize_vault(existing, self.config.host_id, self.config.source_instance_id)
        self.assertEqual(initialized, existing)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user content stays")
        for relative in (".chronicle", "80_Events", "90_System/Checkpoints", "10_Projects"):
            directory_stat = (existing / relative).stat()
            self.assertEqual(directory_stat.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(directory_stat.st_mode), 0o700)
        manifest_stat = (existing / ".chronicle/manifest.json").stat()
        self.assertEqual(stat.S_IMODE(manifest_stat.st_mode), 0o600)

    def test_rejects_symbolic_links_known_sync_names_and_separate_mounts(self) -> None:
        real = self.root / "real"
        real.mkdir()
        link = self.root / "linked"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ChronicleError):
            _ = validate_destination(link)
        with self.assertRaises(ChronicleError):
            _ = validate_destination(self.root / "Google Drive" / "vault")
        with patch(
            "chronicle.destination._mount_info",
            side_effect=[(Path("/mnt/other"), "ext4"), (Path("/"), "ext4")],
        ), self.assertRaises(ChronicleError):
            _ = validate_destination(self.root / "separate-mount")

    def test_generated_writes_reject_symlinked_parent_components(self) -> None:
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        outside_month = outside / "11"
        outside_month.mkdir(mode=0o700)
        link = self.vault / "80_Events/2026"
        link.symlink_to(outside, target_is_directory=True)
        target = link / "11/event.md"
        with self.assertRaises(ChronicleError):
            atomic_write_new(target, b"must not escape the vault")
        self.assertFalse((outside_month / "event.md").exists())

    def test_rejects_foreign_vault_manifest_and_never_replaces_it(self) -> None:
        manifest = self.vault / ".chronicle/manifest.json"
        original = manifest.read_bytes()
        altered = original.replace(self.config.host_id.encode(), b"different-host")
        _ = manifest.write_bytes(altered)
        with self.assertRaises(ChronicleError):
            _ = initialize_vault(self.vault, self.config.host_id, self.config.source_instance_id)
        self.assertEqual(manifest.read_bytes(), altered)


if __name__ == "__main__":
    _ = unittest.main()
