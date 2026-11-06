"""Resident startup regressions using private fixtures and a systemd boundary fake."""

import os
import signal
from pathlib import Path
from types import FrameType
from typing import override
from unittest.mock import patch

from chronicle.cli_parser import Arguments, build_parser
from chronicle.errors import ChronicleError
from chronicle.locking import collector_lock
from chronicle.service_manager import SystemCommandResult, manage_service
from chronicle.service_unit import read_existing_unit, render_user_unit
from chronicle.source_registry import enable_source
from tests.support import ChronicleTestCase
from tests.test_cli import json_text
from tests.test_service_manager import FakeSystemdExecutor


class ResidentServiceTests(ChronicleTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        before = {key: os.environ.get(key) for key in self.environment}

        def restore() -> None:
            for key, value in before.items():
                if value is None:
                    _ = os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.update(self.environment)
        self.unit.parent.mkdir()
        unit_patch = patch("chronicle.service_manager._unit_path", return_value=self.unit)
        _ = unit_patch.start()
        self.addCleanup(unit_patch.stop)

    @property
    def unit(self) -> Path:
        return self.root / "user/agent-chronicle.service"

    @property
    def environment(self) -> dict[str, str]:
        return {
            "CHRONICLE_CONFIG_DIR": str(self.paths.config_dir),
            "CHRONICLE_STATE_DIR": str(self.paths.state_dir),
            "CHRONICLE_CACHE_DIR": str(self.paths.cache_dir),
        }

    def test_unit_pins_import_and_profile_without_copying_private_environment(self) -> None:
        # Given explicit operational roots and an unrelated private environment key.
        with patch.dict(os.environ, {"RESIDENT_TEST_PRIVATE": "synthetic-private-value"}):
            # When a unit is rendered without installing anything.
            content = render_user_unit()
        # Then startup is independent of the login shell, and not a secret dump.
        self.assertIn("PYTHONPATH=", content)
        for value in self.environment.values():
            self.assertIn(value, content)
        self.assertNotIn("synthetic-private-value", content)
        self.assertIn("Type=notify", content)
        self.assertIn("Restart=always", content)
        self.assertIn("WantedBy=default.target", content)
        self.assertIn("UMask=0077", content)
        self.assertNotIn("After=default.target", content)
        self.assertFalse(self.unit.exists())

    def test_install_requires_an_explicit_enabled_source_before_writing_unit(self) -> None:
        # Given an initialized profile without source opt-in.
        executor = FakeSystemdExecutor()
        # When resident installation is confirmed.
        with self.assertRaisesRegex(ChronicleError, "no_enabled_sources"):
            _ = manage_service("install", confirmed=True, executor=executor)
        # Then no empty/background collector is silently enabled.
        self.assertFalse(self.unit.exists())
        self.assertEqual(executor.calls, [])

    def test_install_refuses_to_report_enabled_when_manager_is_not_active(self) -> None:
        # Given an opted-in empty synthetic source and successful enable acknowledgement.
        source = self.root / "input"
        source.mkdir()
        _ = enable_source(self.context, "pi-jsonl-v3", source, "root", "resident-test", "Synthetic")
        executor = FakeSystemdExecutor((
            SystemCommandResult(0, ""), SystemCommandResult(0, ""),
            SystemCommandResult(0, "ActiveState=failed\nUnitFileState=enabled\nMainPID=0\n"),
        ))
        # When the real manager boundary reports failed rather than ready.
        with self.assertRaisesRegex(ChronicleError, "service_not_ready"):
            _ = manage_service("install", confirmed=True, executor=executor)
        # Then the known unit remains inspectable/removable; no success is invented.
        self.assertTrue(self.unit.exists())

    def test_stop_refuses_a_foreign_unit_before_contacting_manager(self) -> None:
        # Given an existing unrelated service at the managed name.
        _ = self.unit.write_text("[Service]\nExecStart=/unrelated\n", encoding="utf-8")
        executor = FakeSystemdExecutor()
        # When a stop is requested from this profile.
        with self.assertRaisesRegex(ChronicleError, "service_unit_conflict"):
            _ = manage_service("stop", confirmed=True, executor=executor)
        # Then it does not stop another process merely because its name matches.
        self.assertEqual(executor.calls, [])

    def test_remove_reloads_manager_only_after_unit_has_been_removed(self) -> None:
        # Given a unit belonging exactly to this profile/runtime.
        _ = self.unit.write_text(render_user_unit(), encoding="utf-8")
        self.unit.chmod(0o600)
        unit = self.unit

        class RemovalExecutor:
            def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
                if arguments == ("daemon-reload",):
                    if unit.exists():
                        raise ChronicleError("test_reload_order", "unit still exists during reload")
                return SystemCommandResult(0, "")

        # When the managed service is removed.
        removed = manage_service("remove", confirmed=True, executor=RemovalExecutor())
        # Then the manager no longer retains a removed unit, and data stays intact.
        self.assertEqual(removed["status"], "removed")
        self.assertFalse(self.unit.exists())
        self.assertTrue((self.paths.config_dir / "config.json").is_file())
        self.assertTrue(self.vault.is_dir())

    def test_status_exposes_process_restart_and_observed_health(self) -> None:
        # Given a manager report for a ready but degraded collector.
        executor = FakeSystemdExecutor((SystemCommandResult(0,
            "ActiveState=active\nUnitFileState=enabled\nMainPID=1234\n"
            + "NRestarts=2\nStatusText=health=degraded\nResult=success\n"),))
        # When status is queried.
        status = manage_service("status", executor=executor)
        # Then active process state is not substituted for healthy collection.
        self.assertEqual(status.get("main_pid"), 1234)
        self.assertEqual(status.get("restart_count"), 2)
        self.assertEqual(status.get("health"), "degraded")

    def test_restart_is_an_explicit_confirmed_cli_action(self) -> None:
        # Given the public parser rather than a private dispatch-only entry.
        # When a confirmed restart is parsed.
        arguments = build_parser().parse_args(["service", "restart", "--confirm"], namespace=Arguments())
        # Then the public operation is actually reachable.
        self.assertEqual(arguments.service_action, "restart")
        self.assertTrue(arguments.service_confirm)

    def test_read_unit_rejects_fifo_without_waiting_for_a_writer(self) -> None:
        # Given an unsafe FIFO at a service unit name.
        os.mkfifo(self.unit, mode=0o600)
        # When the unit reader opens it.
        # A timeout alarm keeps the old blocking reader from hanging the test run.
        def timeout(_number: int, _frame: FrameType | None) -> None:
            raise ChronicleError("test_blocking_reader", "unit reader blocked on FIFO")

        old = signal.signal(signal.SIGALRM, timeout)
        try:
            _ = signal.alarm(2)
            with self.assertRaisesRegex(ChronicleError, "unsafe_permissions"):
                _ = read_existing_unit(self.unit)
        finally:
            _ = signal.alarm(0)
            _ = signal.signal(signal.SIGALRM, old)

    def test_install_will_not_compete_with_an_existing_foreground_collector(self) -> None:
        # Given a selected source and a real manually held collector lock.
        source = self.root / "input"
        source.mkdir()
        _ = enable_source(self.context, "pi-jsonl-v3", source, "root", "resident-test", "Synthetic")
        executor = FakeSystemdExecutor()
        with collector_lock(self.paths.state_dir):
            # When service installation would create a second collector.
            with self.assertRaisesRegex(ChronicleError, "writer_busy"):
                _ = manage_service("install", confirmed=True, executor=executor)
        # Then it does not install or call the manager.
        self.assertFalse(self.unit.exists())
        self.assertEqual(executor.calls, [])

    def test_reinstall_does_not_start_a_stopped_unit_over_a_manual_collector(self) -> None:
        # Given a correctly installed but stopped unit and a real manual collector.
        source = self.root / "input"
        source.mkdir()
        _ = enable_source(self.context, "pi-jsonl-v3", source, "root", "resident-test", "Synthetic")
        _ = self.unit.write_text(render_user_unit(), encoding="utf-8")
        self.unit.chmod(0o600)
        executor = FakeSystemdExecutor((SystemCommandResult(0, "ActiveState=inactive\nMainPID=0\n"),))
        with collector_lock(self.paths.state_dir):
            # When an idempotent installation would otherwise bypass the new-unit guard.
            with self.assertRaisesRegex(ChronicleError, "writer_busy"):
                _ = manage_service("install", confirmed=True, executor=executor)
        # Then no enable/start request reaches the manager.
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0][0], "show")
        self.assertEqual(self.unit.read_text(), render_user_unit())

    def test_restart_does_not_compete_with_a_manual_collector(self) -> None:
        # Given a known stopped unit and another foreground collector.
        _ = self.unit.write_text(render_user_unit(), encoding="utf-8")
        self.unit.chmod(0o600)
        executor = FakeSystemdExecutor((SystemCommandResult(0, "ActiveState=inactive\nMainPID=0\n"),))
        with collector_lock(self.paths.state_dir):
            # When restart is requested.
            with self.assertRaisesRegex(ChronicleError, "writer_busy"):
                _ = manage_service("restart", confirmed=True, executor=executor)
        # Then the resident service is not launched into a restart loop.
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0][0], "show")

    def test_failed_enable_leaves_a_known_unit_and_does_not_report_success(self) -> None:
        # Given a source opt-in and an enable failure at the manager boundary.
        source = self.root / "input"
        source.mkdir()
        _ = enable_source(self.context, "pi-jsonl-v3", source, "root", "resident-test", "Synthetic")
        executor = FakeSystemdExecutor((SystemCommandResult(0, ""), SystemCommandResult(1, "private diagnostic")))
        # When installation cannot enable/start the service.
        with self.assertRaisesRegex(ChronicleError, "service_operation_failed") as raised:
            _ = manage_service("install", confirmed=True, executor=executor)
        # Then failure is visible without leaking output or inventing a rollback.
        self.assertNotIn("private diagnostic", str(raised.exception))
        self.assertEqual(self.unit.read_text(), render_user_unit())

    def test_status_does_not_reflect_unknown_manager_health_or_counter_text(self) -> None:
        # Given malformed counters and a manager health string not authored by Chronicle.
        executor = FakeSystemdExecutor((SystemCommandResult(0,
            "ActiveState=arbitrary\nMainPID=bad\nNRestarts=-1\nStatusText=private diagnostic\n"),))
        # When the output crosses the status boundary.
        report = manage_service("status", executor=executor)
        # Then only safe typed/known values are exposed.
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["health"], "unknown")
        self.assertIsNone(report["main_pid"])
        self.assertIsNone(report["restart_count"])

    def test_preview_does_not_claim_startup_has_been_verified(self) -> None:
        # Given no service manager activity.
        executor = FakeSystemdExecutor()
        # When installation is previewed.
        preview = manage_service("install", preview=True, executor=executor)
        # Then only the plan is returned, with a pinned import path.
        self.assertEqual(preview["status"], "install_preview")
        self.assertIn("PYTHONPATH=", json_text(preview["unit_content"]))
        self.assertEqual(executor.calls, [])
        self.assertFalse(self.unit.exists())
