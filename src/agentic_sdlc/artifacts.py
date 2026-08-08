"""Write compact, reviewable artifacts after a successful workflow run."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.state import WorkflowState


ARTIFACT_FILENAMES = (
    "requirements.json",
    "decomposition.json",
    "implementation_plan.md",
    "architecture.md",
    "test_plan.md",
    "summary.md",
)


def write_artifacts(state: WorkflowState, output_dir: Path) -> list[Path]:
    """Write the V0.1 artifact set only for a successfully validated state."""

    if state.get("workflow_status") != "success" or not state.get(
        "exit_gate_passed"
    ):
        raise ValueError("Artifacts can only be written for a successful workflow.")

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / filename for filename in ARTIFACT_FILENAMES]

    _write_json(
        paths[0],
        {
            "project_name": state["project_name"],
            "submitted_requirements": state["requirements"],
            "normalized_requirements": state["normalized_requirements"],
        },
    )
    _write_json(paths[1], {"work_items": state["work_items"]})
    paths[2].write_text(_implementation_plan_markdown(state), encoding="utf-8")
    paths[3].write_text(_architecture_markdown(state), encoding="utf-8")
    paths[4].write_text(_test_plan_markdown(state), encoding="utf-8")
    paths[5].write_text(_summary_markdown(state), encoding="utf-8")
    return paths


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _implementation_plan_markdown(state: WorkflowState) -> str:
    lines = ["# Implementation Plan", ""]
    for step in state["implementation_plan"]:
        related_items = ", ".join(step["work_item_ids"])
        lines.extend(
            [
                f"{step['order']}. {step['action']}",
                f"   - Related work items: {related_items}",
            ]
        )
    return "\n".join(lines) + "\n"


def _architecture_markdown(state: WorkflowState) -> str:
    architecture = state["architecture"]
    lines = [
        "# Architecture",
        "",
        architecture["summary"],
        "",
        "## Conceptual components",
        "",
        *(f"- {component}" for component in architecture["components"]),
        "",
        "## Design notes",
        "",
        *(f"- {note}" for note in architecture["design_notes"]),
    ]
    return "\n".join(lines) + "\n"


def _test_plan_markdown(state: WorkflowState) -> str:
    test_plan = state["test_plan"]
    lines = ["# Test Plan", "", test_plan["strategy"], "", "## Cases", ""]
    for test_case in test_plan["cases"]:
        lines.append(f"- **{test_case['name']}** — {test_case['purpose']}")
    return "\n".join(lines) + "\n"


def _summary_markdown(state: WorkflowState) -> str:
    lines = [
        "# Workflow Summary",
        "",
        f"- Project: {state['project_name']}",
        f"- Workflow result: {state['workflow_status']}",
        f"- Entry gate: {'passed' if state['entry_gate_passed'] else 'failed'}",
        "- Synchronization: "
        + ("complete" if state["synchronization_complete"] else "failed"),
        f"- Exit gate: {'passed' if state['exit_gate_passed'] else 'failed'}",
        "",
        "The deterministic V0.1 workflow processed requirements, decomposed them, "
        "created an implementation plan, ran architecture and test planning in "
        "parallel, synchronized both results, and validated the final state.",
        "",
        "## Generated artifacts",
        "",
        *(f"- `{filename}`" for filename in ARTIFACT_FILENAMES),
    ]
    return "\n".join(lines) + "\n"
