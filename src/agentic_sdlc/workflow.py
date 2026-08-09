"""Explicit LangGraph definition for the V0.3 SDLC workflow."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Literal, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from agentic_sdlc.artifacts import write_artifacts
from agentic_sdlc.llm import (
    OpenAIRequirementAnalysisClient,
    RequirementAnalysisClient,
)
from agentic_sdlc.nodes import (
    architecture_task,
    create_implementation_plan,
    decompose_requirements,
    entry_gate,
    exit_gate,
    implementation_plan_approval,
    prepare_requirement_analysis_retry,
    prepare_requirement_analysis_revision,
    requirement_analysis_review,
    requirement_analysis_task,
    requirements_intake,
    revise_implementation_plan,
    safe_stop,
    synchronize,
    test_plan_task,
    validate_requirement_analysis,
)
from agentic_sdlc.state import (
    MAX_PLAN_REVISIONS,
    MAX_REQUIREMENT_ANALYSIS_ATTEMPTS,
    MAX_REQUIREMENT_REVISIONS,
    ApprovalResponse,
    WorkflowState,
)


def route_after_entry_gate(state: WorkflowState) -> Literal["proceed", "stop"]:
    """Route valid work to analysis and invalid work directly to END."""

    return "proceed" if state.get("entry_gate_passed") else "stop"


ApprovalRoute = Literal[
    "architecture_task",
    "test_plan_task",
    "revise_implementation_plan",
    "safe_stop",
]

AnalysisTaskRoute = Literal[
    "validate_requirement_analysis",
    "prepare_requirement_analysis_retry",
    "safe_stop",
]
AnalysisValidationRoute = Literal[
    "requirement_analysis_review",
    "prepare_requirement_analysis_retry",
    "safe_stop",
]
RequirementReviewRoute = Literal[
    "decompose_requirements",
    "prepare_requirement_analysis_revision",
    "safe_stop",
]


def route_after_requirement_analysis_task(state: WorkflowState) -> AnalysisTaskRoute:
    """Send a provider result to validation, retry, or safe stop."""

    if state.get("requirement_analysis_status") == "candidate":
        return "validate_requirement_analysis"
    return _failed_analysis_route(state)


def route_after_requirement_analysis_validation(
    state: WorkflowState,
) -> AnalysisValidationRoute:
    """Allow only validated output to reach human review."""

    if state.get("requirement_analysis_status") == "validated":
        return "requirement_analysis_review"
    return _failed_analysis_route(state)


def _failed_analysis_route(
    state: WorkflowState,
) -> Literal["prepare_requirement_analysis_retry", "safe_stop"]:
    if (
        state.get("requirement_analysis_retryable")
        and state.get("requirement_analysis_attempt_count", 0)
        < MAX_REQUIREMENT_ANALYSIS_ATTEMPTS
    ):
        return "prepare_requirement_analysis_retry"
    return "safe_stop"


def route_after_requirement_review(state: WorkflowState) -> RequirementReviewRoute:
    """Keep human authority and its bounded revision policy deterministic."""

    decision = state.get("requirement_review_decision")
    if decision == "APPROVE":
        return "decompose_requirements"
    if decision == "REJECT":
        return "safe_stop"
    if decision == "REQUEST_CHANGES":
        if state.get("requirement_analysis_revision_count", 0) >= (
            MAX_REQUIREMENT_REVISIONS
        ):
            return "safe_stop"
        return "prepare_requirement_analysis_revision"
    raise ValueError("Requirement review did not record a valid decision.")


def route_after_plan_approval(
    state: WorkflowState,
) -> ApprovalRoute | list[ApprovalRoute]:
    """Route a recorded human decision without allowing an unbounded loop."""

    decision = state.get("implementation_plan_decision")
    if decision == "APPROVE":
        return ["architecture_task", "test_plan_task"]
    if decision == "REJECT":
        return "safe_stop"
    if decision == "REQUEST_CHANGES":
        if state.get("plan_revision_count", 0) >= MAX_PLAN_REVISIONS:
            return "safe_stop"
        return "revise_implementation_plan"
    raise ValueError("Implementation-plan approval did not record a valid decision.")


def build_workflow(
    requirement_analyst: RequirementAnalysisClient | None = None,
) -> CompiledStateGraph:
    """Build and compile the intentionally explicit V0.3 dependency graph."""

    analyst = requirement_analyst or OpenAIRequirementAnalysisClient()
    builder = StateGraph(WorkflowState)

    builder.add_node("requirements_intake", requirements_intake)
    builder.add_node("entry_gate", entry_gate)
    builder.add_node(
        "requirement_analysis_task",
        partial(requirement_analysis_task, client=analyst),
    )
    builder.add_node("validate_requirement_analysis", validate_requirement_analysis)
    builder.add_node(
        "prepare_requirement_analysis_retry", prepare_requirement_analysis_retry
    )
    builder.add_node("requirement_analysis_review", requirement_analysis_review)
    builder.add_node(
        "prepare_requirement_analysis_revision", prepare_requirement_analysis_revision
    )
    builder.add_node("decompose_requirements", decompose_requirements)
    builder.add_node("create_implementation_plan", create_implementation_plan)
    builder.add_node("implementation_plan_approval", implementation_plan_approval)
    builder.add_node("revise_implementation_plan", revise_implementation_plan)
    builder.add_node("safe_stop", safe_stop)
    builder.add_node("architecture_task", architecture_task)
    builder.add_node("test_plan_task", test_plan_task)
    builder.add_node("synchronize", synchronize)
    builder.add_node("exit_gate", exit_gate)

    builder.add_edge(START, "requirements_intake")
    builder.add_edge("requirements_intake", "entry_gate")
    builder.add_conditional_edges(
        "entry_gate",
        route_after_entry_gate,
        {"proceed": "requirement_analysis_task", "stop": END},
    )
    builder.add_conditional_edges(
        "requirement_analysis_task",
        route_after_requirement_analysis_task,
        {
            "validate_requirement_analysis": "validate_requirement_analysis",
            "prepare_requirement_analysis_retry": (
                "prepare_requirement_analysis_retry"
            ),
            "safe_stop": "safe_stop",
        },
    )
    builder.add_conditional_edges(
        "validate_requirement_analysis",
        route_after_requirement_analysis_validation,
        {
            "requirement_analysis_review": "requirement_analysis_review",
            "prepare_requirement_analysis_retry": (
                "prepare_requirement_analysis_retry"
            ),
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge(
        "prepare_requirement_analysis_retry", "requirement_analysis_task"
    )
    builder.add_conditional_edges(
        "requirement_analysis_review",
        route_after_requirement_review,
        {
            "decompose_requirements": "decompose_requirements",
            "prepare_requirement_analysis_revision": (
                "prepare_requirement_analysis_revision"
            ),
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge(
        "prepare_requirement_analysis_revision", "requirement_analysis_task"
    )
    builder.add_edge("decompose_requirements", "create_implementation_plan")
    builder.add_edge("create_implementation_plan", "implementation_plan_approval")
    builder.add_conditional_edges(
        "implementation_plan_approval",
        route_after_plan_approval,
        {
            "architecture_task": "architecture_task",
            "test_plan_task": "test_plan_task",
            "revise_implementation_plan": "revise_implementation_plan",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge("revise_implementation_plan", "implementation_plan_approval")
    builder.add_edge("safe_stop", END)

    # APPROVE fans out into independent branches. This list edge is their barrier.
    builder.add_edge(["architecture_task", "test_plan_task"], "synchronize")
    builder.add_edge("synchronize", "exit_gate")
    builder.add_edge("exit_gate", END)

    return builder.compile(checkpointer=InMemorySaver())


WORKFLOW = build_workflow()


def run_workflow(
    workflow_input: WorkflowState | Command,
    *,
    thread_id: str,
    artifact_dir: Path | None = None,
    workflow: CompiledStateGraph | None = None,
) -> WorkflowState:
    """Run or resume one checkpointed workflow thread."""

    config = {"configurable": {"thread_id": thread_id}}
    active_workflow = workflow or WORKFLOW
    final_state = cast(
        WorkflowState, active_workflow.invoke(workflow_input, config=config)
    )
    if (
        artifact_dir is not None
        and not final_state.get("__interrupt__")
        and final_state.get("workflow_status") in {"success", "safe_stopped"}
    ):
        write_artifacts(final_state, artifact_dir)
    return final_state


def resume_workflow(
    thread_id: str,
    decision: ApprovalResponse,
    *,
    artifact_dir: Path | None = None,
    workflow: CompiledStateGraph | None = None,
) -> WorkflowState:
    """Resume one interrupted workflow with a typed human decision."""

    return run_workflow(
        Command(resume=decision),
        thread_id=thread_id,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
