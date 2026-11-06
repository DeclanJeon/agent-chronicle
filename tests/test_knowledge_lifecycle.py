"""Lifecycle checks for immutable candidates and explicit relationship records."""

from pathlib import Path
from typing import override

from chronicle.capture_input import load_capture_input
from chronicle.errors import ChronicleError
from chronicle.journal import capture_event
from chronicle.knowledge_candidates import create_candidate
from chronicle.knowledge_common import decision_files, preview_candidate
from chronicle.knowledge_decisions import approve_candidate, reject_candidate
from chronicle.knowledge_models import (
    ApprovalInput, CandidateInput, KnowledgeSearchRequest, RelationInput,
    RelationReviewInput, RejectionInput,
)
from chronicle.knowledge_relations import create_relation, list_relations, review_relation
from chronicle.knowledge_search import search_knowledge
from chronicle.markdown import parse_frontmatter, render_markdown
from chronicle.models import CaptureOptions
from chronicle.schema import text_field
from tests.support import ChronicleTestCase
from tests.test_cli import json_text


class KnowledgeLifecycleTests(ChronicleTestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.quote: str = ""
        self.event_id: str = ""
        self.candidate_path: Path = Path()
        self.candidate_id: str = ""
        self.approval_digest: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.quote = "A committed synthetic quote supports this exact candidate."
        source_path = self.write_input("lifecycle-evidence.md", f"{self.quote}\n")
        options = CaptureOptions(
            source_path, "lifecycle-source", "lifecycle-session", "Lifecycle evidence",
            "lifecycle-project", "Synthetic reviewer", "2026-11-06T12:30:00+09:00", None, True,
        )
        self.event_id = capture_event(self.context, load_capture_input(options, self.context)).event_id
        self.candidate_path = create_candidate(self.context, CandidateInput(
            self.event_id, "Lifecycle claim", "A synthetic claim is retained for review.", self.quote,
            "Synthetic reviewer",
        ))
        self.candidate_id = self.candidate_path.stem
        _metadata, _snapshot, self.approval_digest = preview_candidate(self.vault, self.candidate_id)

    def test_pre_v013_candidate_snapshot_remains_immutable_and_usable(self) -> None:
        metadata, body, _normalized = parse_frontmatter(self.candidate_path.read_bytes())
        family_id = text_field(metadata, "candidate_family_id")
        member_path = self.vault / "90_System" / "KnowledgeFamilies" / family_id / f"{self.candidate_id}.md"
        member_path.unlink()
        member_path.parent.rmdir()
        _ = metadata.pop("family_membership_version")
        legacy_bytes = render_markdown(metadata, body).encode("utf-8")
        _ = self.candidate_path.write_bytes(legacy_bytes)

        repeated = create_candidate(self.context, CandidateInput(
            self.event_id, "Lifecycle claim", "A synthetic claim is retained for review.", self.quote,
            "Synthetic reviewer",
        ))
        self.assertEqual(repeated, self.candidate_path)
        self.assertEqual(self.candidate_path.read_bytes(), legacy_bytes)
        _legacy_metadata, _snapshot, legacy_digest = preview_candidate(self.vault, self.candidate_id)
        _ = approve_candidate(self.context, ApprovalInput(self.candidate_id, legacy_digest, "Synthetic reviewer"))

    def test_family_membership_prevents_approving_a_mutated_candidate_revision(self) -> None:
        _ = self.candidate_path.write_bytes(self.candidate_path.read_bytes() + b"\nchanged after proposal\n")
        _metadata, _snapshot, changed_digest = preview_candidate(self.vault, self.candidate_id)
        with self.assertRaisesRegex(ChronicleError, "invalid_candidate"):
            _ = approve_candidate(self.context, ApprovalInput(
                self.candidate_id, changed_digest, "Synthetic reviewer",
            ))

    def test_rejection_is_durable_and_closed_candidates_cannot_be_approved(self) -> None:
        _ = reject_candidate(self.context, RejectionInput(
            self.candidate_id, "Synthetic reviewer", "Evidence is insufficient for reuse",
        ))
        self.assertEqual(
            text_field(decision_files(self.vault, self.candidate_id)[-1][1], "decision"), "reject",
        )
        with self.assertRaisesRegex(ChronicleError, "candidate_closed"):
            _ = approve_candidate(self.context, ApprovalInput(
                self.candidate_id, self.approval_digest, "Synthetic reviewer",
            ))

    def test_approved_candidate_cannot_be_rejected(self) -> None:
        _ = approve_candidate(self.context, ApprovalInput(
            self.candidate_id, self.approval_digest, "Synthetic reviewer",
        ))
        with self.assertRaisesRegex(ChronicleError, "candidate_closed"):
            _ = reject_candidate(self.context, RejectionInput(
                self.candidate_id, "Synthetic reviewer", "Cannot overwrite an approved decision",
            ))

    def test_superseding_relation_only_replaces_an_approved_old_candidate(self) -> None:
        newer_path = create_candidate(self.context, CandidateInput(
            self.event_id, "Lifecycle claim revised", "A revised synthetic claim supersedes the earlier one.",
            self.quote, "Synthetic reviewer",
        ))
        _old_metadata, _old_snapshot, old_digest = preview_candidate(self.vault, self.candidate_id)
        _new_metadata, _new_snapshot, new_digest = preview_candidate(self.vault, newer_path.stem)
        _ = approve_candidate(self.context, ApprovalInput(self.candidate_id, old_digest, "Synthetic reviewer"))
        _ = approve_candidate(self.context, ApprovalInput(newer_path.stem, new_digest, "Synthetic reviewer"))
        relation_path = create_relation(self.context, RelationInput(
            newer_path.stem, self.candidate_id, "supersedes", "Newer source-backed revision supersedes old claim",
        ))
        self.assertEqual(len(search_knowledge(
            self.vault, KnowledgeSearchRequest("synthetic", "personal", "lifecycle-project"),
        )), 2)
        _ = review_relation(self.context, RelationReviewInput(
            relation_path.stem, "confirm", "Synthetic reviewer", "Explicit supersession review",
        ))
        results = search_knowledge(
            self.vault, KnowledgeSearchRequest("synthetic", "personal", "lifecycle-project"),
        )
        self.assertEqual([item.candidate_id for item in results], [newer_path.stem])

    def test_all_relationship_kinds_keep_evidence_reasons_and_pending_review(self) -> None:
        second_path = create_candidate(self.context, CandidateInput(
            self.event_id, "Relationship target", "A separately typed synthetic claim.", self.quote,
            "Synthetic reviewer",
        ))
        relation_types = ("supports", "related_to", "contradicts", "supersedes", "used_in")
        for relation_type in relation_types:
            _ = create_relation(self.context, RelationInput(
                self.candidate_id, second_path.stem, relation_type,
                f"Explicit user rationale for {relation_type}",
            ))
        rows = list_relations(self.vault)
        self.assertEqual(len(rows), len(relation_types))
        self.assertEqual({json_text(row.get("relation_type")) for row in rows}, set(relation_types))
        self.assertTrue(all(json_text(row.get("status")) == "pending_review" for row in rows))
        self.assertTrue(all(bool(json_text(row.get("rationale"))) for row in rows))
        self.assertTrue(all(bool(json_text(row.get("first_candidate_revision_id"))) for row in rows))
        self.assertTrue(all(bool(json_text(row.get("second_candidate_digest"))) for row in rows))
        directed_rows = [
            row for row in rows
            if json_text(row.get("relation_type")) in {"supports", "supersedes", "used_in"}
        ]
        self.assertTrue(all(
            json_text(row.get("first_candidate_id")) == self.candidate_id for row in directed_rows
        ))
