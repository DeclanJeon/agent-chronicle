"""Immutable request and result values for the knowledge workflow."""

from dataclasses import dataclass
from typing import Literal

from chronicle.models import JsonValue

KnowledgeType = Literal["concept", "lesson", "decision", "procedure", "idea", "question", "preference"]
RelationType = Literal["supports", "related_to", "contradicts", "supersedes", "used_in"]
RelationDecision = Literal["confirm", "dismiss"]


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """User-authored claim and exact quote to stage for review."""

    event_id: str
    title: str
    claim: str
    quote: str
    created_by: str
    knowledge_type: KnowledgeType = "lesson"


@dataclass(frozen=True, slots=True)
class ApprovalInput:
    """Digest-bound user approval request."""

    candidate_id: str
    confirm_digest: str
    reviewer: str
    review_days: int = 30


@dataclass(frozen=True, slots=True)
class RetractionInput:
    """Immediate user retraction request."""

    candidate_id: str
    reviewer: str
    reason: str


@dataclass(frozen=True, slots=True)
class RejectionInput:
    """User rejection request for a candidate that has not been approved."""

    candidate_id: str
    reviewer: str
    reason: str


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRequest:
    """Search query and the user's currently allowed context."""

    query: str
    context_name: str = "personal"
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    """Safe result returned only after current eligibility checks."""

    candidate_id: str
    title: str
    claim: str
    quote: str
    source_event_id: str
    project_id: str
    approval_digest: str


@dataclass(frozen=True, slots=True)
class RelationInput:
    """An explicit, user-authored relationship proposal between candidates."""

    first_candidate_id: str
    second_candidate_id: str
    relation_type: RelationType
    reason: str


@dataclass(frozen=True, slots=True)
class RelationReviewInput:
    """A user decision about one immutable relation proposal."""

    relation_id: str
    decision: RelationDecision
    reviewer: str
    reason: str


@dataclass(frozen=True, slots=True)
class DecisionInput:
    """Data for one immutable approval, rejection, or retraction event."""

    candidate_id: str
    metadata: dict[str, JsonValue]
    decision: str
    reviewer: str
    reason: str
    digest: str
    review_days: int | None = None

