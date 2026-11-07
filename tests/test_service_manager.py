"""Synthetic service lifecycle tests with a fake systemd executor."""

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from chronicle.errors import ChronicleError
from chronicle.service_manager import SystemCommandResult, manage_service
from chronicle.source_registry import enable_source
from tests.support import ChronicleTestCase
from tests.test_cli import json_text


class FakeSystemdExecutor:
    """Record service calls without contacting a system user manager."""

    def __init__(self, results: tuple[SystemCommandResult, ...] = ()) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.results: list[SystemCommandResult] = list(results)

    def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
        self.calls.append(arguments)
        if self.results:
            return self.results.pop(0)
        return SystemCommandResult(0, "")


class ServiceManagerTests(ChronicleTestCase):
    def test_plan_and_install_preview_never_call_executor(self) -> None:
        executor = FakeSystemdExecutor()
        plan = manage_service("plan", executor=executor)
        preview = manage_service("install", preview=True, executor=executor)
        self.assertEqual(plan["status"], "plan_only")
        self.assertEqual(preview["status"], "install_preview")
        self.assertIn("ExecStart=", json_text(plan["unit_content"]))
        self.assertEqual(executor.calls, [])

    def test_mutations_require_confirmation_before_executor_calls(self) -> None:
        executor = FakeSystemdExecutor()
        with self.assertRaisesRegex(ChronicleError, "explicit_confirmation_required"):
            _ = manage_service("stop", executor=executor)
        with self.assertRaisesRegex(ChronicleError, "explicit_confirmation_required"):
            _ = manage_service("remove", executor=executor)
        self.assertEqual(executor.calls, [])

    def test_install_status_stop_and_remove_use_only_fake_executor(self) -> None:
        source = self.root / "input"
        source.mkdir()
        _ = enable_source(self.context, "pi-jsonl-v3", source, "root", "service-test", "Synthetic")
        active = "ActiveState=active\nUnitFileState=enabled\nMainPID=1234\nStatusText=health=healthy\n"
        executor = FakeSystemdExecutor((
            SystemCommandResult(0, ""), SystemCommandResult(0, ""),
            SystemCommandResult(0, active), SystemCommandResult(0, active),
        ))
        with tempfile.TemporaryDirectory(prefix="chronicle-service-") as temporary:
            unit_path = Path(temporary) / "systemd/user/agent-chronicle.service"
            unit_path.parent.mkdir(parents=True)
            with patch.dict(os.environ, {
                "CHRONICLE_CONFIG_DIR": str(self.paths.config_dir),
                "CHRONICLE_STATE_DIR": str(self.paths.state_dir),
                "CHRONICLE_CACHE_DIR": str(self.paths.cache_dir),
            }), patch("chronicle.service_manager._unit_path", return_value=unit_path):
                installed = manage_service("install", confirmed=True, executor=executor)
                self.assertEqual(installed["status"], "enabled")
                self.assertTrue(unit_path.is_file())
                status = manage_service("status", executor=executor)
                self.assertEqual(status["status"], "active")
                self.assertTrue(status["enabled"])
                stopped = manage_service("stop", confirmed=True, executor=executor)
                self.assertEqual(stopped["status"], "stopped")
                removed = manage_service("remove", confirmed=True, executor=executor)
                self.assertEqual(removed["status"], "removed")
                self.assertFalse(unit_path.exists())
        self.assertEqual(executor.calls, [
            ("daemon-reload",),
            ("enable", "--now", "agent-chronicle.service"),
            ("show", "agent-chronicle.service", "--property=ActiveState", "--property=UnitFileState",
             "--property=MainPID", "--property=NRestarts", "--property=StatusText", "--property=Result"),
            ("show", "agent-chronicle.service", "--property=ActiveState", "--property=UnitFileState",
             "--property=MainPID", "--property=NRestarts", "--property=StatusText", "--property=Result"),
            ("stop", "agent-chronicle.service"),
            ("disable", "--now", "agent-chronicle.service"),
            ("daemon-reload",),
        ])
