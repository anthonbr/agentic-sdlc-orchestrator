"""Minimal command-line entry point for the built-in demonstration."""

from __future__ import annotations

import sys
from pathlib import Path
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
    """Run the one V0.2 command without introducing a CLI dependency."""

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
        demo_input(), thread_id=thread_id, artifact_dir=artifact_dir
    )
    while state.get("__interrupt__"):
        response = _prompt_for_implementation_plan_decision(state)
        state = resume_workflow(
            thread_id, response, artifact_dir=artifact_dir
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


def _prompt_for_implementation_plan_decision(
    state: WorkflowState,
) -> ApprovalResponse:
    """Collect one valid response for the active approval interrupt."""

    interrupt_event = state["__interrupt__"][0]
    payload = interrupt_event.value
    print("\nImplementation plan requires approval.")
    print(f"Current revision: {payload['revision_number']}")
    for step in payload["implementation_plan"]:
        print(f"  {step['order']}. {step['action']}")

    choices = {"a": "APPROVE", "c": "REQUEST_CHANGES", "r": "REJECT"}
    while True:
        choice = input("[A] Approve  [C] Request changes  [R] Reject: ").strip().lower()
        if choice in choices:
            break
        print("Please enter A, C, or R.")

    feedback = ""
    if choice == "c":
        while not feedback:
            feedback = input("Requested changes: ").strip()
            if not feedback:
                print("Feedback is required when requesting changes.")

    return {"decision": choices[choice], "feedback": feedback}


if __name__ == "__main__":
    raise SystemExit(main())
