"""Per-installation paths and immutable local identity."""

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

from chronicle.atomic import atomic_replace, atomic_write_new
from chronicle.errors import ChronicleError
from chronicle.models import ChronicleConfig, EnvironmentPaths
from chronicle.schema import parse_json_object


def environment_paths() -> EnvironmentPaths:
    """Resolve roots without creating or reading any user data."""
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    return EnvironmentPaths(
        config_dir=Path(os.environ.get("CHRONICLE_CONFIG_DIR", config_home / "agent-chronicle")).expanduser().absolute(),
        state_dir=Path(os.environ.get("CHRONICLE_STATE_DIR", state_home / "agent-chronicle")).expanduser().absolute(),
        cache_dir=Path(os.environ.get("CHRONICLE_CACHE_DIR", cache_home / "agent-chronicle")).expanduser().absolute(),
    )


def validate_store_roots(paths: EnvironmentPaths) -> None:
    """Require config, state, and cache roots to be canonical and disjoint."""
    roots = (paths.config_dir.absolute(), paths.state_dir.absolute(), paths.cache_dir.absolute())
    for root in roots:
        try:
            if root.resolve(strict=False) != root:
                raise ChronicleError("unsafe_store_layout", "configuration/state/cache roots may not traverse symlinks")
        except (OSError, RuntimeError) as error:
            raise ChronicleError("unsafe_store_layout", "configuration/state/cache paths cannot be resolved") from error
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise ChronicleError("unsafe_store_layout", "configuration, state, and cache roots must be disjoint")


def validate_store_layout(vault: Path, paths: EnvironmentPaths) -> None:
    """Keep all operational stores physically separate from the selected vault."""
    validate_store_roots(paths)
    absolute_vault = vault.absolute()
    protected_roots = (paths.config_dir.absolute(), paths.state_dir.absolute(), paths.cache_dir.absolute())
    if any(
        absolute_vault == root
        or absolute_vault in root.parents
        or root in absolute_vault.parents
        for root in protected_roots
    ):
        raise ChronicleError("unsafe_store_layout", "vault and configuration/state/cache roots must be separate")


def _validate_config_file(path: Path) -> None:
    """Require a regular user-owned config file with no group/world access."""
    if path.is_symlink() or path.parent.resolve(strict=False) != path.parent.absolute() or not path.is_file():
        raise ChronicleError("unsafe_config", "configuration must be a regular file")
    file_stat = path.stat()
    if file_stat.st_uid != os.getuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ChronicleError("unsafe_config", "configuration owner or permissions are unsafe")


def _validate_timezone(value: object) -> str:
    """Parse one configured timezone as an available IANA ZoneInfo key."""
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ChronicleError(
            "invalid_timezone", "timezone must be a non-empty IANA timezone name"
        )
    try:
        zone = ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ChronicleError(
            "invalid_timezone", "timezone must name an installed IANA timezone"
        ) from error
    return zone.key


def _zone_name_from_path(path: Path) -> str | None:
    """Map a system zoneinfo path back to its relative IANA key."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for root in TZPATH:
        try:
            relative = resolved.relative_to(Path(root).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        candidate = relative.as_posix()
        try:
            return _validate_timezone(candidate)
        except ChronicleError:
            continue
    return None


def _system_timezone_name() -> str:
    """Resolve the current process or Linux system timezone to an IANA key."""
    environment_timezone = os.environ.get("TZ")
    if environment_timezone:
        candidate = environment_timezone.removeprefix(":")
        if Path(candidate).is_absolute():
            resolved_name = _zone_name_from_path(Path(candidate))
            if resolved_name is None:
                raise ChronicleError(
                    "timezone_unavailable",
                    "TZ does not point to an installed zoneinfo file",
                )
            return resolved_name
        return _validate_timezone(candidate)

    timezone_file = Path("/etc/timezone")
    try:
        candidate = timezone_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        candidate = ""
    if candidate:
        try:
            return _validate_timezone(candidate)
        except ChronicleError:
            pass

    localtime_name = _zone_name_from_path(Path("/etc/localtime"))
    if localtime_name is not None:
        return localtime_name
    raise ChronicleError(
        "timezone_unavailable",
        "system timezone is not an IANA zone; pass --timezone",
    )


def _validate_config_identity(
    value: dict[str, object],
) -> tuple[str, str, str, str | None]:
    """Validate persisted schema, stable IDs, and any pinned timezone."""
    version = value.get("schema_version")
    vault = value.get("vault")
    host_id = value.get("host_id")
    source_instance = value.get("source_instance_id")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in (1, 2)
        or not isinstance(vault, str)
        or not Path(vault).is_absolute()
        or not isinstance(host_id, str)
        or not isinstance(source_instance, str)
    ):
        raise ChronicleError(
            "invalid_config", "configuration fields or schema version are invalid"
        )
    try:
        parsed_source_id = str(uuid.UUID(source_instance))
    except ValueError as error:
        raise ChronicleError("invalid_config", "source instance ID is invalid") from error
    if parsed_source_id != source_instance:
        raise ChronicleError("invalid_config", "source instance ID must use canonical UUID form")
    if version == 1:
        if "timezone" in value:
            raise ChronicleError(
                "invalid_config", "legacy configuration may not contain timezone"
            )
        timezone = None
    else:
        try:
            timezone = _validate_timezone(value.get("timezone"))
        except ChronicleError as error:
            raise ChronicleError(
                "invalid_config", "configuration timezone is invalid"
            ) from error
    return vault, host_id, source_instance, timezone


def _serialize_config(config: ChronicleConfig) -> bytes:
    """Render the current private config schema deterministically."""
    return json.dumps(
        {
            "schema_version": 2,
            "vault": str(config.vault),
            "host_id": config.host_id,
            "source_instance_id": config.source_instance_id,
            "timezone": config.timezone,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def current_host_id() -> str:
    """Return a stable, non-reversible identifier derived from Linux machine-id."""
    machine_id = Path("/etc/machine-id")
    try:
        raw = machine_id.read_text(encoding="ascii").strip()
    except OSError as error:
        raise ChronicleError("unsupported_host", "stable Linux host identity is unavailable") from error
    if len(raw) < 16:
        raise ChronicleError("unsupported_host", "stable Linux host identity is invalid")
    return hashlib.sha256(f"agent-chronicle/host/v1\0{raw}".encode()).hexdigest()


def load_or_create_config(
    vault: Path,
    paths: EnvironmentPaths,
    timezone_name: str | None = None,
) -> ChronicleConfig:
    """Read or create a stable installation identity and pin its timezone once."""
    config_path = paths.config_dir / "config.json"
    host_id = current_host_id()
    requested_timezone = (
        _validate_timezone(timezone_name) if timezone_name is not None else None
    )
    if config_path.exists():
        _validate_config_file(config_path)
        try:
            value = parse_json_object(
                config_path.read_text(encoding="utf-8"), "configuration"
            )
        except (OSError, UnicodeDecodeError, ChronicleError) as error:
            raise ChronicleError("invalid_config", "configuration is unreadable") from error
        configured_vault, configured_host, source_instance, saved_timezone = (
            _validate_config_identity(value)
        )
        if configured_host != host_id:
            raise ChronicleError("host_mismatch", "configuration belongs to a different host")
        if Path(configured_vault) != vault:
            raise ChronicleError("vault_mismatch", "selected vault differs from saved configuration")
        if saved_timezone is not None:
            if requested_timezone is not None and requested_timezone != saved_timezone:
                raise ChronicleError(
                "timezone_mismatch",
                "timezone differs from the fixed installation timezone",
            )
            return ChronicleConfig(vault, host_id, source_instance, saved_timezone)

        selected_timezone = requested_timezone or _system_timezone_name()
        config = ChronicleConfig(vault, host_id, source_instance, selected_timezone)
        atomic_replace(config_path, _serialize_config(config))
        return config

    if (vault / ".chronicle/manifest.json").exists():
        raise ChronicleError(
            "missing_identity", "vault exists but its installation identity is missing"
        )
    selected_timezone = requested_timezone or _system_timezone_name()
    config = ChronicleConfig(vault, host_id, str(uuid.uuid4()), selected_timezone)
    atomic_write_new(config_path, _serialize_config(config))
    return config


def load_config(paths: EnvironmentPaths) -> ChronicleConfig:
    """Load configuration for commands that operate on an initialized vault."""
    config_path = paths.config_dir / "config.json"
    if not config_path.exists():
        raise ChronicleError("not_initialized", "run chronicle init --vault PATH first")
    _validate_config_file(config_path)
    try:
        value = parse_json_object(config_path.read_text(encoding="utf-8"), "configuration")
    except (OSError, UnicodeDecodeError, ChronicleError) as error:
        raise ChronicleError("invalid_config", "configuration is unreadable") from error
    configured_vault, host_id, source_instance, timezone_name = (
        _validate_config_identity(value)
    )
    if host_id != current_host_id():
        raise ChronicleError("host_mismatch", "configuration belongs to a different host")
    if timezone_name is None:
        raise ChronicleError(
            "timezone_missing",
            "run chronicle init --vault PATH to pin the installation timezone",
        )
    return ChronicleConfig(Path(configured_vault), host_id, source_instance, timezone_name)
