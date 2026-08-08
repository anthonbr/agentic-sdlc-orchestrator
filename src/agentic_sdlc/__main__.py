"""Minimal command-line entry point for the built-in demonstration."""

from __future__ import annotations

import sys
from pathlib import Path

from agentic_sdlc.state import demo_input
from agentic_sdlc.workflow import WORKFLOW, run_workflow


def write_workflow_diagram(output_path: Path) -> None:
    """Render the actual compiled workflow graph to a PNG file."""

    png_bytes = WORKFLOW.get_graph().draw_mermaid_png()
    if not png_bytes:
        raise ValueError("The workflow diagram renderer returned no PNG data.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png_bytes)


def main(arguments: list[str] | None = None) -> int:
    """Run the one V0.1 command without introducing a CLI dependency."""

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
    state = run_workflow(demo_input(), artifact_dir=artifact_dir)

    for event in state.get("trace", []):
        print(event)

    if state.get("workflow_status") == "success":
        print("Workflow completed successfully.")
        print(f"Artifacts written to: {artifact_dir}")
        return 0

    print(f"Workflow stopped safely: {state.get('workflow_status', 'unknown')}")
    for error in state.get("errors", []):
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
