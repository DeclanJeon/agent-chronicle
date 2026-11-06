"""Synthetic case-folded project ID and alias collision regressions."""

from tests.test_cli import CliTestCase, json_array, json_object


class ProjectAliasCollisionCliTests(CliTestCase):
    def capture_project(self, project_id: str, source_event_id: str) -> None:
        """Create one committed event with an explicitly stable project ID."""
        source = self.root / f"{source_event_id}.md"
        _ = source.write_text(f"synthetic evidence for {project_id}\n", encoding="utf-8")
        result = self.run_cli(
            "capture", "--file", str(source), "--source-event-id", source_event_id,
            "--session-id", "alias-session", "--title", f"Project {project_id}",
            "--project", project_id, "--author", "Safe",
            "--occurred-at", "2026-11-06T12:30:00+09:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uppercase_alias_cannot_shadow_lowercase_stable_id(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        self.capture_project("alpha", "alias-alpha-lower")
        self.capture_project("beta", "alias-beta-lower")

        result = self.run_cli(
            "project", "alias", "add", "--project-id", "beta", "--alias", "ALPHA",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ambiguous_project_alias", result.stderr)
        self.assertEqual(json_array(self.checked_json("project", "alias", "list")), [])
        canonical = json_object(self.checked_json("projects", "--project", "alpha"))
        self.assertEqual(len(json_array(canonical["items"])), 1)

    def test_lowercase_alias_cannot_shadow_uppercase_stable_id(self) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        self.capture_project("ALPHA", "alias-alpha-upper")
        self.capture_project("beta", "alias-beta-upper")

        result = self.run_cli(
            "project", "alias", "add", "--project-id", "beta", "--alias", "alpha",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ambiguous_project_alias", result.stderr)
        self.assertEqual(json_array(self.checked_json("project", "alias", "list")), [])
        canonical = json_object(self.checked_json("projects", "--project", "ALPHA"))
        self.assertEqual(len(json_array(canonical["items"])), 1)
