"""Static LangGraph control plane for governed planning and TaskGraph execution."""

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
    OpenAITaskPlanningClient,
    RequirementAnalysisClient,
    TaskPlanningClient,
)
from agentic_sdlc.nodes import (
    approve_task_graph,
    build_approved_requirement_spec,
    entry_gate,
    execute_task_graph_step,
    exit_gate,
    initialize_task_graph_execution_node,
    normalize_and_validate_task_graph,
    prepare_requirement_analysis_retry,
    prepare_requirement_analysis_revision,
    prepare_task_graph_revision,
    prepare_task_planning_retry,
    requirement_analysis_review,
    requirement_analysis_task,
    requirements_intake,
    safe_stop,
    task_decomposition_task,
    task_graph_review,
    validate_requirement_analysis,
)
from agentic_sdlc.requirement_analysis import RequirementPlanningReadinessError
from agentic_sdlc.state import (
    MAX_REQUIREMENT_ANALYSIS_ATTEMPTS,
    MAX_REQUIREMENT_REVISIONS,
    MAX_TASK_GRAPH_REVISIONS,
    MAX_TASK_PLANNING_ATTEMPTS,
    ApprovalResponse,
    WorkflowState,
)
from agentic_sdlc.task_execution import TaskGraphExecutionStatus
from agentic_sdlc.task_executor import OpenAITaskExecutor, TaskExecutor
from agentic_sdlc.workspace_integration import (
    DeterministicRepositoryContextPathProvider,
    GovernedWorkspaceRuntime,
    RepositoryContextPathProvider,
)


def route_after_entry_gate(state: WorkflowState) -> Literal["proceed", "stop"]:
    """Route valid work to analysis and invalid work directly to END."""

    return "proceed" if state.get("entry_gate_passed") else "stop"


def route_after_requirement_analysis_task(
    state: WorkflowState,
) -> Literal[
    "validate_requirement_analysis",
    "prepare_requirement_analysis_retry",
    "safe_stop",
]:
    """Send a provider result to validation, retry, or safe stop."""

    if state.get("requirement_analysis_status") == "candidate":
        return "validate_requirement_analysis"
    return _failed_requirement_analysis_route(state)


def route_after_requirement_analysis_validation(
    state: WorkflowState,
) -> Literal[
    "requirement_analysis_review",
    "prepare_requirement_analysis_retry",
    "safe_stop",
]:
    """Allow only validated analysis to reach human review."""

    if state.get("requirement_analysis_status") == "validated":
        return "requirement_analysis_review"
    return _failed_requirement_analysis_route(state)


def _failed_requirement_analysis_route(
    state: WorkflowState,
) -> Literal["prepare_requirement_analysis_retry", "safe_stop"]:
    if (
        state.get("requirement_analysis_retryable")
        and state.get("requirement_analysis_attempt_count", 0)
        < MAX_REQUIREMENT_ANALYSIS_ATTEMPTS
    ):
        return "prepare_requirement_analysis_retry"
    return "safe_stop"


def route_after_requirement_review(
    state: WorkflowState,
) -> Literal[
    "build_approved_requirement_spec",
    "prepare_requirement_analysis_revision",
    "safe_stop",
]:
    """Keep requirement approval authority and bounded revision deterministic."""

    decision = state.get("requirement_review_decision")
    if decision == "APPROVE":
        readiness = state.get("requirement_planning_readiness")
        if readiness is None or readiness["status"] != "READY":
            reason_code = (
                readiness["reason_code"]
                if readiness is not None
                else "MISSING_REQUIREMENT_PLANNING_READINESS"
            )
            raise RequirementPlanningReadinessError(
                f"{reason_code}: blocked requirement analysis cannot advance to "
                "approved specification creation."
            )
        return "build_approved_requirement_spec"
    if decision == "REJECT":
        return "safe_stop"
    if decision == "REQUEST_CHANGES":
        if state.get("requirement_analysis_revision_count", 0) >= (
            MAX_REQUIREMENT_REVISIONS
        ):
            return "safe_stop"
        return "prepare_requirement_analysis_revision"
    raise ValueError("Requirement review did not record a valid decision.")


def route_after_task_decomposition(
    state: WorkflowState,
) -> Literal[
    "normalize_and_validate_task_graph", "prepare_task_planning_retry", "safe_stop"
]:
    """Route a task proposal to deterministic validation or bounded failure."""

    if state.get("task_planning_status") == "candidate":
        return "normalize_and_validate_task_graph"
    return _failed_task_planning_route(state)


def route_after_task_graph_validation(
    state: WorkflowState,
) -> Literal["task_graph_review", "prepare_task_planning_retry", "safe_stop"]:
    """Allow only deterministically valid graphs to reach human review."""

    if state.get("task_planning_status") == "validated":
        return "task_graph_review"
    return _failed_task_planning_route(state)


def _failed_task_planning_route(
    state: WorkflowState,
) -> Literal["prepare_task_planning_retry", "safe_stop"]:
    if (
        state.get("task_planning_retryable")
        and state.get("task_planning_attempt_count", 0) < MAX_TASK_PLANNING_ATTEMPTS
    ):
        return "prepare_task_planning_retry"
    return "safe_stop"


def route_after_task_graph_review(
    state: WorkflowState,
) -> Literal["approve_task_graph", "prepare_task_graph_revision", "safe_stop"]:
    """Keep TaskGraph promotion under bounded human authority."""

    decision = state.get("task_graph_decision")
    if decision == "APPROVE":
        return "approve_task_graph"
    if decision == "REJECT":
        return "safe_stop"
    if decision == "REQUEST_CHANGES":
        if state.get("task_graph_revision_count", 0) >= MAX_TASK_GRAPH_REVISIONS:
            return "safe_stop"
        return "prepare_task_graph_revision"
    raise ValueError("Task-graph review did not record a valid decision.")


def route_after_task_graph_execution_step(
    state: WorkflowState,
) -> Literal["execute_task_graph_step", "exit_gate", "safe_stop"]:
    """Route the static loop from authoritative runtime execution status."""

    status = state["task_graph_execution"].status
    if status is TaskGraphExecutionStatus.RUNNING:
        return "execute_task_graph_step"
    if status is TaskGraphExecutionStatus.SUCCEEDED:
        return "exit_gate"
    if status is TaskGraphExecutionStatus.FAILED:
        return "safe_stop"
    raise ValueError(f"Unsupported TaskGraph execution route: {status.value}.")


def route_after_task_graph_execution_initialization(
    state: WorkflowState,
) -> Literal["execute_task_graph_step", "safe_stop"]:
    """Do not enter execution when source authority blocked initialization."""

    if state.get("task_graph_execution") is None:
        return "safe_stop"
    return "execute_task_graph_step"


def build_workflow(
    requirement_analyst: RequirementAnalysisClient | None = None,
    task_planner: TaskPlanningClient | None = None,
    task_executor: TaskExecutor | None = None,
    workspace_runtime: GovernedWorkspaceRuntime | None = None,
    repository_context_path_provider: RepositoryContextPathProvider | None = None,
) -> CompiledStateGraph:
    """Build the explicit static control graph; engineering tasks stay data."""

    analyst = requirement_analyst or OpenAIRequirementAnalysisClient()
    planner = task_planner or OpenAITaskPlanningClient()
    executor = task_executor or OpenAITaskExecutor()
    active_workspace_runtime = workspace_runtime or GovernedWorkspaceRuntime()
    path_provider = (
        repository_context_path_provider
        or DeterministicRepositoryContextPathProvider()
    )
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
    builder.add_node(
        "build_approved_requirement_spec", build_approved_requirement_spec
    )
    builder.add_node(
        "task_decomposition_task",
        partial(task_decomposition_task, client=planner),
    )
    builder.add_node(
        "normalize_and_validate_task_graph", normalize_and_validate_task_graph
    )
    builder.add_node("prepare_task_planning_retry", prepare_task_planning_retry)
    builder.add_node("task_graph_review", task_graph_review)
    builder.add_node("prepare_task_graph_revision", prepare_task_graph_revision)
    builder.add_node("approve_task_graph", approve_task_graph)
    builder.add_node(
        "initialize_task_graph_execution",
        partial(
            initialize_task_graph_execution_node,
            workspace_runtime=active_workspace_runtime,
        ),
    )
    builder.add_node(
        "execute_task_graph_step",
        partial(
            execute_task_graph_step,
            executor=executor,
            workspace_runtime=active_workspace_runtime,
            repository_context_path_provider=path_provider,
        ),
    )
    builder.add_node("safe_stop", safe_stop)
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
            "prepare_requirement_analysis_retry": "prepare_requirement_analysis_retry",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_conditional_edges(
        "validate_requirement_analysis",
        route_after_requirement_analysis_validation,
        {
            "requirement_analysis_review": "requirement_analysis_review",
            "prepare_requirement_analysis_retry": "prepare_requirement_analysis_retry",
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
            "build_approved_requirement_spec": "build_approved_requirement_spec",
            "prepare_requirement_analysis_revision": (
                "prepare_requirement_analysis_revision"
            ),
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge(
        "prepare_requirement_analysis_revision", "requirement_analysis_task"
    )
    builder.add_edge("build_approved_requirement_spec", "task_decomposition_task")
    builder.add_conditional_edges(
        "task_decomposition_task",
        route_after_task_decomposition,
        {
            "normalize_and_validate_task_graph": (
                "normalize_and_validate_task_graph"
            ),
            "prepare_task_planning_retry": "prepare_task_planning_retry",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_conditional_edges(
        "normalize_and_validate_task_graph",
        route_after_task_graph_validation,
        {
            "task_graph_review": "task_graph_review",
            "prepare_task_planning_retry": "prepare_task_planning_retry",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge("prepare_task_planning_retry", "task_decomposition_task")
    builder.add_conditional_edges(
        "task_graph_review",
        route_after_task_graph_review,
        {
            "approve_task_graph": "approve_task_graph",
            "prepare_task_graph_revision": "prepare_task_graph_revision",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_edge("prepare_task_graph_revision", "task_decomposition_task")
    builder.add_edge("safe_stop", END)

    # Dynamic TASK-### records remain data interpreted by this fixed static loop.
    builder.add_edge("approve_task_graph", "initialize_task_graph_execution")
    builder.add_conditional_edges(
        "initialize_task_graph_execution",
        route_after_task_graph_execution_initialization,
        {
            "execute_task_graph_step": "execute_task_graph_step",
            "safe_stop": "safe_stop",
        },
    )
    builder.add_conditional_edges(
        "execute_task_graph_step",
        route_after_task_graph_execution_step,
        {
            "execute_task_graph_step": "execute_task_graph_step",
            "exit_gate": "exit_gate",
            "safe_stop": "safe_stop",
        },
    )
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

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 100,
    }
    active_workflow = workflow or WORKFLOW
    prepared_input: WorkflowState | Command = workflow_input
    if isinstance(workflow_input, dict):
        prepared_input = cast(
            WorkflowState,
            {**workflow_input, "run_id": thread_id},
        )
    final_state = cast(
        WorkflowState, active_workflow.invoke(prepared_input, config=config)
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
