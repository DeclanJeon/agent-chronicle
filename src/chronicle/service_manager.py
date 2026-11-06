"""Explicit profile-bound systemd user installation and resident lifecycle actions."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Protocol, assert_never

from chronicle.atomic import fsync_directory
from chronicle.command_requests import ServiceAction
from chronicle.command_runtime import load_context
from chronicle.errors import ChronicleError
from chronicle.locking import collector_is_running
from chronicle.models import CaptureContext, JsonValue
from chronicle.service_unit import (
    SERVICE_NAME as _SERVICE_NAME,
    read_existing_unit,
    render_user_unit,
    unit_installed,
    unit_path,
    write_unit_new,
)
from chronicle.source_registry import list_sources


@dataclass(frozen=True, slots=True)
class SystemCommandResult:
    """Sanitized process result used by the service executor boundary."""

    returncode: int
    stdout: str


class ServiceExecutor(Protocol):
    """The only adapter allowed to contact the local systemd user manager."""

    def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
        """Run one systemctl --user operation and return captured output."""
        ...


class SystemdUserExecutor:
    """Bounded local requests; start/restart wait for the notify readiness handshake."""

    def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
        """Contact the user's manager without reflecting subprocess diagnostics."""
        executable = shutil.which("systemctl")
        if executable is None:
            raise ChronicleError("service_unavailable", "systemctl is not installed")
        timeout = 180 if arguments[0] in ("enable", "restart") else 45
        try:
            result = subprocess.run(
                [executable, "--user", *arguments], capture_output=True,
                text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ChronicleError("service_timeout", "manager operation timed out; inspect service status") from error
        except OSError as error:
            raise ChronicleError("service_unavailable", "systemctl could not be executed") from error
        return SystemCommandResult(result.returncode, result.stdout)


def _unit_path() -> Path:
    """Return the managed user-unit path; tests use a private fixture path."""
    return unit_path()


def _result_status(result: SystemCommandResult, action: str) -> None:
    """Raise a safe error without echoing manager output or claiming rollback."""
    if result.returncode != 0:
        raise ChronicleError("service_operation_failed", f"systemd {action} failed; inspect service status")


def _number(value: str) -> int | None:
    """Parse bounded nonnegative manager counters without accepting arbitrary text."""
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= 18 else None


def _status(executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Report process lifecycle, restarts, and observed health as separate facts."""
    result = executor.run((
        "show", _SERVICE_NAME, "--property=ActiveState", "--property=UnitFileState",
        "--property=MainPID", "--property=NRestarts", "--property=StatusText", "--property=Result",
    ))
    installed = unit_installed(_unit_path())
    if result.returncode != 0:
        return {"service": _SERVICE_NAME, "status": "not_loaded", "installed": installed}
    fields = dict(line.split("=", maxsplit=1) for line in result.stdout.splitlines() if "=" in line)
    active_state = fields.get("ActiveState", "unknown")
    if active_state not in ("active", "inactive", "failed", "activating", "deactivating", "reloading"):
        active_state = "unknown"
    health = fields.get("StatusText", "").removeprefix("health=")
    if health not in ("healthy", "degraded", "stopped"):
        health = "unknown"
    return {
        "service": _SERVICE_NAME, "status": active_state, "installed": installed,
        "enabled": fields.get("UnitFileState", "unknown") == "enabled",
        "main_pid": _number(fields.get("MainPID", "")),
        "restart_count": _number(fields.get("NRestarts", "")), "health": health,
    }


def _require_owned_unit(path: Path) -> None:
    """Refuse control of unknown units or another runtime/profile at the same name."""
    if not unit_installed(path) or read_existing_unit(path) != render_user_unit().encode("utf-8"):
        raise ChronicleError("service_unit_conflict", "unit differs from this profile/runtime; nothing replaced")


def _require_ready(report: dict[str, JsonValue]) -> None:
    """An enable acknowledgement is not a successfully started notify service."""
    pid = report.get("main_pid")
    if report.get("status") != "active" or not isinstance(pid, int) or pid <= 0:
        raise ChronicleError("service_not_ready", "service is not ready; inspect service status and journal")


def _require_available(context: CaptureContext, executor: ServiceExecutor, *, installed: bool) -> None:
    """Do not start/restart over a manual collector, even when a stopped unit exists."""
    if not collector_is_running(context.paths.state_dir):
        return
    if installed:
        report = _status(executor)
        try:
            _require_ready(report)
            return
        except ChronicleError as error:
            raise ChronicleError("writer_busy", "stop the existing foreground collector first") from error
    raise ChronicleError("writer_busy", "stop the existing foreground collector before service installation")


def _install(preview: bool, confirmed: bool, executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Preview or explicitly install an opted-in profile, then verify live readiness."""
    unit = render_user_unit()
    path = _unit_path()
    if preview:
        return {
            "service": _SERVICE_NAME, "status": "install_preview",
            "unit_path": str(path), "unit_content": unit, "external_delivery": False,
        }
    if not confirmed:
        raise ChronicleError("explicit_confirmation_required", "service install requires --confirm")
    context = load_context()
    if not any(source.enabled for source in list_sources(context)):
        raise ChronicleError("no_enabled_sources", "explicitly enable a source before installing the resident service")
    content = unit.encode("utf-8")
    try:
        current = read_existing_unit(path)
    except ChronicleError as error:
        if error.code != "service_unit_error" or not isinstance(error.__cause__, FileNotFoundError):
            raise
        _require_available(context, executor, installed=False)
        write_unit_new(path, content)
    else:
        if current != content:
            raise ChronicleError("service_unit_conflict", "existing service unit differs; it was not replaced")
        _require_available(context, executor, installed=True)
    _result_status(executor.run(("daemon-reload",)), "daemon-reload")
    _result_status(executor.run(("enable", "--now", _SERVICE_NAME)), "enable")
    report = _status(executor)
    _require_ready(report)
    if report.get("enabled") is not True:
        raise ChronicleError("service_not_enabled", "service did not become login-enabled; inspect service status")
    return {**report, "status": "enabled", "unit_path": str(path)}


def _restart(executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Resume a stopped collector or restart the currently managed resident process."""
    _require_owned_unit(_unit_path())
    _require_available(load_context(), executor, installed=True)
    _result_status(executor.run(("restart", _SERVICE_NAME)), "restart")
    report = _status(executor)
    _require_ready(report)
    return {**report, "status": "restarted"}


def _stop(executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Stop only this profile's known unit; systemd suppresses automatic restart."""
    _require_owned_unit(_unit_path())
    _result_status(executor.run(("stop", _SERVICE_NAME)), "stop")
    return {"service": _SERVICE_NAME, "status": "stopped", "login_enabled_unchanged": True}


def _remove(executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Disable/stop, remove the exact known unit, then reload; never delete records."""
    path = _unit_path()
    _require_owned_unit(path)
    _result_status(executor.run(("disable", "--now", _SERVICE_NAME)), "disable")
    _require_owned_unit(path)
    try:
        path.unlink()
        fsync_directory(path.parent)
    except OSError as error:
        raise ChronicleError("service_unit_error", "unit removal failed; inspect service status") from error
    _result_status(executor.run(("daemon-reload",)), "daemon-reload")
    return {"service": _SERVICE_NAME, "status": "removed", "records_preserved": True}


def manage_service(
    action: ServiceAction, preview: bool = False, confirmed: bool = False,
    executor: ServiceExecutor | None = None,
) -> dict[str, JsonValue]:
    """Dispatch explicit local lifecycle actions, never install from plan/preview."""
    if action == "plan":
        return {
            "service": _SERVICE_NAME, "status": "plan_only",
            "unit_content": render_user_unit(), "external_delivery": False,
        }
    if preview and action != "install":
        raise ChronicleError("invalid_command", "--preview is supported only by service install")
    if preview:
        return _install(True, confirmed, SystemdUserExecutor())
    if action != "status" and not confirmed:
        raise ChronicleError("explicit_confirmation_required", f"service {action} requires --confirm")
    if os.name != "posix" or sys.platform != "linux":
        raise ChronicleError("unsupported_service", "only Linux systemd user services are supported")
    manager = executor if executor is not None else SystemdUserExecutor()
    match action:
        case "install":
            return _install(False, confirmed, manager)
        case "status":
            return _status(manager)
        case "restart":
            return _restart(manager)
        case "stop":
            return _stop(manager)
        case "remove":
            return _remove(manager)
        case _:
            assert_never(action)
