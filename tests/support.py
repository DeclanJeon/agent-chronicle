"""Isolated Linux-only synthetic workspace helpers for unittest fixtures."""

from pathlib import Path
import tempfile
import unittest
from typing import override

from chronicle.destination import initialize_vault, validate_destination
from chronicle.models import CaptureContext, ChronicleConfig, EnvironmentPaths
from chronicle.paths import load_or_create_config


class ChronicleTestCase(unittest.TestCase):
    """Each test owns a private temporary root on the home local filesystem."""

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.paths: EnvironmentPaths = EnvironmentPaths(Path(), Path(), Path())
        self.vault: Path = Path()
        self.config: ChronicleConfig = ChronicleConfig(Path(), "", "", "Asia/Seoul")
        self.context: CaptureContext = CaptureContext(self.config, self.paths)

    @override
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="chronicle-test-", dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = EnvironmentPaths(
            self.root / "config", self.root / "state", self.root / "cache"
        )
        self.vault = validate_destination(self.root / "vault")
        self.config = load_or_create_config(self.vault, self.paths, "Asia/Seoul")
        _ = initialize_vault(self.vault, self.config.host_id, self.config.source_instance_id)
        self.context = CaptureContext(self.config, self.paths)

    def write_input(self, name: str, content: str) -> Path:
        """Create one synthetic input outside all Chronicle data roots."""
        path = self.root / name
        _ = path.write_text(content, encoding="utf-8")
        return path
