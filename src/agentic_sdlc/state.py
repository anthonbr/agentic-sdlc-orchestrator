"""Typed shared state and demonstration input for the governed workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from agentic_sdlc.task_execution import (
    TaskExecutionFailure,
    TaskExecutionRecoveryDecision,
    TaskGraphExecutionState,
)
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    TaskExecutionRequest,
    TaskExecutionResult,
    TaskExecutionValidationResult,
)


class NormalizedRequirement(TypedDict):
    """Submitted requirement with a stable intake identifier."""

    id: str
    text: str


ApprovalDecision = Literal["APPROVE", "REQUEST_CHANGES", "REJECT"]


class ApprovalResponse(TypedDict):
    """Human response used to resume a governance checkpoint."""

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
    """One requirement-analysis provider or schema failure."""

    sequence: int
    revision_number: int
    attempt_number: int
    reason: str
    retryable: bool


class RequirementSpecItemData(TypedDict):
    """JSON-safe canonical approved requirement item."""

    item_id: str
    lineage_id: str
    text: str


class ApprovedRequirementSpecData(TypedDict):
    """JSON-safe immutable requirement specification."""

    spec_id: str
    lineage_id: str
    version: int
    supersedes_spec_id: str | None
    source_analysis_revision: int
    created_at: str
    content_hash: str
    normalized_problem_statement: str
    requirement_type: Literal["greenfield", "brownfield", "ambiguous"]
    assumptions: list[str]
    functional_requirements: list[RequirementSpecItemData]
    nonfunctional_requirements: list[RequirementSpecItemData]
    constraints: list[RequirementSpecItemData]
    acceptance_criteria: list[RequirementSpecItemData]
    risks: list[RequirementSpecItemData]
    ambiguities: list[RequirementSpecItemData]


TaskTypeData = Literal[
    "DESIGN", "IMPLEMENTATION", "TEST", "DOCUMENTATION", "VALIDATION", "RELEASE"
]
TaskPlanningStatus = Literal["pending", "candidate", "validated", "failed"]


class TaskData(TypedDict):
    """JSON-safe canonical task definition."""

    task_id: str
    lineage_id: str
    source_key: str
    title: str
    description: str
    task_type: TaskTypeData
    depends_on: list[str]
    requirement_refs: list[str]
    acceptance_criteria_refs: list[str]
    risk_refs: list[str]
    ambiguity_refs: list[str]
    expected_outputs: list[str]


class TaskGraphData(TypedDict):
    """JSON-safe canonical engineering task graph."""

    graph_id: str
    lineage_id: str
    version: int
    requirement_spec_id: str
    requirement_spec_version: int
    supersedes_graph_id: str | None
    created_at: str
    content_hash: str
    tasks: list[TaskData]


class TaskGraphSemanticsData(TypedDict):
    """JSON-safe derived interpretation of one canonical task graph."""

    topological_order: list[str]
    execution_layers: list[list[str]]
    entry_ready_tasks: list[str]
    exit_predecessor_tasks: list[str]
    synchronization_points: list[str]


class TaskGraphRecord(TypedDict):
    """One validated candidate graph with generation lineage."""

    sequence: int
    revision_number: int
    attempt_number: int
    prompt_version: str
    model_name: str
    reviewer_feedback: str
    task_graph: TaskGraphData


class TaskPlanningFailure(TypedDict):
    """One provider, schema, or deterministic graph-validation failure."""

    sequence: int
    revision_number: int
    attempt_number: int
    reason: str
    retryable: bool


WorkflowStatus = Literal[
    "pending",
    "awaiting_approval",
    "entry_gate_failed",
    "exit_gate_failed",
    "safe_stopped",
    "success",
]


class WorkflowState(TypedDict, total=False):
    """Shared JSON-safe state updated by the static LangGraph control plane."""

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
    approved_requirement_spec: ApprovedRequirementSpecData
    task_planning_candidate: object | None
    task_planning_status: TaskPlanningStatus
    task_planning_attempt_count: int
    task_planning_retryable: bool
    task_planning_error: str
    task_graph_revision_count: int
    task_planning_model: str
    candidate_task_graph: TaskGraphData
    task_graph_semantics: TaskGraphSemanticsData
    approved_task_graph: TaskGraphData
    task_graph_history: Annotated[list[TaskGraphRecord], operator.add]
    task_planning_failures: Annotated[list[TaskPlanningFailure], operator.add]
    task_graph_decision: ApprovalDecision | None
    task_graph_feedback: str
    task_graph_review_history: Annotated[list[ApprovalEvent], operator.add]
    task_graph_execution: TaskGraphExecutionState
    task_execution_requests: Annotated[list[TaskExecutionRequest], operator.add]
    task_execution_results: Annotated[list[TaskExecutionResult], operator.add]
    engineering_artifacts: Annotated[list[EngineeringArtifact], operator.add]
    task_execution_validations: Annotated[
        list[TaskExecutionValidationResult], operator.add
    ]
    task_execution_failures: Annotated[list[TaskExecutionFailure], operator.add]
    task_execution_recovery_decisions: Annotated[
        list[TaskExecutionRecoveryDecision], operator.add
    ]
    safe_stop_reason: str
    exit_gate_passed: bool
    workflow_status: WorkflowStatus
    errors: list[str]
    trace: Annotated[list[str], operator.add]


MAX_REQUIREMENT_ANALYSIS_ATTEMPTS = 3
MAX_REQUIREMENT_REVISIONS = 3
MAX_TASK_PLANNING_ATTEMPTS = 3
MAX_TASK_GRAPH_REVISIONS = 3
REQUIREMENT_ANALYSIS_REJECTED_REASON = "Requirement analysis rejected by human."
MAX_REQUIREMENT_REVISIONS_REASON = (
    f"Maximum requirement-analysis revisions ({MAX_REQUIREMENT_REVISIONS}) reached; "
    "no further revisions are allowed."
)
REQUIREMENT_ANALYSIS_ATTEMPTS_REASON = (
    f"Requirement analysis failed after {MAX_REQUIREMENT_ANALYSIS_ATTEMPTS} attempts."
)
TASK_GRAPH_REJECTED_REASON = "Engineering task graph rejected by human."
MAX_TASK_GRAPH_REVISIONS_REASON = (
    f"Maximum task-graph revisions ({MAX_TASK_GRAPH_REVISIONS}) reached; "
    "no further revisions are allowed."
)
TASK_PLANNING_ATTEMPTS_REASON = (
    f"Task planning failed after {MAX_TASK_PLANNING_ATTEMPTS} attempts."
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
