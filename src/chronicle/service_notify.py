"""Minimal local sd_notify support; no dependency or systemd calls outside a service."""

import os
import socket

from chronicle.errors import ChronicleError


def notify_service(health: str, *, ready: bool = False, stopping: bool = False) -> None:
    """Publish bounded lifecycle/health status to the manager-provided Unix socket."""
    selected = os.environ.get("NOTIFY_SOCKET")
    if not selected:
        return
    if not selected.startswith(("/", "@")):
        raise ChronicleError("service_notification_error", "notification socket address is invalid")
    if health not in ("healthy", "degraded", "stopped"):
        raise ChronicleError("service_notification_error", "notification health is invalid")
    address = "\0" + selected[1:] if selected.startswith("@") else selected
    lines = [f"STATUS=health={health}"]
    if ready:
        lines.append("READY=1")
    if stopping:
        lines.append("STOPPING=1")
    payload = "\n".join(lines).encode("ascii")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(1)
            sent = client.sendto(payload, address)
        if sent != len(payload):
            raise ChronicleError("service_notification_error", "notification was incomplete")
    except OSError as error:
        raise ChronicleError("service_notification_error", "service manager notification failed") from error
