"""Independent-QA regression: diagnostics must not create operational roots or locks."""

from pathlib import Path
import stat

from chronicle.locking import collector_lock, collector_is_running
from tests.test_cli import CliTestCase, json_object


def _snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    """Record names, permissions and bytes without normalizing a missing root."""
    if not root.exists():
        return ()
    return tuple(sorted(
        (str(path.relative_to(root)), stat.S_IMODE(path.lstat().st_mode), path.read_bytes() if path.is_file() else b"")
        for path in (root, *root.rglob("*"))
    ))


class ReadOnlyDoctorTests(CliTestCase):
    def test_fresh_doctor_preserves_vault_config_state_and_cache(self) -> None:
        # Given initialized storage, but no state/cache directory or collector lock.
        _ = self.checked_json("init", "--vault", str(self.vault))
        state = self.root / "state"
        cache = self.root / "cache"
        self.assertFalse(state.exists())
        self.assertFalse(cache.exists())
        before = _snapshot(self.root)
        # When the actual subprocess diagnostic command runs.
        report = json_object(self.checked_json("doctor"))
        # Then all roots/names/permissions/content remain exactly as before.
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(_snapshot(self.root), before)
        self.assertFalse(state.exists())
        self.assertFalse(cache.exists())

    def test_existing_empty_state_is_not_given_a_collector_lock_by_doctor(self) -> None:
        # Given an existing private state root without a collector lock.
        _ = self.checked_json("init", "--vault", str(self.vault))
        state = self.root / "state"
        state.mkdir(mode=0o700)
        before = _snapshot(self.root)
        # When doctor inspects it.
        _ = self.checked_json("doctor")
        # Then neither the state root nor any sibling gains a file.
        self.assertEqual(_snapshot(self.root), before)
        self.assertFalse((state / "collector.lock").exists())

    def test_read_only_probe_still_observes_an_actual_collector_lock(self) -> None:
        # Given a real held collector lock rather than a mocked running flag.
        state = self.root / "state"
        with collector_lock(state):
            before = _snapshot(state)
            # When the read-only probe checks that same file.
            running = collector_is_running(state)
            # Then lock observation works without changing names or bytes.
            self.assertTrue(running)
            self.assertEqual(_snapshot(state), before)
        self.assertFalse(collector_is_running(state))
