"""Typed shared state and demonstration input for the V0.3 workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict


class NormalizedRequirement(TypedDict):
    """A normalized requirement with a stable workflow identifier."""

    id: str
    text: str


class WorkItem(TypedDict):
    """An actionable item traced back to one source requirement."""

    id: str
    source_requirement_id: str
    source_requirement: str
    action: str


class PlanStep(TypedDict):
    """One ordered implementation-planning step."""

    order: int
    action: str
    work_item_ids: list[str]


class ArchitectureArtifact(TypedDict):
    """Small architecture output produced by one parallel branch."""

    summary: str
    components: list[str]
    design_notes: list[str]


class TestCase(TypedDict):
    """One planned verification case."""

    name: str
    purpose: str


class TestPlanArtifact(TypedDict):
    """Small test-plan output produced by one parallel branch."""

    strategy: str
    cases: list[TestCase]


ApprovalDecision = Literal["APPROVE", "REQUEST_CHANGES", "REJECT"]


class ApprovalResponse(TypedDict):
    """Human response used to resume either governance checkpoint."""

    decision: ApprovalDecision
    feedback: str


class ApprovalEvent(TypedDict):
    """One ordered human governance decision at a named checkpoint."""

    sequence: int
    checkpoint: str
    decision: ApprovalDecision
    feedback: str
    revision_number: int


RequirementAnalysisStatus = Literal["pending", "candidate", "validated", "failed"]


class RequirementAnalysisData(TypedDict):
    """JSON-safe validated analysis stored in checkpointed shared state."""

    normalized_problem_statement: str
    requirement_type: Literal["greenfield", "brownfield", "ambiguous"]
    functional_requirements: list[str]
    nonfunctional_requirements: list[str]
    constraints: list[str]
    ambiguities: list[str]
    assumptions: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    needs_clarification: bool
    confidence: float


class RequirementAnalysisRecord(TypedDict):
    """One validated analysis proposal with its generation lineage."""

    sequence: int
    revision_number: int
    attempt_number: int
    prompt_version: str
    model_name: str
    reviewer_feedback: str
    analysis: RequirementAnalysisData


class RequirementAnalysisFailure(TypedDict):
    """One provider or schema failure considered by retry policy."""

    sequence: int
    revision_number: int
    attempt_number: int
    reason: str
    retryable: bool


WorkflowStatus = Literal[
    "pending",
    "awaiting_approval",
    "entry_gate_failed",
    "synchronization_failed",
    "exit_gate_failed",
    "safe_stopped",
    "success",
]


class WorkflowState(TypedDict, total=False):
    """Shared state updated by governed LangGraph nodes."""

    project_name: str
    requirements: list[str]
    raw_requirement: str
    normalized_requirements: list[NormalizedRequirement]
    entry_gate_passed: bool
    requirement_analysis_candidate: object | None
    requirement_analysis: RequirementAnalysisData
    requirement_analysis_status: RequirementAnalysisStatus
    requirement_analysis_attempt_count: int
    requirement_analysis_retryable: bool
    requirement_analysis_error: str
    requirement_analysis_revision_count: int
    requirement_analysis_model: str
    requirement_analysis_history: Annotated[
        list[RequirementAnalysisRecord], operator.add
    ]
    requirement_analysis_failures: Annotated[
        list[RequirementAnalysisFailure], operator.add
    ]
    requirement_review_decision: ApprovalDecision | None
    requirement_review_feedback: str
    requirement_review_history: Annotated[list[ApprovalEvent], operator.add]
    work_items: list[WorkItem]
    implementation_plan: list[PlanStep]
    implementation_plan_decision: ApprovalDecision | None
    approval_feedback: str
    plan_revision_count: int
    approval_history: Annotated[list[ApprovalEvent], operator.add]
    safe_stop_reason: str
    architecture: ArchitectureArtifact
    test_plan: TestPlanArtifact
    synchronization_complete: bool
    exit_gate_passed: bool
    workflow_status: WorkflowStatus
    errors: list[str]
    trace: Annotated[list[str], operator.add]


MAX_PLAN_REVISIONS = 3
MAX_REQUIREMENT_ANALYSIS_ATTEMPTS = 3
MAX_REQUIREMENT_REVISIONS = 3
PLAN_REJECTED_REASON = "Implementation plan rejected by human."
MAX_PLAN_REVISIONS_REASON = (
    f"Maximum implementation plan revisions ({MAX_PLAN_REVISIONS}) reached; "
    "no further revisions are allowed."
)
REQUIREMENT_ANALYSIS_REJECTED_REASON = "Requirement analysis rejected by human."
MAX_REQUIREMENT_REVISIONS_REASON = (
    f"Maximum requirement-analysis revisions ({MAX_REQUIREMENT_REVISIONS}) reached; "
    "no further revisions are allowed."
)
REQUIREMENT_ANALYSIS_ATTEMPTS_REASON = (
    f"Requirement analysis failed after {MAX_REQUIREMENT_ANALYSIS_ATTEMPTS} attempts."
)

DEMO_REQUIREMENTS = (
    "Accept a long URL.",
    "Generate a unique short URL.",
    "Redirect the short URL to the original URL.",
    "Return an error for unknown short URLs.",
)
DEMO_RAW_REQUIREMENT = "Build a URL Shortener that:\n" + "\n".join(
    f"{index}. {requirement}"
    for index, requirement in enumerate(DEMO_REQUIREMENTS, start=1)
)


def demo_input() -> WorkflowState:
    """Return a fresh copy of the built-in URL Shortener requirements."""

    return {
        "project_name": "URL Shortener",
        "requirements": list(DEMO_REQUIREMENTS),
        "raw_requirement": DEMO_RAW_REQUIREMENT,
    }
