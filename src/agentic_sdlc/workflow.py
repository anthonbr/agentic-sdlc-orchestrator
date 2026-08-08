"""Explicit LangGraph definition for the V0.1 SDLC workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agentic_sdlc.artifacts import write_artifacts
from agentic_sdlc.nodes import (
    architecture_task,
    create_implementation_plan,
    decompose_requirements,
    entry_gate,
    exit_gate,
    requirements_intake,
    synchronize,
    test_plan_task,
)
from agentic_sdlc.state import WorkflowState


def route_after_entry_gate(state: WorkflowState) -> Literal["proceed", "stop"]:
    """Route valid work to decomposition and invalid work directly to END."""

    return "proceed" if state.get("entry_gate_passed") else "stop"


def build_workflow() -> CompiledStateGraph:
    """Build and compile the intentionally explicit V0.1 dependency graph."""

    builder = StateGraph(WorkflowState)

    builder.add_node("requirements_intake", requirements_intake)
    builder.add_node("entry_gate", entry_gate)
    builder.add_node("decompose_requirements", decompose_requirements)
    builder.add_node("create_implementation_plan", create_implementation_plan)
    builder.add_node("architecture_task", architecture_task)
    builder.add_node("test_plan_task", test_plan_task)
    builder.add_node("synchronize", synchronize)
    builder.add_node("exit_gate", exit_gate)

    builder.add_edge(START, "requirements_intake")
    builder.add_edge("requirements_intake", "entry_gate")
    builder.add_conditional_edges(
        "entry_gate",
        route_after_entry_gate,
        {"proceed": "decompose_requirements", "stop": END},
    )
    builder.add_edge("decompose_requirements", "create_implementation_plan")

    # Fan out into two independent branches.
    builder.add_edge("create_implementation_plan", "architecture_task")
    builder.add_edge("create_implementation_plan", "test_plan_task")

    # The list edge is a LangGraph barrier: synchronize waits for both branches.
    builder.add_edge(["architecture_task", "test_plan_task"], "synchronize")
    builder.add_edge("synchronize", "exit_gate")
    builder.add_edge("exit_gate", END)

    return builder.compile()


WORKFLOW = build_workflow()


def run_workflow(
    initial_state: WorkflowState, artifact_dir: Path | None = None
) -> WorkflowState:
    """Run the graph and optionally write artifacts after successful validation."""

    final_state = cast(WorkflowState, WORKFLOW.invoke(initial_state))
    if artifact_dir is not None and final_state.get("workflow_status") == "success":
        write_artifacts(final_state, artifact_dir)
    return final_state
