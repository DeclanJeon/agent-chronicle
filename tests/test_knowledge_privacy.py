"""Fail-closed privacy boundaries for knowledge writes and returns."""

from pathlib import Path
import unittest
from typing import override
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from chronicle.capture_input import load_capture_input
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.knowledge_candidates import create_candidate
from chronicle.knowledge_common import decision_files, preview_candidate, read_retraction_metadata, source_event
from chronicle.knowledge_decisions import approve_candidate, retract_candidate
from chronicle.knowledge_models import (
    ApprovalInput,
    CandidateInput,
    KnowledgeSearchRequest,
    RetractionInput,
)
from chronicle.knowledge_search import search_knowledge
from chronicle.markdown import approval_digest, parse_frontmatter, render_markdown
from chronicle.models import CaptureOptions, JsonValue
from chronicle.schema import text_field
from tests.support import ChronicleTestCase


EMAIL = "qa-knowledge-person@example.test"
COOKIE = "cookie=qa-knowledge-cookie-secret"
BEARER = "Bearer qa-knowledge-creator-token"
SESSION = "session_id=qa-knowledge-quote-secret"
SENSITIVE_MARKERS = (EMAIL, "qa-knowledge-cookie-secret", "qa-knowledge-creator-token", "qa-knowledge-quote-secret")


class KnowledgePrivacyTests(ChronicleTestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.event_id: str = ""
        self.quote: str = ""
        self.candidate_path: Path = Path()
        self.candidate_id: str = ""
        self.digest: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.quote = "검토한 지식은 원본 근거와 정확히 연결된다."
        source = self.write_input("knowledge-privacy-source.md", self.quote + "\n")
        captured = load_capture_input(CaptureOptions(
            source, "knowledge-privacy-source", "synthetic-session", "안전한 출처",
            "privacy-project", "Dex", "2026-11-06T12:30:00+09:00", None, False,
        ), self.context)
        self.event_id = capture_event(self.context, captured).event_id
        self.candidate_path = create_candidate(self.context, CandidateInput(
            self.event_id, "안전한 제목", "원본 근거와 함께 검토한다.", self.quote, "Dex",
        ))
        self.candidate_id = self.candidate_path.stem
        _metadata, _snapshot, self.digest = preview_candidate(self.vault, self.candidate_id)

    def _snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _taint_candidate(self) -> tuple[bytes, dict[str, JsonValue]]:
        metadata, body, _normalized = parse_frontmatter(self.candidate_path.read_bytes())
        metadata["title"] = EMAIL
        metadata["claim"] = COOKIE
        metadata["created_by"] = BEARER
        encoded = render_markdown(metadata, body).encode("utf-8")
        _ = self.candidate_path.write_bytes(encoded)
        return encoded, metadata

    def _install_matching_approval(self, raw: bytes, metadata: dict[str, JsonValue]) -> Path:
        digest = approval_digest(raw, 1)
        decision_id = str(uuid5(NAMESPACE_URL, f"tainted-approval:{self.candidate_id}"))
        created_at = "2026-11-06T12:30:00+00:00"
        decision_metadata: dict[str, JsonValue] = {
            "schema_version": 1,
            "kind": "knowledge_decision",
            "decision_id": decision_id,
            "previous_decision_id": None,
            "decision": "approve",
            "document_id": text_field(metadata, "document_id"),
            "revision_id": text_field(metadata, "revision_id"),
            "approval_digest_version": 1,
            "approval_digest": digest,
            "reviewer": "Synthetic Reviewer",
            "created_at": created_at,
            "reviewed_at": created_at,
            "review_due_at": "2099-11-06T12:30:00+00:00",
            "reason": "synthetic approval fixture",
            "scope": text_field(metadata, "scope"),
            "allowed_contexts": metadata.get("allowed_contexts", []),
        }
        path = self.vault / "90_System/KnowledgeDecisions" / self.candidate_id / f"2026-11-06_{decision_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(render_markdown(decision_metadata, "# Synthetic approval\n"), encoding="utf-8")
        return path

    def test_candidate_free_text_is_rejected_before_identity_or_write(self) -> None:
        requests = (
            ("title", CandidateInput(self.event_id, EMAIL, "safe claim", self.quote, "Dex")),
            ("claim", CandidateInput(self.event_id, "safe title", COOKIE, self.quote, "Dex")),
            ("created_by", CandidateInput(self.event_id, "safe title", "safe claim", self.quote, BEARER)),
            ("quote", CandidateInput(self.event_id, "safe title", "safe claim", SESSION, "Dex")),
        )
        for field, request in requests:
            with self.subTest(field=field):
                before = self._snapshot()
                with self.assertRaises(ChronicleError) as raised:
                    _ = create_candidate(self.context, request)
                self.assertEqual(raised.exception.code, "sensitive_knowledge")
                for marker in SENSITIVE_MARKERS:
                    self.assertNotIn(marker, str(raised.exception))
                self.assertEqual(before, self._snapshot())

    def test_candidate_publication_rejects_sensitive_legacy_source_payload(self) -> None:
        source_path, metadata, content = source_event(self.vault, self.event_id)
        legacy_metadata = {**metadata, "project_id": EMAIL}
        before = self._snapshot()
        request = CandidateInput(
            self.event_id,
            "safe legacy candidate",
            "safe legacy claim",
            self.quote,
            "Safe Creator",
        )
        with patch(
            "chronicle.knowledge_candidates.source_event",
            return_value=(source_path, legacy_metadata, content),
        ):
            with self.assertRaises(ChronicleError) as raised:
                _ = create_candidate(self.context, request)
        self.assertEqual(raised.exception.code, "sensitive_knowledge")
        self.assertNotIn(EMAIL, str(raised.exception))
        self.assertEqual(before, self._snapshot())

    def test_approval_reviewer_is_rejected_before_decision_write(self) -> None:
        before = self._snapshot()
        with self.assertRaises(ChronicleError) as raised:
            _ = approve_candidate(self.context, ApprovalInput(self.candidate_id, self.digest, EMAIL))
        self.assertEqual(raised.exception.code, "sensitive_knowledge")
        self.assertNotIn(EMAIL, str(raised.exception))
        self.assertEqual(before, self._snapshot())

    def test_retraction_reviewer_and_reason_are_rejected_before_decision_write(self) -> None:
        for field, reviewer, reason, marker in (
            ("reviewer", COOKIE, "safe reason", "qa-knowledge-cookie-secret"),
            ("reason", "Safe Reviewer", SESSION, "qa-knowledge-quote-secret"),
        ):
            with self.subTest(field=field):
                before = self._snapshot()
                with self.assertRaises(ChronicleError) as raised:
                    _ = retract_candidate(self.context, RetractionInput(self.candidate_id, reviewer, reason))
                self.assertEqual(raised.exception.code, "sensitive_knowledge")
                self.assertNotIn(marker, str(raised.exception))
                self.assertEqual(before, self._snapshot())

    def test_sensitive_preview_is_blocked_without_rewriting_tainted_candidate(self) -> None:
        tainted, _metadata = self._taint_candidate()
        before = self._snapshot()
        with self.assertRaises(ChronicleError) as raised:
            _ = preview_candidate(self.vault, self.candidate_id)
        self.assertEqual(raised.exception.code, "sensitive_knowledge")
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, str(raised.exception))
        self.assertEqual(before, self._snapshot())
        self.assertEqual(self.candidate_path.read_bytes(), tainted)

    def test_tainted_candidate_is_not_returned_from_knowledge_search(self) -> None:
        raw, metadata = self._taint_candidate()
        _ = self._install_matching_approval(raw, metadata)
        before = self._snapshot()
        results = search_knowledge(
            self.vault, KnowledgeSearchRequest("qa-knowledge", "personal", "privacy-project"),
        )
        self.assertEqual(results, [])
        self.assertEqual(before, self._snapshot())

    def test_sensitive_search_query_is_rejected_without_state_change(self) -> None:
        before = self._snapshot()
        with self.assertRaises(ChronicleError) as raised:
            _ = search_knowledge(self.vault, KnowledgeSearchRequest(COOKIE, "personal", "privacy-project"))
        self.assertEqual(raised.exception.code, "sensitive_knowledge")
        self.assertNotIn("qa-knowledge-cookie-secret", str(raised.exception))
        self.assertEqual(before, self._snapshot())

    def test_tainted_candidate_preview_and_approval_fail_but_safe_retraction_preserves_bytes(self) -> None:
        tainted, _metadata = self._taint_candidate()
        before = self._snapshot()
        with self.assertRaises(ChronicleError) as preview_error:
            _ = preview_candidate(self.vault, self.candidate_id)
        self.assertEqual(preview_error.exception.code, "sensitive_knowledge")
        with self.assertRaises(ChronicleError) as approval_error:
            _ = approve_candidate(self.context, ApprovalInput(self.candidate_id, approval_digest(tainted, 1), "Safe Reviewer"))
        self.assertEqual(approval_error.exception.code, "sensitive_knowledge")
        self.assertEqual(before, self._snapshot())

        decision_path = retract_candidate(
            self.context, RetractionInput(self.candidate_id, "Safe Reviewer", "safe removal request"),
        )
        self.assertEqual(self.candidate_path.read_bytes(), tainted)
        decision_text = decision_path.read_text(encoding="utf-8")
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, decision_text)
        self.assertEqual(
            search_knowledge(self.vault, KnowledgeSearchRequest("qa-knowledge", "personal", "privacy-project")),
            [],
        )

    def test_safe_retraction_can_extend_tainted_decision_history_without_rewriting_it(self) -> None:
        _ = approve_candidate(self.context, ApprovalInput(self.candidate_id, self.digest, "Safe Reviewer"))
        first = decision_files(self.vault, self.candidate_id)[0][0]
        metadata, body, _normalized = parse_frontmatter(first.read_bytes())
        metadata["reviewer"] = EMAIL
        metadata["reason"] = COOKIE
        _ = first.write_text(render_markdown(metadata, body), encoding="utf-8")
        previous_bytes = first.read_bytes()
        candidate_bytes = self.candidate_path.read_bytes()
        retraction_metadata, _digest = read_retraction_metadata(self.vault, self.candidate_id)
        self.assertEqual(
            set(retraction_metadata),
            {"document_id", "revision_id", "approval_digest_version", "scope", "allowed_contexts"},
        )
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, str(retraction_metadata))

        second = retract_candidate(
            self.context, RetractionInput(self.candidate_id, "Safe Reviewer", "safe retraction"),
        )
        self.assertEqual(first.read_bytes(), previous_bytes)
        self.assertEqual(self.candidate_path.read_bytes(), candidate_bytes)
        second_text = second.read_text(encoding="utf-8")
        for marker in SENSITIVE_MARKERS:
            self.assertNotIn(marker, second_text)
        history = decision_files(self.vault, self.candidate_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(text_field(history[-1][1], "decision"), "retract")
        self.assertEqual(
            search_knowledge(self.vault, KnowledgeSearchRequest("검토한", "personal", "privacy-project")),
            [],
        )

    def test_redacted_source_quote_remains_previewable_approvable_and_retractable(self) -> None:
        source = self.write_input("redacted-knowledge-source.md", "정상 근거\ncookie=synthetic-redaction-secret\n")
        captured = load_capture_input(CaptureOptions(
            source, "redacted-knowledge-source", "synthetic-session-2", "정상 출처",
            "privacy-project", "Dex", "2026-11-06T12:31:00+09:00", None, False,
        ), self.context)
        event_id = capture_event(self.context, captured).event_id
        candidate = create_candidate(self.context, CandidateInput(
            event_id, "정상 가림 후보", "가려진 안전 인용을 검토한다.", "cookie=[REDACTED]", "Dex",
        ))
        metadata, snapshot, digest = preview_candidate(self.vault, candidate.stem)
        self.assertEqual(digest, approval_digest(candidate.read_bytes(), 1))
        self.assertIn("cookie=[REDACTED]", snapshot)
        _ = approve_candidate(self.context, ApprovalInput(candidate.stem, digest, "Safe Reviewer"))
        request = KnowledgeSearchRequest("[REDACTED]", "personal", "privacy-project")
        results = search_knowledge(self.vault, request)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].quote, "cookie=[REDACTED]")
        _ = retract_candidate(self.context, RetractionInput(candidate.stem, "Safe Reviewer", "safe retraction"))
        self.assertEqual(search_knowledge(self.vault, request), [])
        self.assertEqual(text_field(metadata, "review_status"), "candidate")


if __name__ == "__main__":
    _ = unittest.main()
