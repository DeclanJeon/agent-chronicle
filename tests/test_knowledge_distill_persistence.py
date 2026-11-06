"""End-to-end tests for persisted rule candidates and durable extraction jobs."""

from pathlib import Path
from unittest.mock import patch

from chronicle.errors import ChronicleError
from chronicle.knowledge_common import preview_candidate
from chronicle.knowledge_families import family_member_ids
from chronicle.knowledge_candidates import create_candidate as create_candidate_direct
from chronicle.knowledge_models import CandidateInput
from chronicle.knowledge_job_queries import list_distillation_jobs
from chronicle.knowledge_jobs import persist_session_distillation, retry_distillation
from chronicle.models import CaptureContext, EnvironmentPaths, JsonValue
from chronicle.paths import load_config
from tests.test_cli import CliTestCase, json_array, json_object, json_text


class KnowledgeDistillPersistenceTests(CliTestCase):
    def _capture_marked_events(self, session_id: str, count: int = 2) -> None:
        _ = self.checked_json("init", "--vault", str(self.vault))
        for index in range(count):
            quote = "Keep each committed event as the exact evidence for this explicit lesson."
            source = self.root / f"marked-source-{index}.md"
            _ = source.write_text(f"## 배운 점\n\n{quote}\n", encoding="utf-8")
            _ = self.checked_json(
                "capture", "--file", str(source), "--source-event-id", f"distill-event-{index}",
                "--session-id", session_id, "--title", f"Marked source {index}",
                "--project", "distill-project", "--author", "Dex",
                "--occurred-at", "2026-11-06T12:00:00+09:00",
            )

    def _capture_document_revisions(self) -> dict[str, JsonValue]:
        document_id = "46fa2083-2887-4b7a-855f-80bcd96846ad"
        for index in range(2):
            source = self.root / f"document-revision-{index}.md"
            _ = source.write_text(f"Synthetic document revision {index}.\n", encoding="utf-8")
            arguments = [
                "capture", "--file", str(source), "--source-event-id", f"document-event-{index}",
                "--session-id", "document-lifecycle-session", "--title", "Lifecycle document",
                "--project", "distill-project", "--author", "Dex",
                "--occurred-at", f"2026-11-06T12:0{index}:00+09:00", "--document-id", document_id,
            ]
            if index == 1:
                arguments.append("--final")
            _ = self.checked_json(*arguments)
        document_report = json_object(self.checked_json("documents", "--project", "distill-project"))
        documents = json_array(document_report.get("items"))
        matches = [json_object(row) for row in documents if json_object(row).get("document_id") == document_id]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].get("revision_count"), 2)
        self.assertEqual(matches[0].get("latest_revision_number"), 2)
        self.assertEqual(matches[0].get("is_final"), True)
        return matches[0]

    def _context(self) -> CaptureContext:
        paths = EnvironmentPaths(
            Path(self.env["CHRONICLE_CONFIG_DIR"]),
            Path(self.env["CHRONICLE_STATE_DIR"]),
            Path(self.env["CHRONICLE_CACHE_DIR"]),
        )
        return CaptureContext(load_config(paths), paths)

    def test_saved_candidates_keep_overlap_family_and_approve_revoke_after_md_rebuild(self) -> None:
        self._capture_marked_events("persistent-session")
        document_before = self._capture_document_revisions()
        saved = json_object(self.checked_json(
            "knowledge", "distill", "--session", "persistent-session", "--save",
        ))
        candidate_values = saved.get("candidate_ids")
        if not isinstance(candidate_values, list):
            self.fail("persisted candidate IDs are not a list")
        candidate_ids = [json_text(value) for value in candidate_values]
        self.assertEqual(len(candidate_ids), 2)
        self.assertEqual(saved.get("status"), "completed")
        job_id = json_text(saved.get("job_id"))
        repeated_save = json_object(self.checked_json(
            "knowledge", "distill", "--session", "persistent-session", "--save",
        ))
        self.assertEqual(repeated_save.get("job_id"), job_id)
        self.assertEqual(repeated_save.get("candidate_ids"), candidate_values)
        self.assertEqual(repeated_save.get("attempt"), 1)

        first_preview = json_object(self.checked_json("knowledge", "preview", candidate_ids[0]))
        self.assertEqual(first_preview.get("title"), "배운 점 candidate")
        self.assertIn("Keep each committed event", json_text(first_preview.get("markdown_snapshot")))
        first_metadata, _snapshot, _digest = preview_candidate(self.vault, candidate_ids[0])
        second_metadata, _second_snapshot, _second_digest = preview_candidate(self.vault, candidate_ids[1])
        family_one = json_text(first_metadata.get("candidate_family_id"))
        self.assertEqual(second_metadata.get("candidate_family_id"), family_one)
        self.assertEqual(set(family_member_ids(self.vault, family_one)), set(candidate_ids))

        approved = self.checked_json(
            "knowledge", "approve", candidate_ids[0],
            "--confirm-digest", json_text(first_preview.get("approval_digest")),
            "--reviewer", "Synthetic reviewer",
        )
        self.assertEqual(json_object(approved).get("status"), "approved")
        before = json_array(self.checked_json(
            "knowledge", "search", "--query", "exact evidence", "--project", "distill-project",
        ))
        self.assertEqual([json_text(json_object(row).get("candidate_id")) for row in before], [candidate_ids[0]])

        relation = json_object(self.checked_json(
            "knowledge", "relation", "create", "--first-candidate-id", candidate_ids[0],
            "--second-candidate-id", candidate_ids[1], "--type", "related_to",
            "--reason", "Synthetic overlap-family relationship",
        ))
        relation_id = json_text(relation.get("relation_id"))
        rejected = json_object(self.checked_json(
            "knowledge", "reject", candidate_ids[1], "--reviewer", "Synthetic reviewer",
            "--reason", "This duplicate candidate is not accepted",
        ))
        self.assertEqual(rejected.get("status"), "rejected")
        _ = self.checked_json(
            "knowledge", "relation", "review", relation_id, "--decision", "confirm",
            "--reviewer", "Synthetic reviewer", "--reason", "Explicitly related test records",
        )
        relation_before = json_array(self.checked_json("knowledge", "relation", "list"))

        database = Path(self.env["CHRONICLE_STATE_DIR"]) / "projection.sqlite3"
        database.unlink(missing_ok=True)
        Path(f"{database}-wal").unlink(missing_ok=True)
        Path(f"{database}-shm").unlink(missing_ok=True)
        self.assertEqual(json_object(self.checked_json("rebuild")).get("status"), "rebuilt")
        after = json_array(self.checked_json(
            "knowledge", "search", "--query", "exact evidence", "--project", "distill-project",
        ))
        relation_after = json_array(self.checked_json("knowledge", "relation", "list"))
        document_after_report = json_object(self.checked_json("documents", "--project", "distill-project"))
        document_after_rows = json_array(document_after_report.get("items"))
        document_after = next(
            json_object(row) for row in document_after_rows
            if json_object(row).get("document_id") == document_before.get("document_id")
        )
        self.assertEqual(after, before)
        self.assertEqual(relation_after, relation_before)
        self.assertEqual(document_after, document_before)
        jobs = json_array(self.checked_json("knowledge", "jobs"))
        self.assertEqual(json_object(jobs[0]).get("job_id"), job_id)

        _ = self.checked_json(
            "knowledge", "retract", candidate_ids[0], "--reviewer", "Synthetic reviewer",
            "--reason", "Synthetic revocation regression",
        )
        self.assertEqual(self.checked_json(
            "knowledge", "search", "--query", "exact evidence", "--project", "distill-project",
        ), [])
        reapproval = self.run_cli(
            "knowledge", "approve", candidate_ids[0], "--confirm-digest",
            json_text(first_preview.get("approval_digest")), "--reviewer", "Synthetic reviewer",
        )
        self.assertEqual(reapproval.returncode, 2)
        self.assertIn("candidate_closed", reapproval.stderr)
        _ = self.checked_json("rebuild")
        self.assertEqual(self.checked_json(
            "knowledge", "search", "--query", "exact evidence", "--project", "distill-project",
        ), [])

    def test_failed_job_is_durable_and_retry_reuses_the_same_candidate_ids(self) -> None:
        self._capture_marked_events("retry-session", count=2)
        context = self._context()
        call_count = 0

        def fail_after_first(context_value: CaptureContext, request: CandidateInput) -> Path:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ChronicleError("durability_error", "synthetic publication failure")
            return create_candidate_direct(context_value, request)

        with patch("chronicle.knowledge_jobs.create_candidate", side_effect=fail_after_first):
            with self.assertRaises(ChronicleError):
                _ = persist_session_distillation(context, "retry-session")

        failed_rows = list_distillation_jobs(self.vault)
        self.assertEqual(len(failed_rows), 1)
        failed = json_object(failed_rows[0])
        self.assertEqual(failed.get("status"), "failed")
        self.assertEqual(failed.get("attempt"), 1)
        job_id = json_text(failed.get("job_id"))
        partial_ids = failed.get("candidate_ids")
        if not isinstance(partial_ids, list):
            self.fail("failed job candidate IDs are not a list")
        partial_candidate_ids = [item for item in partial_ids if isinstance(item, str)]
        self.assertEqual(len(partial_candidate_ids), 1)

        retried = json_object(self.checked_json("knowledge", "retry", job_id))
        self.assertEqual(retried.get("status"), "completed")
        self.assertEqual(retried.get("attempt"), 2)
        completed_ids = retried.get("candidate_ids")
        if not isinstance(completed_ids, list):
            self.fail("completed candidate IDs are not a list")
        completed_candidate_ids = [item for item in completed_ids if isinstance(item, str)]
        self.assertEqual(len(completed_candidate_ids), 2)
        self.assertIn(partial_candidate_ids[0], completed_candidate_ids)
        self.assertEqual(list_distillation_jobs(self.vault)[0].get("status"), "completed")

    def test_retry_refuses_a_changed_committed_input_set(self) -> None:
        self._capture_marked_events("changed-input-session", count=1)
        context = self._context()
        with patch(
            "chronicle.knowledge_jobs.create_candidate",
            side_effect=ChronicleError("durability_error", "synthetic failure"),
        ):
            with self.assertRaises(ChronicleError):
                _ = persist_session_distillation(context, "changed-input-session")
        failed = list_distillation_jobs(self.vault)[0]
        job_id = json_text(failed.get("job_id"))
        extra_source = self.root / "changed-input-extra.md"
        _ = extra_source.write_text(
            "## 배운 점\n\nA second, distinct source changes the committed extraction input.\n",
            encoding="utf-8",
        )
        _ = self.checked_json(
            "capture", "--file", str(extra_source), "--source-event-id", "changed-input-extra",
            "--session-id", "changed-input-session", "--title", "Extra marked source",
            "--project", "distill-project", "--author", "Dex",
            "--occurred-at", "2026-11-06T12:01:00+09:00",
        )
        with self.assertRaisesRegex(ChronicleError, "distill_input_changed"):
            _ = retry_distillation(context, job_id)
        self.assertEqual(list_distillation_jobs(self.vault)[0].get("status"), "failed")

    def test_pending_state_survives_interruption_and_resumes_on_retry(self) -> None:
        self._capture_marked_events("pending-session", count=1)
        context = self._context()
        with patch("chronicle.knowledge_jobs.create_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = persist_session_distillation(context, "pending-session")
        pending = list_distillation_jobs(self.vault)
        self.assertEqual(len(pending), 1)
        job_id = json_text(pending[0].get("job_id"))
        self.assertEqual(pending[0].get("status"), "pending")
        self.assertEqual(pending[0].get("attempt"), 1)

        resumed = retry_distillation(context, job_id)
        self.assertEqual(resumed.get("status"), "completed")
        self.assertEqual(resumed.get("attempt"), 1)
        resumed_ids = resumed.get("candidate_ids")
        if not isinstance(resumed_ids, list):
            self.fail("resumed candidate IDs are not a list")
        self.assertEqual(len([item for item in resumed_ids if isinstance(item, str)]), 1)
