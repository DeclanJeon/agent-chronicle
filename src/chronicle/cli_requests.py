"""Convert parsed CLI arguments into immutable typed requests."""

from collections.abc import Sequence
from pathlib import Path

from chronicle.cli_parser import Arguments, build_parser
from chronicle.cli_knowledge_requests import parse_knowledge_command
from chronicle.cli_parse_common import optional_text, parse_date, required_text
from chronicle.command_requests import (
    AskCommand,
    ClassifyCommand,
    CaptureCommand,
    CommandRequest,
    ContextCommand,
    DailyCommand,
    DaemonCommand,
    WeeklyReviewCommand,
    InitCommand,
    ImportCommand,
    InventoryCommand,
    OpenTodayCommand,
    PrivacyInspectCommand,
    StorageMigrateCommand,
    ProjectAliasCommand,
    RebuildCommand,
    ReviewDueCommand,
    ServiceCommand,
    SearchCommand,
    SourcesListCommand,
    SourceEnableCommand,
    SourceDisableCommand,
    CollectCommand,
    StatusCommand,
    DoctorCommand,
)
from chronicle.errors import ChronicleError
from chronicle.knowledge_models import (
    KnowledgeSearchRequest,
)
from chronicle.models import CaptureOptions
from chronicle.search import SearchRequest


def parse_command(argv: Sequence[str] | None = None) -> CommandRequest:
    """Parse raw argv into one immutable typed command variant."""
    arguments = Arguments()
    _ = build_parser().parse_args(argv, namespace=arguments)
    command = required_text(arguments.command, "command")
    match command:  # noqa: MATCH_OK — argparse provides an open string command
        case "init":
            vault = Path(arguments.vault) if arguments.vault is not None else None
            return InitCommand(vault, arguments.timezone)
        case "capture":
            options = CaptureOptions(
                Path(required_text(arguments.file, "file")),
                required_text(arguments.source_event_id, "source-event-id"),
                arguments.session_id,
                required_text(arguments.title, "title"),
                required_text(arguments.project_id, "project"),
                required_text(arguments.author, "author"),
                arguments.occurred_at,
                arguments.document_id,
                arguments.final,
                arguments.event_type,
            )
            return CaptureCommand(options)
        case "import":
            return ImportCommand(
                Path(required_text(arguments.file, "file")),
                required_text(arguments.source, "source"),
                required_text(arguments.project_id, "project"),
                required_text(arguments.title, "title"),
                arguments.dry_run,
                arguments.start_byte,
                arguments.end_byte,
            )
        case "daily":
            return DailyCommand(parse_date(arguments.day))
        case "search":
            request = SearchRequest(
                required_text(arguments.query, "query"),
                parse_date(arguments.date),
                arguments.author,
                arguments.project_id,
                optional_text(arguments.search_session_id),
                optional_text(arguments.search_event_type),
            )
            return SearchCommand(request)
        case "knowledge":
            return parse_knowledge_command(arguments)
        case "ask":
            request = KnowledgeSearchRequest(
                required_text(arguments.query, "query"), "personal", arguments.project_id,
            )
            return AskCommand(request)
        case "context":
            if arguments.saved_bundle_id is not None:
                if arguments.query is not None or arguments.project_id is not None or arguments.save_context:
                    raise ChronicleError("invalid_command", "saved bundle checks cannot include query, project, or save")
                request = KnowledgeSearchRequest("", "personal")
            else:
                request = KnowledgeSearchRequest(
                    required_text(arguments.query, "query"), "personal", arguments.project_id,
                )
            return ContextCommand(
                request, arguments.preview, arguments.max_characters,
                arguments.save_context, arguments.saved_bundle_id,
            )
        case "sources":
            action = required_text(arguments.source_action, "source action")
            match action:  # noqa: MATCH_OK — argparse supplies an open string value
                case "list":
                    return SourcesListCommand()
                case "enable":
                    if arguments.source_file is not None:
                        selected_path = Path(arguments.source_file)
                        scope_kind = "file"
                    elif arguments.source_root is not None:
                        selected_path = Path(arguments.source_root)
                        scope_kind = "root"
                    else:
                        raise ChronicleError("invalid_command", "select one source file or root")
                    return SourceEnableCommand(
                        required_text(arguments.source_name, "source"),
                        selected_path, scope_kind,
                        required_text(arguments.project_id, "project"),
                        required_text(arguments.title, "title"),
                    )
                case "disable":
                    return SourceDisableCommand(required_text(arguments.source_id, "source ID"))
                case _:
                    raise ChronicleError("invalid_command", "unsupported source command")
        case "collect":
            if not 0.1 <= arguments.interval_seconds <= 3600:
                raise ChronicleError("invalid_input", "collector interval must be between 0.1 and 3600 seconds")
            return CollectCommand(arguments.once, arguments.interval_seconds)
        case "status":
            return StatusCommand()
        case "review":
            if arguments.weekly_review:
                return WeeklyReviewCommand()
            if not arguments.review_due:
                raise ChronicleError("invalid_command", "review requires --due or --weekly")
            return ReviewDueCommand(arguments.review_limit, arguments.review_offset)
        case "classify":
            return ClassifyCommand(
                required_text(arguments.classify_event_id, "event ID"),
                required_text(arguments.project_id, "project"),
                optional_text(arguments.classify_event_type),
                required_text(arguments.reviewer, "reviewer"),
                required_text(arguments.reason, "reason"),
            )
        case "open":
            if not arguments.today or not arguments.preview:
                raise ChronicleError("invalid_command", "open supports only --today --preview")
            return OpenTodayCommand(arguments.preview)
        case "privacy":
            if arguments.privacy_action != "inspect":
                raise ChronicleError("invalid_command", "privacy requires inspect")
            return PrivacyInspectCommand(Path(required_text(arguments.privacy_file, "privacy file")))
        case "storage":
            if arguments.storage_action != "migrate":
                raise ChronicleError("invalid_command", "storage requires migrate")
            return StorageMigrateCommand(
                Path(required_text(arguments.storage_destination, "storage destination")),
                arguments.storage_dry_run,
            )
        case "home":
            return InventoryCommand("home")
        case "projects":
            return InventoryCommand("projects", optional_text(arguments.project_id))
        case "sessions":
            return InventoryCommand(
                "sessions", optional_text(arguments.project_id),
                optional_text(arguments.inventory_session_id),
            )
        case "documents":
            return InventoryCommand(
                "documents", optional_text(arguments.project_id), None,
                optional_text(arguments.inventory_document_id),
            )
        case "project":
            if arguments.project_alias_action is None:
                raise ChronicleError("invalid_command", "project alias action is required")
            return ProjectAliasCommand(
                arguments.project_alias_action,
                optional_text(arguments.project_id),
                optional_text(arguments.project_alias),
            )
        case "service":
            if arguments.service_action is None:
                raise ChronicleError("invalid_command", "service action is required")
            return ServiceCommand(
                arguments.service_action, arguments.service_preview, arguments.service_confirm,
            )
        case "doctor":
            return DoctorCommand()
        case "rebuild":
            return RebuildCommand()
        case "daemon":
            return DaemonCommand()
        case _:
            raise ChronicleError("invalid_command", "unsupported command")
