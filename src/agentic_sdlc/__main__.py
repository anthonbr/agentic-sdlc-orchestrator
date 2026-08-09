"""Minimal command-line entry point for the built-in demonstration."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_sdlc.state import ApprovalResponse, WorkflowState, demo_input
from agentic_sdlc.workflow import WORKFLOW, resume_workflow, run_workflow


def write_workflow_diagram(output_path: Path) -> None:
    """Render the actual compiled workflow graph to a PNG file."""

    png_bytes = WORKFLOW.get_graph().draw_mermaid_png()
    if not png_bytes:
        raise ValueError("The workflow diagram renderer returned no PNG data.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png_bytes)


def main(arguments: list[str] | None = None) -> int:
    """Run the one V0.4 command without introducing a CLI dependency."""

    args = list(sys.argv[1:] if arguments is None else arguments)
    if args != ["demo"]:
        print("Usage: python -m agentic_sdlc demo", file=sys.stderr)
        return 2

    artifacts_dir = Path.cwd() / "artifacts"
    diagram_path = artifacts_dir / "workflow_diagram.png"
    try:
        write_workflow_diagram(diagram_path)
    except Exception as error:
        detail = str(error).splitlines()[0] or type(error).__name__
        print(
            f"Warning: workflow diagram was not generated: {detail}",
            file=sys.stderr,
        )
    else:
        print(f"Workflow diagram written to: {diagram_path}")

    artifact_dir = artifacts_dir / "demo-run"
    thread_id = f"demo-{uuid4().hex}"
    state = run_workflow(
        demo_input(),
        thread_id=thread_id,
        artifact_dir=artifact_dir,
        workflow=WORKFLOW,
    )
    while state.get("__interrupt__"):
        payload = _interrupt_payload(state)
        if payload.get("stage") == "requirement_analysis_review":
            response = _prompt_for_requirement_analysis_decision(payload)
        elif payload.get("stage") == "task_graph_review":
            response = _prompt_for_task_graph_decision(payload)
        else:
            raise ValueError("The workflow paused at an unknown review stage.")
        state = resume_workflow(
            thread_id,
            response,
            artifact_dir=artifact_dir,
            workflow=WORKFLOW,
        )

    for event in state.get("trace", []):
        print(event)

    if state.get("workflow_status") == "success":
        print("Workflow completed successfully.")
        print(f"Artifacts written to: {artifact_dir}")
        return 0

    if state.get("workflow_status") == "safe_stopped":
        print(f"Workflow stopped safely: {state['safe_stop_reason']}")
        print(f"Partial artifacts written to: {artifact_dir}")
        return 1

    print(f"Workflow failed: {state.get('workflow_status', 'unknown')}")
    for error in state.get("errors", []):
        print(f"- {error}")
    return 1


def _interrupt_payload(state: WorkflowState) -> dict[str, Any]:
    interrupt_event = state["__interrupt__"][0]
    return interrupt_event.value


def _prompt_for_requirement_analysis_decision(
    payload: dict[str, Any],
) -> ApprovalResponse:
    """Show the complete analysis boundary before collecting human authority."""

    analysis = payload["requirement_analysis"]
    print("\nRequirement analysis requires human review.")
    print(f"Current revision: {payload['revision_number']}")
    print(f"Normalized problem: {analysis['normalized_problem_statement']}")
    print(f"Requirement type: {analysis['requirement_type']}")
    _print_analysis_list("Functional requirements", analysis["functional_requirements"])
    _print_analysis_list(
        "Nonfunctional requirements", analysis["nonfunctional_requirements"]
    )
    _print_analysis_list("Constraints", analysis["constraints"])
    _print_analysis_list("Ambiguities", analysis["ambiguities"])
    _print_analysis_list("Assumptions", analysis["assumptions"])
    _print_analysis_list("Acceptance criteria", analysis["acceptance_criteria"])
    _print_analysis_list("Risks", analysis["risks"])
    print(f"Needs clarification: {analysis['needs_clarification']}")
    print(f"Confidence: {analysis['confidence']:.2f}")
    return _prompt_for_decision()


def _print_analysis_list(label: str, values: list[str]) -> None:
    print(f"{label}:")
    if not values:
        print("  - None identified.")
        return
    for value in values:
        print(f"  - {value}")


def _prompt_for_task_graph_decision(payload: dict[str, Any]) -> ApprovalResponse:
    """Show canonical identities and derived layers before human authority."""

    spec = payload["approved_requirement_spec"]
    graph = payload["candidate_task_graph"]
    semantics = payload["graph_semantics"]
    tasks = {task["task_id"]: task for task in graph["tasks"]}

    print("\nEngineering task graph requires human review.")
    print(
        f"Approved requirement spec: {spec['spec_id']} "
        f"(analysis revision {spec['source_analysis_revision']})"
    )
    for label, field_name in (
        ("Functional", "functional_requirements"),
        ("Nonfunctional", "nonfunctional_requirements"),
        ("Constraints", "constraints"),
        ("Acceptance criteria", "acceptance_criteria"),
        ("Risks", "risks"),
        ("Ambiguities", "ambiguities"),
    ):
        values = spec[field_name]
        if values:
            print(f"{label} IDs: " + ", ".join(item["item_id"] for item in values))

    print(f"TaskGraph revision: {payload['revision_number']}")
    for layer_number, task_ids in enumerate(
        semantics["execution_layers"], start=1
    ):
        suffix = " — parallel" if len(task_ids) > 1 else ""
        print(f"\nLayer {layer_number}{suffix}")
        for task_id in task_ids:
            task = tasks[task_id]
            print(f"  {task_id}  {task['title']}")
            print(f"    Type: {task['task_type']}")
            print(
                "    Depends on: "
                + (", ".join(task["depends_on"]) or "ENTRY")
            )
            print(
                "    Requirements: "
                + (", ".join(task["requirement_refs"]) or "None")
            )
            print(
                "    Acceptance: "
                + (", ".join(task["acceptance_criteria_refs"]) or "None")
            )
            print("    Risks: " + (", ".join(task["risk_refs"]) or "None"))
            print(
                "    Ambiguities: "
                + (", ".join(task["ambiguity_refs"]) or "None")
            )
    print(
        "\nSynchronization points: "
        + (", ".join(semantics["synchronization_points"]) or "None")
    )
    print(
        "EXIT predecessors: " + ", ".join(semantics["exit_predecessor_tasks"])
    )
    return _prompt_for_decision()


def _prompt_for_decision() -> ApprovalResponse:
    choices = {"a": "APPROVE", "c": "REQUEST_CHANGES", "r": "REJECT"}
    while True:
        choice = input("[A] Approve  [C] Request changes  [R] Reject: ").strip().lower()
        if choice in choices:
            break
        print("Please enter A, C, or R.")

    feedback = ""
    if choice == "c":
        feedback = _prompt_for_revision_feedback()

    return {"decision": choices[choice], "feedback": feedback}


def _prompt_for_revision_feedback() -> str:
    """Collect one or more feedback lines terminated by a blank line."""

    while True:
        print("Requested changes (finish with a blank line):")
        feedback_lines: list[str] = []
        while True:
            line = input()
            if not line.strip():
                break
            feedback_lines.append(line.strip())

        if feedback_lines:
            return "\n".join(feedback_lines)
        print("Feedback is required when requesting changes.")


if __name__ == "__main__":
    raise SystemExit(main())
