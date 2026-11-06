"""Real local datagram tests for the systemd startup/health handshake."""

import socket
import uuid

import pytest

from chronicle.errors import ChronicleError
from chronicle.service_notify import notify_service


def test_ready_reaches_the_manager_socket_with_observed_health(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real Linux abstract Unix datagram socket rather than a mocked send.
    name = f"chronicle-notify-{uuid.uuid4().hex}"
    monkeypatch.setenv("NOTIFY_SOCKET", f"@{name}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(f"\0{name}")
        server.settimeout(2)
        # When startup readiness is reported with degraded collection health.
        notify_service("degraded", ready=True)
        received = server.recv(1024)
    # Then readiness is not falsely equated with healthy input.
    assert set(received.splitlines()) == {b"READY=1", b"STATUS=health=degraded"}


def test_stop_notifies_the_manager_without_claiming_a_new_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real manager-provided socket.
    name = f"chronicle-notify-{uuid.uuid4().hex}"
    monkeypatch.setenv("NOTIFY_SOCKET", f"@{name}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(f"\0{name}")
        server.settimeout(2)
        # When an intentional stop is acknowledged.
        notify_service("stopped", stopping=True)
        received = server.recv(1024)
    # Then no READY message is sent.
    assert set(received.splitlines()) == {b"STOPPING=1", b"STATUS=health=stopped"}


def test_foreground_use_does_not_require_a_manager_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given ordinary CLI execution outside a systemd service.
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # When the daemon publishes lifecycle state.
    result = notify_service("healthy", ready=True)
    # Then foreground usage remains valid without a socket or dependency.
    assert result is None


def test_invalid_socket_address_fails_with_a_safe_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a non-Unix manager address.
    monkeypatch.setenv("NOTIFY_SOCKET", "relative-invalid-private-address")
    # When notification is attempted.
    with pytest.raises(ChronicleError, match="service_notification_error") as raised:
        notify_service("healthy", ready=True)
    # Then the address is not reflected in the public error.
    assert "private-address" not in str(raised.value)
