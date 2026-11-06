"""Durable foreground catch-up for elapsed local daily-note dates."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

from chronicle.atomic import atomic_replace, atomic_write_new, ensure_private_dir, require_private_directory
from chronicle.daily import update_daily
from chronicle.errors import ChronicleError
from chronicle.locking import writer_lock
from chronicle.destination import verify_vault
from chronicle.models import CaptureContext, JsonValue
from chronicle.schema import parse_json_object

_CATCHUP_GRACE_MINUTES = 5
_MAX_CATCHUP_DAYS = 366


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


def _state_path(vault: Path) -> Path:
    """Return the single-host local daily catch-up cursor path."""
    return vault / "90_System/Schedules/daily-catchup.json"


def _read_last_attempted(path: Path) -> date | None:
    """Read one private, schema-pinned catch-up cursor or fail closed."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor cannot be inspected") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > 4096
    ):
        raise ChronicleError("unsafe_permissions", "daily catch-up cursor is unsafe")
    try:
        value = parse_json_object(path.read_text(encoding="utf-8"), "daily catch-up cursor")
    except (OSError, UnicodeDecodeError, ChronicleError) as error:
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


def _write_last_attempted(path: Path, selected: date) -> None:
    """Atomically persist one day only after its daily update attempt completes."""
    payload = json.dumps(
        {"schema_version": 1, "last_attempted_date": selected.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if path.exists():
        if path.is_symlink():
            raise ChronicleError("unsafe_path", "daily catch-up cursor may not be a symbolic link")
        atomic_replace(path, payload)
    else:
        atomic_write_new(path, payload)


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


def catch_up_daily_notes(context: CaptureContext, now: datetime | None = None) -> DailyCatchupResult:
    """Update elapsed local dates through the five-minute daily-summary cutoff."""
    timezone = ZoneInfo(context.config.timezone)
    observed = now or datetime.now(timezone)
    if observed.tzinfo is None:
        raise ChronicleError("invalid_input", "daily catch-up time must include a timezone")
    local_now = observed.astimezone(timezone)
    days_ago = 1 if local_now.timetz().replace(tzinfo=None) >= time(0, _CATCHUP_GRACE_MINUTES) else 2
    cutoff_day = local_now.date() - timedelta(days=days_ago)
    vault = verify_vault(
        context.config.vault, context.config.host_id, context.config.source_instance_id,
    )
    schedule_dir = _state_path(vault).parent
    ensure_private_dir(schedule_dir)
    require_private_directory(schedule_dir)
    cursor_path = _state_path(vault)
    last_attempted = _read_last_attempted(cursor_path)
    if last_attempted is not None and last_attempted > cutoff_day:
        raise ChronicleError("daily_schedule_error", "daily catch-up cursor is beyond the current cutoff")
    if last_attempted is None:
        latest = _latest_daily_day(vault)
        last_attempted = min(latest, cutoff_day - timedelta(days=1)) if latest else cutoff_day - timedelta(days=1)
    if last_attempted >= cutoff_day:
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
        conflicts += int(result.conflict_copy is not None)
        _write_last_attempted(cursor_path, selected)
        attempted += 1
    status = "conflict" if conflicts else ("caught_up" if attempted > 1 else "updated")
    return DailyCatchupResult(status, first_day.isoformat(), cutoff_day.isoformat(), attempted, conflicts)
