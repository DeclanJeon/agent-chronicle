"""Parse the explicit knowledge command family."""

from chronicle.cli_parser import Arguments
from chronicle.cli_parse_common import optional_text, required_text
from chronicle.command_requests import (
    ApproveCommand,
    CommandRequest,
    DistillCommand,
    DistillJobsCommand,
    RetryDistillCommand,
    KnowledgeSearchCommand,
    PreviewCommand,
    ProposeCommand,
    RelateCommand,
    RelationListCommand,
    RelationReviewCommand,
    RejectCommand,
    RetractCommand,
)
from chronicle.errors import ChronicleError
from chronicle.knowledge_models import (
    ApprovalInput,
    CandidateInput,
    KnowledgeSearchRequest,
    RelationInput,
    RelationReviewInput,
    RejectionInput,
    RetractionInput,
)


def parse_knowledge_command(arguments: Arguments) -> CommandRequest:
    """Validate one knowledge subcommand and produce its typed request."""
    action = required_text(arguments.knowledge_action, "knowledge action")
    match action:  # noqa: MATCH_OK — argparse provides an open subcommand string
        case "propose":
            request = CandidateInput(
                required_text(arguments.event_id, "event-id"),
                required_text(arguments.title, "title"),
                required_text(arguments.claim, "claim"),
                required_text(arguments.quote, "quote"),
                required_text(arguments.created_by, "created-by"),
                arguments.knowledge_type,
            )
            return ProposeCommand(request)
        case "preview":
            return PreviewCommand(required_text(arguments.candidate_id, "candidate-id"))
        case "approve":
            request = ApprovalInput(
                required_text(arguments.candidate_id, "candidate-id"),
                required_text(arguments.confirm_digest, "confirm-digest"),
                required_text(arguments.reviewer, "reviewer"),
                arguments.review_days,
            )
            return ApproveCommand(request)
        case "distill":
            return DistillCommand(
                required_text(arguments.distill_session_id, "session"),
                arguments.distill_preview,
                arguments.distill_save,
            )
        case "jobs":
            return DistillJobsCommand()
        case "retry":
            return RetryDistillCommand(required_text(arguments.distill_job_id, "job ID"))
        case "relation":
            relation_action = required_text(arguments.relation_action, "relation action")
            if relation_action == "create":
                if arguments.relation_type is None:
                    raise ChronicleError("invalid_command", "relation type is required")
                request = RelationInput(
                    required_text(arguments.relation_first_candidate_id, "first candidate ID"),
                    required_text(arguments.relation_second_candidate_id, "second candidate ID"),
                    arguments.relation_type,
                    required_text(arguments.reason, "reason"),
                )
                return RelateCommand(request)
            if relation_action == "list":
                return RelationListCommand(optional_text(arguments.candidate_id))
            if relation_action == "review":
                if arguments.relation_decision is None:
                    raise ChronicleError("invalid_command", "relation decision is required")
                request = RelationReviewInput(
                    required_text(arguments.relation_id, "relation ID"),
                    arguments.relation_decision,
                    required_text(arguments.reviewer, "reviewer"),
                    required_text(arguments.reason, "reason"),
                )
                return RelationReviewCommand(request)
            raise ChronicleError("invalid_command", "relation action is unsupported")
        case "reject":
            request = RejectionInput(
                required_text(arguments.candidate_id, "candidate-id"),
                required_text(arguments.reviewer, "reviewer"),
                required_text(arguments.reason, "reason"),
            )
            return RejectCommand(request)
        case "retract":
            request = RetractionInput(
                required_text(arguments.candidate_id, "candidate-id"),
                required_text(arguments.reviewer, "reviewer"),
                required_text(arguments.reason, "reason"),
            )
            return RetractCommand(request)
        case "search":
            request = KnowledgeSearchRequest(
                required_text(arguments.query, "query"),
                arguments.context_name,
                arguments.project_id,
            )
            return KnowledgeSearchCommand(request)
        case _:
            raise ChronicleError("invalid_command", "unsupported knowledge command")
