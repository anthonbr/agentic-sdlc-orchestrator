"""Typed shared state and demonstration input for the V0.2 workflow."""

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
    """Human response used to resume the implementation-plan checkpoint."""

    decision: ApprovalDecision
    feedback: str


class ApprovalEvent(TypedDict):
    """One ordered implementation-plan governance decision."""

    sequence: int
    checkpoint: str
    decision: ApprovalDecision
    feedback: str
    revision_number: int


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
    """Shared state updated by the deterministic LangGraph nodes."""

    project_name: str
    requirements: list[str]
    normalized_requirements: list[NormalizedRequirement]
    entry_gate_passed: bool
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
PLAN_REJECTED_REASON = "Implementation plan rejected by human."
MAX_PLAN_REVISIONS_REASON = (
    f"Maximum implementation plan revisions ({MAX_PLAN_REVISIONS}) reached; "
    "no further revisions are allowed."
)

DEMO_REQUIREMENTS = (
    "Accept a long URL.",
    "Generate a unique short URL.",
    "Redirect the short URL to the original URL.",
    "Return an error for unknown short URLs.",
)


def demo_input() -> WorkflowState:
    """Return a fresh copy of the built-in URL Shortener requirements."""

    return {
        "project_name": "URL Shortener",
        "requirements": list(DEMO_REQUIREMENTS),
    }
