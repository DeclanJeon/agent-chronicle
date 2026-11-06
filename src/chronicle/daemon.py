"""Foreground-only collection lifecycle; no installation or detachment."""

import json
import signal
import threading
from types import FrameType
from typing import TextIO

from chronicle.collector import collect_once_locked
from chronicle.daily_schedule import catch_up_daily_notes
from chronicle.errors import ChronicleError
from chronicle.destination import verify_vault
from chronicle.locking import collector_lock, writer_lock
from chronicle.models import CaptureContext, JsonValue
from chronicle.recovery import rebuild_from_markdown


def _maintenance(context: CaptureContext) -> dict[str, JsonValue]:
    """Catch up basic daily notes without invoking knowledge or weekly-review paths."""
    try:
        return {"daily_catchup": catch_up_daily_notes(context).as_json()}
    except ChronicleError as error:
        return {"daily_catchup": {"status": "failed", "error": error.code, "reviewed": False}}


def run_foreground(
    context: CaptureContext,
    output: TextIO,
    interval_seconds: float = 1.0,
) -> int:
    """Hold the collector lock, reconcile sources periodically, and stop on SIGTERM."""
    vault = verify_vault(
        context.config.vault, context.config.host_id, context.config.source_instance_id
    )
    stopped = threading.Event()

    def stop(_signum: int, _frame: FrameType | None) -> None:
        stopped.set()

    with collector_lock(context.paths.state_dir):
        with writer_lock(vault):
            _recovery = rebuild_from_markdown(context)
        first = collect_once_locked(context)
        maintenance = _maintenance(context)
        previous_term = signal.signal(signal.SIGTERM, stop)
        previous_interrupt = signal.signal(signal.SIGINT, stop)
        try:
            _ = output.write(json.dumps({
                "status": "ready",
                "collector_status": first.status,
                "vault": str(vault),
                "summary": first.as_json(),
                "maintenance": maintenance,
            }, sort_keys=True) + "\n")
            output.flush()
            while not stopped.wait(interval_seconds):
                summary = collect_once_locked(context)
                maintenance = _maintenance(context)
                report = summary.as_json()
                report["maintenance"] = maintenance
                _ = output.write(json.dumps(report, sort_keys=True) + "\n")
                output.flush()
        finally:
            _ = signal.signal(signal.SIGTERM, previous_term)
            _ = signal.signal(signal.SIGINT, previous_interrupt)
    _ = output.write(json.dumps({"status": "stopped"}, sort_keys=True) + "\n")
    output.flush()
    return 0
