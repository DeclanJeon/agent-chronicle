"""Local-only execution for user-mediated knowledge commands."""

from typing import cast

from chronicle.command_requests import (
    ApproveCommand,
    DistillCommand,
    DistillJobsCommand,
    RetryDistillCommand,
    KnowledgeSearchCommand,
    PreviewCommand,
    RelateCommand,
    RelationListCommand,
    RelationReviewCommand,
    RejectCommand,
    ProposeCommand,
    RetractCommand,
    ReviewDueCommand,
)
from chronicle.knowledge_review import due_review_page
from chronicle.knowledge_distill import preview_session_distillation
from chronicle.knowledge_families import family_member_ids
from chronicle.knowledge_job_queries import list_distillation_jobs
from chronicle.knowledge_jobs import persist_session_distillation, retry_distillation
from chronicle.knowledge_relations import create_relation, list_relations, review_relation
from chronicle.errors import ChronicleError
from chronicle.knowledge import (
    approve_candidate,
    create_candidate,
    preview_candidate,
    reject_candidate,
    retract_candidate,
    search_knowledge,
)
from chronicle.models import CaptureContext, JsonValue
from chronicle.schema import text_field

KnowledgeCommand = (
    ProposeCommand | PreviewCommand | ApproveCommand | RetractCommand
    | KnowledgeSearchCommand | ReviewDueCommand | RelateCommand | RejectCommand
    | RelationListCommand | RelationReviewCommand | DistillCommand
    | DistillJobsCommand | RetryDistillCommand
)


def execute_knowledge(context: CaptureContext, command: KnowledgeCommand) -> JsonValue:
    """Run one explicitly chosen proposal, approval, retraction or search action."""
    if isinstance(command, ReviewDueCommand):
        return due_review_page(context.config.vault, command.limit, command.offset)
    if isinstance(command, DistillCommand):
        if command.preview:
            return preview_session_distillation(context.config.vault, command.session_id)
        if command.persist:
            return persist_session_distillation(context, command.session_id)
        raise ChronicleError("invalid_command", "distillation requires --preview or --save")
    if isinstance(command, DistillJobsCommand):
        return cast(JsonValue, list_distillation_jobs(context.config.vault))
    if isinstance(command, RetryDistillCommand):
        return retry_distillation(context, command.job_id)
    if isinstance(command, RelationListCommand):
        rows: list[JsonValue] = []
        for item in list_relations(context.config.vault, command.candidate_id):
            rows.append(item)
        return rows
    if isinstance(command, RelateCommand):
        path = create_relation(context, command.request)
        return {"status": "relation_pending_review", "relation_id": path.stem}
    if isinstance(command, RelationReviewCommand):
        path = review_relation(context, command.request)
        return {"status": "relation_reviewed", "decision_file": path.name}
    if isinstance(command, RejectCommand):
        path = reject_candidate(context, command.request)
        return {"status": "rejected", "decision_file": path.name}
    if isinstance(command, RetractCommand):
        path = retract_candidate(context, command.request)
        return {"status": "retracted", "decision_file": path.name}
    if isinstance(command, KnowledgeSearchCommand):
        results = search_knowledge(context.config.vault, command.request)
        return [{
            "candidate_id": item.candidate_id,
            "title": item.title,
            "claim": item.claim,
            "exact_quote": item.quote,
            "source_event_id": item.source_event_id,
            "project_id": item.project_id,
        } for item in results]
    if isinstance(command, ProposeCommand):
        path = create_candidate(context, command.request)
        metadata, _snapshot, _digest = preview_candidate(context.config.vault, path.stem)
        family_id_value = metadata.get("candidate_family_id")
        if not isinstance(family_id_value, str):
            raise ChronicleError("invalid_candidate", "candidate family ID is missing")
        members = family_member_ids(context.config.vault, family_id_value)
        raw_duplicates = metadata.get("duplicate_candidate_ids", [])
        if not isinstance(raw_duplicates, list) or any(not isinstance(item, str) for item in raw_duplicates):
            raise ChronicleError("invalid_candidate", "duplicate candidate links are malformed")
        duplicate_ids = set(members)
        for item in raw_duplicates:
            if isinstance(item, str):
                duplicate_ids.add(item)
        duplicate_ids.discard(path.stem)
        duplicate_values: list[JsonValue] = []
        for duplicate_id in sorted(duplicate_ids):
            duplicate_values.append(duplicate_id)
        return {
            "status": "candidate_created",
            "candidate_id": path.stem,
            "candidate_family_id": metadata.get("candidate_family_id"),
            "duplicate_status": metadata.get("duplicate_status", "none"),
            "duplicate_candidate_ids": duplicate_values,
        }
    if isinstance(command, PreviewCommand):
        metadata, snapshot, digest = preview_candidate(context.config.vault, command.candidate_id)
        return {
            "status": "preview_only",
            "candidate_id": command.candidate_id,
            "title": text_field(metadata, "title"),
            "scope": text_field(metadata, "scope"),
            "approval_digest_version": 1,
            "approval_digest": digest,
            "markdown_snapshot": snapshot,
        }
    path = approve_candidate(context, command.request)
    return {"status": "approved", "decision_file": path.name}
