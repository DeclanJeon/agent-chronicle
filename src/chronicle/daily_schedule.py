"""Durable foreground catch-up for elapsed local daily-note dates."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from chronicle.daily_cursor_store import DailyCursorStore, open_cursor_store
from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.locking import writer_lock
from chronicle.destination import verify_vault
from chronicle.models import CaptureContext, JsonValue
from chronicle.schema import parse_json_object

_CATCHUP_GRACE_MINUTES: Final = 5
_MAX_CATCHUP_DAYS: Final = 366


@dataclass(frozen=True, slots=True)
class DailyCatchupResult:
    """Observed daily catch-up work; conflicts are reported, never called reviewed."""

    status: str
    first_date: str | None
    last_date: str | None
    days_attempted: int
    conflict_count: int

    def as_json(self) -> dict[str, JsonValue]:
        """Return a structured, non-claiming summary for the foreground daemon."""
        return {
            "status": self.status,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "days_attempted": self.days_attempted,
            "conflict_count": self.conflict_count,
            "reviewed": False,
        }


def _read_last_attempted(store: DailyCursorStore) -> date | None:
    """Parse a bounded cursor already read through its pinned private directory."""
    content = store.read_text()
    if content is None:
        return None
    try:
        value = parse_json_object(content, "daily catch-up cursor")
    except ChronicleError as error:
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor is unreadable") from error
    if frozenset(value) != frozenset({"schema_version", "last_attempted_date"}):
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor fields are unsupported")
    version = value.get("schema_version")
    selected = value.get("last_attempted_date")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1 or not isinstance(selected, str):
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor is malformed")
    try:
        parsed = date.fromisoformat(selected)
    except ValueError as error:
        raise ChronicleError("daily_schedule_error", "daily catch-up date is invalid") from error
    if parsed.isoformat() != selected:
        raise ChronicleError("daily_schedule_error", "daily catch-up date is not canonical")
    return parsed


def _write_last_attempted(store: DailyCursorStore, selected: date) -> None:
    """Atomically persist one day only after its daily update attempt completes."""
    payload = json.dumps(
        {"schema_version": 1, "last_attempted_date": selected.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    store.replace(payload)


def _latest_daily_day(vault: Path) -> date | None:
    """Find the newest canonical latest-view date without traversing symlinks."""
    root = vault / "50_Daily"
    if root.is_symlink() or not root.is_dir():
        raise ChronicleError("unsafe_path", "daily note directory is missing or unsafe")
    latest: date | None = None
    for year_path in root.iterdir():
        if year_path.is_symlink() or not year_path.is_dir() or len(year_path.name) != 4 or not year_path.name.isdigit():
            continue
        for month_path in year_path.iterdir():
            if month_path.is_symlink() or not month_path.is_dir() or len(month_path.name) != 2 or not month_path.name.isdigit():
                continue
            for path in month_path.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"):
                if path.is_symlink() or not path.is_file():
                    raise ChronicleError("unsafe_path", "daily note path is not a regular file")
                try:
                    selected = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if selected.strftime("%Y/%m") != f"{year_path.name}/{month_path.name}":
                    continue
                if latest is None or selected > latest:
                    latest = selected
    return latest


def _cutoff_day(timezone_name: str, now: datetime | None) -> date:
    """Apply the same timezone/grace rule to collection and read-only diagnosis."""
    timezone = ZoneInfo(timezone_name)
    observed = now or datetime.now(timezone)
    if observed.tzinfo is None:
        raise ChronicleError("invalid_input", "daily catch-up time must include a timezone")
    local_now = observed.astimezone(timezone)
    days_ago = 1 if local_now.timetz().replace(tzinfo=None) >= time(0, _CATCHUP_GRACE_MINUTES) else 2
    return local_now.date() - timedelta(days=days_ago)


def _check_cursor_date(last_attempted: date | None, cutoff: date) -> None:
    """Reject an ahead cursor without clamping or silently resetting its history."""
    if last_attempted is not None and last_attempted > cutoff:
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor is beyond the current cutoff")


def inspect_daily_schedule(context: CaptureContext, now: datetime | None = None) -> date | None:
    """Validate stored daily state without creating folders, locks, or daily notes."""
    cutoff = _cutoff_day(context.config.timezone, now)
    with open_cursor_store(context.config.vault) as store:
        last_attempted = _read_last_attempted(store) if store is not None else None
    _check_cursor_date(last_attempted, cutoff)
    return last_attempted


def catch_up_daily_notes(context: CaptureContext, now: datetime | None = None) -> DailyCatchupResult:
    """Update elapsed local dates using one pinned cursor directory for read/write."""
    cutoff_day = _cutoff_day(context.config.timezone, now)
    vault = verify_vault(
        context.config.vault, context.config.host_id, context.config.source_instance_id,
    )
    with open_cursor_store(vault, create=True) as store:
        if store is None:
            raise ChronicleError("unsafe_path", "daily cursor directory disappeared after validation")
        last_attempted = _read_last_attempted(store)
        _check_cursor_date(last_attempted, cutoff_day)
        if last_attempted is None:
            latest = _latest_daily_day(vault)
            last_attempted = min(latest, cutoff_day - timedelta(days=1)) if latest else cutoff_day - timedelta(days=1)
        if last_attempted == cutoff_day:
            return DailyCatchupResult("current", None, None, 0, 0)
        gap_days = (cutoff_day - last_attempted).days
        if gap_days > _MAX_CATCHUP_DAYS:
            raise ChronicleError("daily_catchup_too_large", "daily catch-up exceeds the 366-day safety bound")

        first_day = last_attempted + timedelta(days=1)
        conflicts = 0
        attempted = 0
        for offset in range(gap_days):
            selected = first_day + timedelta(days=offset)
            with writer_lock(vault):
                result = update_daily(vault, selected, timezone_name=context.config.timezone)
                _write_last_attempted(store, selected)
            conflicts += int(result.conflict_copy is not None)
            attempted += 1
        status = "conflict" if conflicts else ("caught_up" if attempted > 1 else "updated")
        return DailyCatchupResult(status, first_day.isoformat(), cutoff_day.isoformat(), attempted, conflicts)
