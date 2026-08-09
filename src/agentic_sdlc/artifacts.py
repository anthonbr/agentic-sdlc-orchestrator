"""Write compact, reviewable artifacts after a completed workflow run."""

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
SAFE_STOP_ARTIFACT_FILENAMES = (
    "requirements.json",
    "decomposition.json",
    "implementation_plan.md",
    "summary.md",
)


def write_artifacts(state: WorkflowState, output_dir: Path) -> list[Path]:
    """Write full success artifacts or an honest partial safe-stop set."""

    is_success = state.get("workflow_status") == "success" and state.get(
        "exit_gate_passed"
    )
    is_safe_stop = state.get("workflow_status") == "safe_stopped" and bool(
        state.get("safe_stop_reason")
    )
    if not is_success and not is_safe_stop:
        raise ValueError("Artifacts require a successful or safely stopped workflow.")

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_filenames = (
        ARTIFACT_FILENAMES if is_success else SAFE_STOP_ARTIFACT_FILENAMES
    )
    paths = {filename: output_dir / filename for filename in ARTIFACT_FILENAMES}

    for filename in set(ARTIFACT_FILENAMES) - set(generated_filenames):
        paths[filename].unlink(missing_ok=True)

    _write_json(
        paths["requirements.json"],
        {
            "project_name": state["project_name"],
            "submitted_requirements": state["requirements"],
            "normalized_requirements": state["normalized_requirements"],
        },
    )
    _write_json(paths["decomposition.json"], {"work_items": state["work_items"]})
    paths["implementation_plan.md"].write_text(
        _implementation_plan_markdown(state), encoding="utf-8"
    )
    if is_success:
        paths["architecture.md"].write_text(
            _architecture_markdown(state), encoding="utf-8"
        )
        paths["test_plan.md"].write_text(
            _test_plan_markdown(state), encoding="utf-8"
        )
    paths["summary.md"].write_text(
        _summary_markdown(state, generated_filenames), encoding="utf-8"
    )
    return [paths[filename] for filename in generated_filenames]


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


def _summary_markdown(
    state: WorkflowState, generated_filenames: tuple[str, ...]
) -> str:
    safe_stopped = state["workflow_status"] == "safe_stopped"
    lines = [
        "# Workflow Summary",
        "",
        f"- Project: {state['project_name']}",
        f"- Workflow result: {state['workflow_status']}",
        f"- Entry gate: {'passed' if state['entry_gate_passed'] else 'failed'}",
        "- Synchronization: "
        + (
            "not reached"
            if safe_stopped
            else ("complete" if state["synchronization_complete"] else "failed")
        ),
        "- Exit gate: "
        + (
            "not reached"
            if safe_stopped
            else ("passed" if state["exit_gate_passed"] else "failed")
        ),
        "",
    ]
    if safe_stopped:
        lines.extend(
            [
                f"Execution stopped safely: {state['safe_stop_reason']}",
                "Downstream architecture and test planning did not run.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "The deterministic V0.2 workflow processed requirements, decomposed "
                "them, received implementation-plan approval, ran architecture and "
                "test planning in parallel, synchronized both results, and validated "
                "the final state.",
                "",
            ]
        )

    lines.extend(["## Human Approval History", "", "### Implementation Plan", ""])
    for event in state.get("approval_history", []):
        lines.extend(
            [
                f"{event['sequence']}. {event['decision']}",
                f"   - Revision: {event['revision_number']}",
            ]
        )
        if event["feedback"]:
            lines.append(f"   - Feedback: {event['feedback']}")

    lines.extend(
        [
            "",
            "## Generated artifacts",
            "",
            *(f"- `{filename}`" for filename in generated_filenames),
        ]
    )
    return "\n".join(lines) + "\n"
