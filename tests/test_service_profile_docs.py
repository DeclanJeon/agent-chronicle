"""Execute documented fresh-shell exports against the real CLI and a fake manager."""

from collections.abc import Generator, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Final
from unittest.mock import patch

import pytest

from chronicle.service_unit import render_user_unit
from tests.test_cli import decode_json, json_object


PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class InstalledProfile:
    """Private selected profile, unit, and executable systemctl boundary recorder."""

    root: Path
    environment: Mapping[str, str]
    unit: Path
    calls: Path


@pytest.fixture
def installed_profile() -> Generator[InstalledProfile, None, None]:
    """Keep every file and manager invocation inside one disposable fixture."""
    with tempfile.TemporaryDirectory(prefix="chronicle-profile-", dir=Path.home()) as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        binary = root / "bin"
        binary.mkdir(mode=0o700)
        calls = root / "manager-calls"
        manager = binary / "systemctl"
        _ = manager.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CHRONICLE_TEST_CALLS"\n'
            + 'if [ "$2" = show ]; then\n'
            + "  printf 'ActiveState=active\\nUnitFileState=enabled\\nMainPID=1234\\n'\n"
            + "fi\n",
            encoding="utf-8",
        )
        manager.chmod(0o700)
        environment = {
            "HOME": str(home), "PATH": f"{binary}:/usr/bin:/bin",
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1", "CHRONICLE_TEST_CALLS": str(calls),
            "CHRONICLE_CONFIG_DIR": str(root / "선택 config"),
            "CHRONICLE_STATE_DIR": str(root / "선택 state"),
            "CHRONICLE_CACHE_DIR": str(root / "선택 cache"),
        }
        (root / "input").mkdir()
        for arguments in (
            ["init", "--vault", str(root / "vault")],
            ["sources", "enable", "--source", "pi-jsonl-v3", "--root", str(root / "input"), "--project", "synthetic"],
        ):
            _ = subprocess.run(
                [sys.executable, "-m", "chronicle", *arguments], env=environment,
                capture_output=True, text=True, timeout=20, check=True,
            )
        unit = home / ".config/systemd/user/agent-chronicle.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        with patch.dict(os.environ, environment, clear=True):
            _ = unit.write_text(render_user_unit(), encoding="utf-8")
        unit.chmod(0o600)
        yield InstalledProfile(root, MappingProxyType(environment), unit, calls)


def _management_setup(document: Path) -> str:
    """Use local instructions when present; otherwise follow the README's guide link."""
    text = document.read_text(encoding="utf-8")
    if document.samefile(PROJECT_ROOT / "README.md"):
        section = text.split("## 상주형 설치:", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
        local_commands = re.search(r"\bchronicle\s+service\s+(?:install|restart|stop|remove)\b", section)
        if local_commands is None:
            guide = re.search(r"\]\((docs/[^)#]+)(?:#[^)]*)?\)", section)
            assert guide is not None, "README management route has no local guide target"
            target = (PROJECT_ROOT / guide.group(1)).resolve()
            assert target.is_relative_to(PROJECT_ROOT)
            return _management_setup(target)
    else:
        section = text.split("## 3.", maxsplit=1)[1].split("## 4.", maxsplit=1)[0]
    exports = re.findall(r"(?:^|`)(export [^\n`]+)", section, flags=re.MULTILINE)
    return "\n".join(exports)


def _fresh_management(profile: InstalledProfile, action: str, *, setup: str | None = None) -> subprocess.CompletedProcess[str]:
    """Execute actual documented exports with fixture paths, never the user's manager."""
    recipe = _management_setup(PROJECT_ROOT / "docs/resident-service.md") if setup is None else setup
    script = "set -eu\ncd " + shlex.quote(str(PROJECT_ROOT)) + "\n" + recipe
    replacements = {
        "/설치당시/venv/bin/python": sys.executable,
        "/설치당시/config": profile.environment["CHRONICLE_CONFIG_DIR"],
        "/설치당시/state": profile.environment["CHRONICLE_STATE_DIR"],
        "/설치당시/cache": profile.environment["CHRONICLE_CACHE_DIR"],
    }
    for placeholder, value in replacements.items():
        script = script.replace(placeholder, value)
    script += '\nexec "${CHRONICLE_PYTHON:-python3}" -m chronicle service ' + shlex.quote(action) + " --confirm"
    fresh = {
        key: value for key, value in profile.environment.items()
        if not key.startswith("CHRONICLE_") and key != "PYTHONPATH"
    }
    fresh["CHRONICLE_TEST_CALLS"] = str(profile.calls)
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script], env=fresh,
        capture_output=True, text=True, timeout=20, check=False,
    )


@pytest.mark.parametrize("entry", ["docs/resident-service.md", "README.md"])
@pytest.mark.parametrize("action", ["restart", "stop", "remove", "install"])
def test_documented_fresh_terminal_manages_the_selected_profile(installed_profile: InstalledProfile, action: str, entry: str) -> None:
    # Given a custom-path installation and a fresh shell without its three roots.
    # When the documentation's executable exports precede the real CLI action.
    result = _fresh_management(installed_profile, action, setup=_management_setup(PROJECT_ROOT / entry))
    # Then the selected unit is managed successfully, not rejected as foreign.
    assert result.returncode == 0, result.stdout + result.stderr
    report = json_object(decode_json(result.stdout))
    expected = {"restart": "restarted", "stop": "stopped", "remove": "removed", "install": "enabled"}
    assert report["status"] == expected[action]
    assert installed_profile.calls.is_file()


@pytest.mark.parametrize("action", ["restart", "stop", "remove"])
def test_missing_profile_roots_refuse_before_systemctl(installed_profile: InstalledProfile, action: str) -> None:
    # Given the same unit but intentionally omitted profile roots.
    before = installed_profile.unit.read_bytes()
    # When a fresh CLI tries to control the unit without the required exports.
    setup = _management_setup(PROJECT_ROOT / "docs/resident-service.md")
    result = _fresh_management(
        installed_profile, action,
        setup=setup + "\nunset CHRONICLE_CONFIG_DIR CHRONICLE_STATE_DIR CHRONICLE_CACHE_DIR",
    )
    # Then the ownership guard still refuses without contacting any manager.
    assert result.returncode == 2
    assert "service_unit_conflict" in result.stdout + result.stderr
    assert not installed_profile.calls.exists()
    assert installed_profile.unit.read_bytes() == before


@pytest.mark.parametrize("action", ["restart", "stop", "remove"])
def test_foreign_unit_is_refused_even_with_documented_profile(installed_profile: InstalledProfile, action: str) -> None:
    # Given an unrelated unit, despite otherwise correct documented exports.
    foreign = b"[Service]\nExecStart=/unrelated\n"
    _ = installed_profile.unit.write_bytes(foreign)
    # When the real CLI receives the documented management environment.
    result = _fresh_management(installed_profile, action)
    # Then it cannot use the recipe to control or replace the foreign unit.
    assert result.returncode == 2
    assert "service_unit_conflict" in result.stdout + result.stderr
    assert not installed_profile.calls.exists()
    assert installed_profile.unit.read_bytes() == foreign
