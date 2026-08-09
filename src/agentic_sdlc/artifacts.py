"""Write compact, reviewable artifacts after a completed workflow run."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.state import WorkflowState


ARTIFACT_FILENAMES = (
    "requirements.json",
    "requirement_analysis.md",
    "decomposition.json",
    "implementation_plan.md",
    "architecture.md",
    "test_plan.md",
    "summary.md",
)
SAFE_STOP_ARTIFACT_FILENAMES = (
    "requirements.json",
    "requirement_analysis.md",
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
        ARTIFACT_FILENAMES if is_success else _safe_stop_filenames(state)
    )
    paths = {filename: output_dir / filename for filename in ARTIFACT_FILENAMES}

    for filename in set(ARTIFACT_FILENAMES) - set(generated_filenames):
        paths[filename].unlink(missing_ok=True)

    _write_json(
        paths["requirements.json"],
        {
            "project_name": state["project_name"],
            "raw_requirement": state["raw_requirement"],
            "submitted_requirements": state["requirements"],
            "normalized_requirements": state["normalized_requirements"],
        },
    )
    if "requirement_analysis.md" in generated_filenames:
        paths["requirement_analysis.md"].write_text(
            _requirement_analysis_markdown(state), encoding="utf-8"
        )
    if "decomposition.json" in generated_filenames:
        _write_json(paths["decomposition.json"], {"work_items": state["work_items"]})
    if "implementation_plan.md" in generated_filenames:
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


def _safe_stop_filenames(state: WorkflowState) -> tuple[str, ...]:
    filenames = ["requirements.json"]
    if state.get("requirement_analysis"):
        filenames.append("requirement_analysis.md")
    if state.get("work_items"):
        filenames.append("decomposition.json")
    if state.get("implementation_plan"):
        filenames.append("implementation_plan.md")
    filenames.append("summary.md")
    return tuple(filenames)


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


def _requirement_analysis_markdown(state: WorkflowState) -> str:
    analysis = state["requirement_analysis"]
    lines = [
        "# Requirement Analysis",
        "",
        "## Original requirement",
        "",
        *(f"> {line}" for line in state["raw_requirement"].splitlines()),
        "",
        "## Current validated analysis",
        "",
        f"- Requirement type: {analysis['requirement_type']}",
        f"- Needs clarification: {str(analysis['needs_clarification']).lower()}",
        f"- Confidence: {analysis['confidence']:.2f}",
        "",
        "### Normalized problem",
        "",
        analysis["normalized_problem_statement"],
    ]
    sections = (
        ("Functional requirements", analysis["functional_requirements"]),
        ("Nonfunctional requirements", analysis["nonfunctional_requirements"]),
        ("Constraints", analysis["constraints"]),
        ("Ambiguities", analysis["ambiguities"]),
        ("Assumptions", analysis["assumptions"]),
        ("Acceptance criteria", analysis["acceptance_criteria"]),
        ("Risks", analysis["risks"]),
    )
    for heading, values in sections:
        lines.extend(["", f"### {heading}", ""])
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- None identified.")

    lines.extend(["", "## Analysis lineage", ""])
    for record in state.get("requirement_analysis_history", []):
        lines.extend(
            [
                f"{record['sequence']}. Revision {record['revision_number']}",
                f"   - Attempt: {record['attempt_number']}",
                f"   - Prompt: {record['prompt_version']}",
                f"   - Model: {record['model_name']}",
                "   - Normalized problem: "
                + record["analysis"]["normalized_problem_statement"],
            ]
        )
        ambiguities = record["analysis"]["ambiguities"]
        assumptions = record["analysis"]["assumptions"]
        lines.append(
            "   - Ambiguities: "
            + ("; ".join(ambiguities) if ambiguities else "None identified.")
        )
        lines.append(
            "   - Assumptions: "
            + ("; ".join(assumptions) if assumptions else "None identified.")
        )
        if record["reviewer_feedback"]:
            lines.append(f"   - Reviewer feedback: {record['reviewer_feedback']}")

    lines.extend(["", "## Human requirement-review history", ""])
    for event in state.get("requirement_review_history", []):
        lines.extend(
            [
                f"{event['sequence']}. {event['decision']}",
                f"   - Revision: {event['revision_number']}",
            ]
        )
        if event["feedback"]:
            lines.append(f"   - Feedback: {event['feedback']}")
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
        "- Requirement analysis: "
        + state.get("requirement_analysis_status", "not reached"),
        "- Requirement review: "
        + (state.get("requirement_review_decision") or "not reached"),
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
        downstream_work = (
            "architecture and test planning"
            if state.get("implementation_plan")
            else "requirement decomposition and all later planning"
        )
        lines.extend(
            [
                f"Execution stopped safely: {state['safe_stop_reason']}",
                f"Downstream {downstream_work} did not run.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "The governed V0.3 workflow validated an LLM-backed requirement "
                "analysis, received requirement and implementation-plan approvals, "
                "ran deterministic architecture and test planning in parallel, "
                "synchronized both results, and validated the final state.",
                "",
            ]
        )

    lines.extend(
        ["## Human Approval History", "", "### Requirement Analysis", ""]
    )
    for event in state.get("requirement_review_history", []):
        lines.extend(
            [
                f"{event['sequence']}. {event['decision']}",
                f"   - Revision: {event['revision_number']}",
            ]
        )
        if event["feedback"]:
            lines.append(f"   - Feedback: {event['feedback']}")

    if state.get("requirement_analysis_failures"):
        lines.extend(["", "### Requirement-analysis failures", ""])
        for failure in state["requirement_analysis_failures"]:
            lines.extend(
                [
                    f"{failure['sequence']}. Attempt {failure['attempt_number']}",
                    f"   - Revision: {failure['revision_number']}",
                    f"   - Retryable: {str(failure['retryable']).lower()}",
                    f"   - Reason: {failure['reason']}",
                ]
            )

    lines.extend(["", "### Implementation Plan", ""])
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
