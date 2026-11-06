"""Knowledge command parser, isolated from the general CLI surface."""

import argparse


def add_knowledge_parser(knowledge: argparse.ArgumentParser) -> None:
    """Attach explicit candidate, relation, and distillation commands."""
    knowledge_actions = knowledge.add_subparsers(dest="knowledge_action", required=True)
    propose = knowledge_actions.add_parser("propose", help="create an unapproved candidate from exact source text")
    _ = propose.add_argument("--event-id", required=True)
    _ = propose.add_argument("--title", required=True)
    _ = propose.add_argument("--claim", required=True)
    _ = propose.add_argument("--quote", required=True)
    _ = propose.add_argument("--created-by", required=True)
    _ = propose.add_argument(
        "--type", dest="knowledge_type",
        choices=("concept", "lesson", "decision", "procedure", "idea", "question", "preference"),
        default="lesson",
    )
    preview = knowledge_actions.add_parser("preview", help="display complete candidate and approval digest")
    _ = preview.add_argument("candidate_id")
    approve = knowledge_actions.add_parser("approve", help="approve only the previewed digest")
    _ = approve.add_argument("candidate_id")
    _ = approve.add_argument("--confirm-digest", required=True)
    _ = approve.add_argument("--reviewer", required=True)
    _ = approve.add_argument("--review-days", type=int, default=30)
    reject = knowledge_actions.add_parser("reject", help="reject an unapproved candidate with a durable reason")
    _ = reject.add_argument("candidate_id")
    _ = reject.add_argument("--reviewer", required=True)
    _ = reject.add_argument("--reason", required=True)
    retract = knowledge_actions.add_parser("retract", help="immediately block a candidate from reuse")
    _ = retract.add_argument("candidate_id")
    _ = retract.add_argument("--reviewer", required=True)
    _ = retract.add_argument("--reason", required=True)
    knowledge_search = knowledge_actions.add_parser("search", help="search currently approved knowledge")
    _ = knowledge_search.add_argument("--query", required=True)
    _ = knowledge_search.add_argument("--context", dest="context_name", default="personal")
    _ = knowledge_search.add_argument("--project", dest="project_id")
    relation = knowledge_actions.add_parser("relation", help="propose, review, or list explicit candidate links")
    relation_actions = relation.add_subparsers(dest="relation_action", required=True)
    relation_create = relation_actions.add_parser("create", help="propose a revision-bound relationship")
    _ = relation_create.add_argument("--first-candidate-id", dest="relation_first_candidate_id", required=True)
    _ = relation_create.add_argument("--second-candidate-id", dest="relation_second_candidate_id", required=True)
    _ = relation_create.add_argument(
        "--type", dest="relation_type",
        choices=("supports", "related_to", "contradicts", "supersedes", "used_in"),
        required=True,
    )
    _ = relation_create.add_argument("--reason", required=True)
    relation_list = relation_actions.add_parser("list", help="list explicit relation proposals")
    _ = relation_list.add_argument("--candidate-id", dest="candidate_id")
    relation_review = relation_actions.add_parser("review", help="confirm or dismiss a relation proposal")
    _ = relation_review.add_argument("relation_id")
    _ = relation_review.add_argument("--decision", dest="relation_decision", choices=("confirm", "dismiss"), required=True)
    _ = relation_review.add_argument("--reviewer", required=True)
    _ = relation_review.add_argument("--reason", required=True)
    distill = knowledge_actions.add_parser("distill", help="preview or persist candidates from marked zones")
    _ = distill.add_argument("--session", dest="distill_session_id", required=True)
    distill_mode = distill.add_mutually_exclusive_group(required=True)
    _ = distill_mode.add_argument("--preview", dest="distill_preview", action="store_true")
    _ = distill_mode.add_argument("--save", dest="distill_save", action="store_true")
    _ = knowledge_actions.add_parser("jobs", help="list durable local distillation job states")
    retry = knowledge_actions.add_parser("retry", help="retry one pending or failed distillation job")
    _ = retry.add_argument("distill_job_id", help="durable distillation job UUID")
