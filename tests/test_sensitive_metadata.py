"""Fail-closed capture metadata checks and safe normal capture behavior."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from chronicle.capture_input import load_capture_input
from chronicle.errors import ChronicleError
from chronicle.event_format import read_capture
from chronicle.journal import capture_event
from chronicle.models import CaptureOptions
from chronicle.recovery import rebuild_from_markdown
from chronicle.schema import parse_json_object
from chronicle.search import SearchRequest, search_documents
from tests.support import ChronicleTestCase


_PRODUCT_ROOT = Path(__file__).resolve().parents[1]
_DOCUMENT_ID = "c22a5ab6-bfa0-4670-9e5a-01f7f4ed30fc"


class SensitiveMetadataTests(ChronicleTestCase):
    """Reject recognizable sensitive values before capture can publish them."""

    def make_capture(self, name: str = "safe.md"):
        input_path = self.write_input(name, "정상 한글 본문\n")
        options = CaptureOptions(
            input_path,
            "safe-source-event",
            "safe-session",
            "정상 한글 제목",
            "safe-project",
            "safe-author",
            "2026-11-06T12:30:00+09:00",
            _DOCUMENT_ID,
            False,
        )
        return load_capture_input(options, self.context)

    @staticmethod
    def snapshot_roots(*roots: Path) -> tuple[tuple[str, str, str], ...]:
        """Return a stable inventory of app-owned directories and file hashes."""
        entries: list[tuple[str, str, str]] = []
        for root in roots:
            root_label = root.name
            if not root.exists():
                entries.append((root_label, "missing", ""))
                continue
            entries.append((root_label, "directory", ""))
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    entries.append((root_label, f"symlink:{relative}", str(path.readlink())))
                elif path.is_dir():
                    entries.append((root_label, f"directory:{relative}", ""))
                elif path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    entries.append((root_label, f"file:{relative}", digest))
        return tuple(entries)

    def run_cli(self, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "chronicle", *arguments],
            cwd=_PRODUCT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_common_capture_boundary_rejects_sensitive_metadata_fields(self) -> None:
        request = self.make_capture()
        roots = (self.vault, self.paths.config_dir, self.paths.state_dir, self.paths.cache_dir)
        before = self.snapshot_roots(*roots)
        cases = (
            ("source_event_id", "qa-source@example.test"),
            ("source_session_id", "session_id=qa-session-boundary-secret"),
            ("title", "qa-title@example.test"),
            ("project_id", "Bearer qa-project-boundary-token"),
            ("author_name", "cookie=qa-author-boundary-secret"),
            ("source_file_name", "Set-Cookie: qa-filename-boundary-secret=value"),
        )

        for field_name, value in cases:
            with self.subTest(field=field_name):
                changes = {field_name: value}
                if field_name != "source_event_id":
                    changes["source_event_id"] = f"safe-{field_name}"
                altered = replace(request, **changes)
                with self.assertRaises(ChronicleError) as raised:
                    _ = capture_event(self.context, altered)
                self.assertEqual(raised.exception.code, "sensitive_metadata")
                self.assertNotIn(value, str(raised.exception))
                self.assertEqual(self.snapshot_roots(*roots), before)

    def test_filename_is_checked_before_slug_normalization(self) -> None:
        input_path = self.write_input("cookie=qa-source-filename-secret.md", "safe body\n")
        request = CaptureOptions(
            input_path,
            "safe-source-event",
            "safe-session",
            "safe title",
            "safe-project",
            "safe-author",
            "2026-11-06T12:30:00+09:00",
            None,
            False,
        )
        roots = (self.vault, self.paths.config_dir, self.paths.state_dir, self.paths.cache_dir)
        before = self.snapshot_roots(*roots)

        with self.assertRaises(ChronicleError) as raised:
            _ = load_capture_input(request, self.context)

        self.assertEqual(raised.exception.code, "sensitive_metadata")
        self.assertNotIn("qa-source-filename-secret", str(raised.exception))
        self.assertEqual(self.snapshot_roots(*roots), before)

    def test_cli_errors_do_not_echo_sensitive_values_or_advance_sinks(self) -> None:
        cli_home = self.root / "cli-home"
        vault = self.root / "cli-vault"
        paths = {
            "HOME": cli_home,
            "CHRONICLE_CONFIG_DIR": cli_home / "config",
            "CHRONICLE_STATE_DIR": cli_home / "state",
            "CHRONICLE_CACHE_DIR": cli_home / "cache",
        }
        cli_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update({key: str(value) for key, value in paths.items()})
        environment["PYTHONPATH"] = str(_PRODUCT_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        initialized = self.run_cli(environment, "init", "--vault", str(vault), "--timezone", "Asia/Seoul")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        safe_file = self.write_input("safe-cli.md", "safe body\n")
        filename_file = self.write_input("Cookie: qa-filename-cli-secret=value.md", "safe body\n")
        roots = (vault, paths["CHRONICLE_CONFIG_DIR"], paths["CHRONICLE_STATE_DIR"], paths["CHRONICLE_CACHE_DIR"])
        before = self.snapshot_roots(*roots)
        base = (
            "capture", "--file", str(safe_file),
            "--source-event-id", "safe-cli-event",
            "--session-id", "safe-cli-session",
            "--title", "safe title",
            "--project", "safe-project",
            "--author", "safe-author",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        )
        cases = (
            ("title-email", ("--title", "qa-metadata-person@example.test"), safe_file,
             "qa-metadata-person@example.test"),
            ("author-cookie", ("--author", "cookie=qa-metadata-cookie-secret"), safe_file,
             "qa-metadata-cookie-secret"),
            ("session-id", ("--session-id", "session_id=qa-metadata-session-secret"), safe_file,
             "qa-metadata-session-secret"),
            ("source-event-id", ("--source-event-id", "Set-Cookie: qa-event-id-secret=value"), safe_file,
             "qa-event-id-secret"),
            ("project-bearer", ("--project", "Bearer qa-project-cli-token"), safe_file,
             "qa-project-cli-token"),
            ("source-filename", (), filename_file, "qa-filename-cli-secret"),
        )
        all_sensitive_values = tuple(case[3] for case in cases)

        for label, override, input_path, secret in cases:
            with self.subTest(field=label):
                arguments = list(base)
                arguments[2] = str(input_path)
                arguments[arguments.index("--source-event-id") + 1] = f"safe-cli-event-{label}"
                arguments[arguments.index("--session-id") + 1] = f"safe-cli-session-{label}"
                for option, value in zip(override[::2], override[1::2], strict=True):
                    arguments[arguments.index(option) + 1] = value
                result = self.run_cli(environment, *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                error = parse_json_object(result.stderr, "capture error")
                self.assertEqual(error.get("error"), "sensitive_metadata")
                combined_output = result.stdout + result.stderr
                for sensitive_value in all_sensitive_values:
                    self.assertNotIn(sensitive_value, combined_output)
                self.assertEqual(self.snapshot_roots(*roots), before)
                detail = error.get("detail")
                if not isinstance(detail, str):
                    self.fail("capture error detail is not text")
                self.assertNotIn(secret, detail)

    def test_korean_metadata_ids_replay_search_and_revision_remain_valid(self) -> None:
        title = "정상 한글 제목"
        author = "작성자"
        project = "한국어 프로젝트"
        input_path = self.write_input("한글-원본.md", "정상 한글 본문\n")
        request = self.make_capture("한글-원본.md")
        request = replace(
            request,
            source_event_id="korean-source-one",
            source_session_id="세션-하나",
            title=title,
            project_id=project,
            author_name=author,
            source_file_name=input_path.name,
        )

        first = capture_event(self.context, request)
        replay = capture_event(self.context, request)
        second = capture_event(
            self.context,
            replace(
                request,
                content="다음 정상 한글 revision\n",
                source_event_id="korean-source-two",
                source_session_id="세션-둘",
                title="다음 한글 제목",
            ),
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual((first.event_id, first.revision_id), (replay.event_id, replay.revision_id))
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.revision_id, second.revision_id)
        self.assertEqual(read_capture(first.event_path)[0]["title"], title)
        self.assertEqual(read_capture(first.event_path)[0]["author_name"], author)
        self.assertEqual(read_capture(first.event_path)[0]["project_id"], project)
        self.assertEqual(read_capture(second.revision_path)[0]["document_revision"], 2)

        hits = search_documents(
            self.vault,
            SearchRequest("정상 한글", None, None, None),
            self.config.timezone,
        )
        self.assertEqual({hit.kind for hit in hits}, {"event", "artifact_revision"})
        self.assertEqual(sum(hit.kind == "event" for hit in hits), 2)
        self.assertEqual(sum(hit.kind == "artifact_revision" for hit in hits), 2)
        _ = rebuild_from_markdown(self.context)
        rebuilt_hits = search_documents(
            self.vault,
            SearchRequest("정상 한글", None, None, None),
            self.config.timezone,
        )
        self.assertEqual(rebuilt_hits, hits)


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
