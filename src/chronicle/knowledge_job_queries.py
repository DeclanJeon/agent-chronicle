"""Read-only summary query for durable distillation job states."""

from pathlib import Path

from chronicle.errors import ChronicleError
from chronicle.knowledge_job_store import job_history, summarize_job, verify_private_directory
from chronicle.models import JsonValue


def list_distillation_jobs(vault: Path) -> list[dict[str, JsonValue]]:
    """List durable pending, failed, and completed Markdown job states."""
    system = vault / "90_System"
    root = system / "KnowledgeJobs"
    if system.is_symlink() or root.is_symlink():
        raise ChronicleError("unsafe_path", "knowledge job directory is a symbolic link")
    if system.exists():
        verify_private_directory(system)
    if not root.exists():
        return []
    verify_private_directory(root)
    folders = sorted(root.iterdir())
    if len(folders) > 10_000:
        raise ChronicleError("invalid_distill_job", "knowledge job index exceeds its record limit")
    rows: list[dict[str, JsonValue]] = []
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise ChronicleError("unsafe_path", "knowledge job entry is unsafe")
        history = job_history(vault, folder.name)
        if history:
            rows.append(summarize_job(history[-1][1]))
    return rows
