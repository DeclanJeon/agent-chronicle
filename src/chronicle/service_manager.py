"""Opt-in systemd-user service plans and confirmation-gated lifecycle actions."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal, Protocol

from chronicle.atomic import fsync_directory
from chronicle.errors import ChronicleError
from chronicle.models import JsonValue
from chronicle.service_unit import (
    SERVICE_NAME as _SERVICE_NAME,
    read_existing_unit,
    render_user_unit,
    unit_installed,
    unit_path,
    write_unit_new,
)

@dataclass(frozen=True, slots=True)
class SystemCommandResult:
    """Sanitized process result used by the service executor boundary."""

    returncode: int
    stdout: str


class ServiceExecutor(Protocol):
    """The only adapter allowed to contact the local systemd user manager."""

    def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
        """Run one `systemctl --user` operation and return captured output."""
        ...


class SystemdUserExecutor:
    """Production executor; never invoked by plan or preview actions."""

    def run(self, arguments: tuple[str, ...]) -> SystemCommandResult:
        """Run a bounded local systemctl request without exposing stderr."""
        executable = shutil.which("systemctl")
        if executable is None:
            raise ChronicleError("service_unavailable", "systemctl is not installed")
        try:
            result = subprocess.run(
                [executable, "--user", *arguments],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ChronicleError("service_timeout", "systemd user manager did not respond") from error
        except OSError as error:
            raise ChronicleError("service_unavailable", "systemctl could not be executed") from error
        return SystemCommandResult(result.returncode, result.stdout)


def _unit_path() -> Path:
    """Return the managed user-unit path; kept patchable for fake-executor tests."""
    return unit_path()


def _result_status(result: SystemCommandResult, action: str) -> None:
    """Raise one generic error without echoing systemctl output."""
    if result.returncode != 0:
        raise ChronicleError("service_operation_failed", f"systemd {action} failed")


def _status(executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Read active and enablement state without returning manager details."""
    result = executor.run(("show", _SERVICE_NAME, "--property=ActiveState", "--property=UnitFileState"))
    if result.returncode != 0:
        return {"service": _SERVICE_NAME, "status": "not_loaded", "installed": unit_installed(_unit_path())}
    fields = dict(
        line.split("=", maxsplit=1) for line in result.stdout.splitlines()
        if "=" in line
    )
    active_state = fields.get("ActiveState", "unknown")
    if active_state not in ("active", "inactive", "failed", "activating", "deactivating", "reloading"):
        active_state = "unknown"
    return {
        "service": _SERVICE_NAME,
        "status": active_state,
        "enabled": fields.get("UnitFileState", "unknown") == "enabled",
        "installed": unit_installed(_unit_path()),
    }


def _install(preview: bool, confirmed: bool, executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Preview or explicitly install and enable one idempotent unit file."""
    unit = render_user_unit()
    path = _unit_path()
    if preview:
        return {
            "service": _SERVICE_NAME,
            "status": "install_preview",
            "unit_path": str(path),
            "unit_content": unit,
            "external_delivery": False,
        }
    if not confirmed:
        raise ChronicleError("explicit_confirmation_required", "service install requires --confirm")
    if os.name != "posix" or sys.platform != "linux":
        raise ChronicleError("unsupported_service", "only Linux systemd user services are supported")
    content = unit.encode("utf-8")
    try:
        current = read_existing_unit(path)
    except ChronicleError as error:
        if error.code != "service_unit_error" or not isinstance(error.__cause__, FileNotFoundError):
            raise
        write_unit_new(path, content)
    else:
        if current != content:
            raise ChronicleError("service_unit_conflict", "existing service unit differs; it was not replaced")
    _result_status(executor.run(("daemon-reload",)), "daemon-reload")
    _result_status(executor.run(("enable", "--now", _SERVICE_NAME)), "enable")
    return {"service": _SERVICE_NAME, "status": "enabled", "unit_path": str(path)}


def _stop(confirmed: bool, executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Stop the service only after an explicit user confirmation flag."""
    if not confirmed:
        raise ChronicleError("explicit_confirmation_required", "service stop requires --confirm")
    _result_status(executor.run(("stop", _SERVICE_NAME)), "stop")
    return {"service": _SERVICE_NAME, "status": "stopped"}


def _remove(confirmed: bool, executor: ServiceExecutor) -> dict[str, JsonValue]:
    """Disable, stop, reload, and remove only the exact managed unit file."""
    if not confirmed:
        raise ChronicleError("explicit_confirmation_required", "service remove requires --confirm")
    path = _unit_path()
    if not unit_installed(path) or read_existing_unit(path) != render_user_unit().encode("utf-8"):
        raise ChronicleError("service_unit_conflict", "refusing to remove an unknown or modified service unit")
    _result_status(executor.run(("disable", "--now", _SERVICE_NAME)), "disable")
    _result_status(executor.run(("daemon-reload",)), "daemon-reload")
    if read_existing_unit(path) != render_user_unit().encode("utf-8"):
        raise ChronicleError("service_unit_conflict", "service unit changed during removal")
    try:
        path.unlink(missing_ok=True)
        fsync_directory(path.parent)
    except OSError as error:
        raise ChronicleError("service_unit_error", "service unit could not be removed") from error
    return {"service": _SERVICE_NAME, "status": "removed"}


def manage_service(
    action: Literal["plan", "install", "status", "stop", "remove"],
    preview: bool = False,
    confirmed: bool = False,
    executor: ServiceExecutor | None = None,
) -> dict[str, JsonValue]:
    """Dispatch a local service action; tests inject a fake executor."""
    if action == "plan":
        return {
            "service": _SERVICE_NAME,
            "status": "plan_only",
            "unit_content": render_user_unit(),
            "external_delivery": False,
        }
    if preview and action != "install":
        raise ChronicleError("invalid_command", "--preview is supported only by service install")
    if preview:
        return _install(True, confirmed, SystemdUserExecutor())
    if sys.platform != "linux":
        raise ChronicleError("unsupported_service", "only Linux systemd user services are supported")
    manager = executor if executor is not None else SystemdUserExecutor()
    if action == "install":
        return _install(False, confirmed, manager)
    if action == "status":
        return _status(manager)
    if action == "stop":
        return _stop(confirmed, manager)
    return _remove(confirmed, manager)
