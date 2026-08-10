"""Write compact, reviewable artifacts after a terminal governed workflow run."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.state import ApprovalEvent, TaskGraphData, WorkflowState


ARTIFACT_FILENAMES = (
    "requirements.json",
    "requirement_analysis.md",
    "approved_requirement_spec.json",
    "task_graph.json",
    "task_graph.md",
    "task_execution.json",
    "engineering_artifacts.json",
    "summary.md",
)
LEGACY_ARTIFACT_FILENAMES = (
    "architecture.md",
    "test_plan.md",
    "decomposition.json",
    "implementation_plan.md",
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
    generated = ARTIFACT_FILENAMES if is_success else _safe_stop_filenames(state)
    paths = {filename: output_dir / filename for filename in ARTIFACT_FILENAMES}
    for filename in (
        set(ARTIFACT_FILENAMES) | set(LEGACY_ARTIFACT_FILENAMES)
    ) - set(generated):
        (output_dir / filename).unlink(missing_ok=True)

    _write_json(
        paths["requirements.json"],
        {
            "project_name": state["project_name"],
            "raw_requirement": state["raw_requirement"],
            "submitted_requirements": state["requirements"],
            "normalized_requirements": state["normalized_requirements"],
        },
    )
    if "requirement_analysis.md" in generated:
        paths["requirement_analysis.md"].write_text(
            _requirement_analysis_markdown(state), encoding="utf-8"
        )
    if "approved_requirement_spec.json" in generated:
        _write_json(
            paths["approved_requirement_spec.json"],
            state["approved_requirement_spec"],
        )
    if "task_graph.json" in generated:
        graph = state.get("approved_task_graph") or state["candidate_task_graph"]
        _write_json(paths["task_graph.json"], graph)
        paths["task_graph.md"].write_text(
            _task_graph_markdown(state, graph), encoding="utf-8"
        )
    if "task_execution.json" in generated:
        _write_json(paths["task_execution.json"], _execution_evidence(state))
        _write_json(
            paths["engineering_artifacts.json"],
            [
                artifact.model_dump(mode="json")
                for artifact in state.get("engineering_artifacts", [])
            ],
        )
    paths["summary.md"].write_text(
        _summary_markdown(state, generated), encoding="utf-8"
    )
    return [paths[filename] for filename in generated]


def _safe_stop_filenames(state: WorkflowState) -> tuple[str, ...]:
    filenames = ["requirements.json"]
    if state.get("requirement_analysis"):
        filenames.append("requirement_analysis.md")
    if state.get("approved_requirement_spec"):
        filenames.append("approved_requirement_spec.json")
    if state.get("candidate_task_graph"):
        filenames.extend(["task_graph.json", "task_graph.md"])
    if state.get("task_graph_execution"):
        filenames.extend(["task_execution.json", "engineering_artifacts.json"])
    filenames.append("summary.md")
    return tuple(filenames)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


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
                "   - Ambiguities: "
                + (
                    "; ".join(record["analysis"]["ambiguities"])
                    or "None identified."
                ),
                "   - Assumptions: "
                + (
                    "; ".join(record["analysis"]["assumptions"])
                    or "None identified."
                ),
            ]
        )
        if record["reviewer_feedback"]:
            lines.append(f"   - Reviewer feedback: {record['reviewer_feedback']}")

    lines.extend(["", "## Human requirement-review history", ""])
    _append_approval_history(lines, state.get("requirement_review_history", []))
    return "\n".join(lines) + "\n"


def _task_graph_markdown(state: WorkflowState, graph: TaskGraphData) -> str:
    semantics = state["task_graph_semantics"]
    tasks = {task["task_id"]: task for task in graph["tasks"]}
    runtime_states = {
        item.task_id: item
        for item in (
            state["task_graph_execution"].task_states
            if state.get("task_graph_execution")
            else ()
        )
    }
    lines = [
        "# Engineering Task Dependency Graph",
        "",
        f"- Graph: {graph['graph_id']}",
        f"- Version: {graph['version']}",
        f"- Requirement specification: {graph['requirement_spec_id']}",
        f"- Content hash: `{graph['content_hash']}`",
        "- Execution status: "
        + (
            state["task_graph_execution"].status.value
            if state.get("task_graph_execution")
            else "not started"
        ),
        "",
        "## Derived execution layers",
        "",
    ]
    for layer_number, task_ids in enumerate(
        semantics["execution_layers"], start=1
    ):
        parallel = " — parallel" if len(task_ids) > 1 else ""
        lines.extend([f"### Layer {layer_number}{parallel}", ""])
        for task_id in task_ids:
            task = tasks[task_id]
            runtime = runtime_states.get(task_id)
            depends_on = ", ".join(task["depends_on"]) or "ENTRY"
            lines.extend(
                [
                    f"#### {task_id} — {task['title']}",
                    "",
                    f"- Type: {task['task_type']}",
                    f"- Depends on: {depends_on}",
                    "- Runtime status: "
                    + (
                        runtime.status.value
                        if runtime is not None
                        else "not started"
                    ),
                    "- Attempts: "
                    + (str(runtime.attempt_count) if runtime is not None else "0"),
                    "- Requirements: "
                    + (", ".join(task["requirement_refs"]) or "None"),
                    "- Acceptance criteria: "
                    + (", ".join(task["acceptance_criteria_refs"]) or "None"),
                    "- Risks: " + (", ".join(task["risk_refs"]) or "None"),
                    "- Ambiguities: "
                    + (", ".join(task["ambiguity_refs"]) or "None"),
                    f"- Description: {task['description']}",
                    "- Expected outputs: "
                    + (", ".join(task["expected_outputs"]) or "None"),
                    "",
                ]
            )
    lines.extend(
        [
            "## Deterministic graph semantics",
            "",
            "- ENTRY-ready: " + ", ".join(semantics["entry_ready_tasks"]),
            "- EXIT predecessors: "
            + ", ".join(semantics["exit_predecessor_tasks"]),
            "- Synchronization points: "
            + (", ".join(semantics["synchronization_points"]) or "None"),
            "- Topological order: " + ", ".join(semantics["topological_order"]),
            "- Required specification coverage: complete (FR/NFR/CON/AC)",
            "",
            "## Human task-graph review history",
            "",
        ]
    )
    _append_approval_history(lines, state.get("task_graph_review_history", []))
    return "\n".join(lines) + "\n"


def _execution_evidence(state: WorkflowState) -> dict[str, object]:
    execution = state["task_graph_execution"]
    return {
        "task_graph_execution": execution.model_dump(mode="json"),
        "requests": [
            request.model_dump(mode="json")
            for request in state.get("task_execution_requests", [])
        ],
        "results": [
            result.model_dump(mode="json")
            for result in state.get("task_execution_results", [])
        ],
        "validations": [
            validation.model_dump(mode="json")
            for validation in state.get("task_execution_validations", [])
        ],
        "failures": [
            failure.model_dump(mode="json")
            for failure in state.get("task_execution_failures", [])
        ],
        "recovery_decisions": [
            decision.model_dump(mode="json")
            for decision in state.get("task_execution_recovery_decisions", [])
        ],
    }


def _summary_markdown(
    state: WorkflowState, generated_filenames: tuple[str, ...]
) -> str:
    safe_stopped = state["workflow_status"] == "safe_stopped"
    execution = state.get("task_graph_execution")
    task_attempts = (
        sum(item.attempt_count for item in execution.task_states)
        if execution is not None
        else 0
    )
    task_count = len(execution.task_states) if execution is not None else 0
    retries = sum(
        decision.action.value == "RETRY"
        for decision in state.get("task_execution_recovery_decisions", [])
    )
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
        "- Approved requirement spec: "
        + state.get("approved_requirement_spec", {}).get("spec_id", "not reached"),
        "- Task planning: " + state.get("task_planning_status", "not reached"),
        "- Task-graph review: "
        + (state.get("task_graph_decision") or "not reached"),
        "- TaskGraph execution: "
        + (
            state["task_graph_execution"].status.value
            if state.get("task_graph_execution")
            else "not reached"
        ),
        f"- Task attempts: {task_attempts} across {task_count} tasks",
        f"- Retries performed: {retries}",
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
                (
                    "Execution evidence was retained for every attempted task."
                    if state.get("task_graph_execution")
                    else "No engineering task was executed."
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                "The governed V0.5 workflow executed the human-approved TaskGraph "
                "in deterministic scheduler order, canonicalized each semantic "
                "result, and allowed only application validation to settle tasks.",
                "",
            ]
        )

    lines.extend(["## Human Approval History", "", "### Requirement Analysis", ""])
    _append_approval_history(lines, state.get("requirement_review_history", []))
    lines.extend(["", "### Engineering Task Graph", ""])
    _append_approval_history(lines, state.get("task_graph_review_history", []))

    for heading, failures in (
        ("Requirement-analysis failures", state.get("requirement_analysis_failures", [])),
        ("Task-planning failures", state.get("task_planning_failures", [])),
    ):
        if failures:
            lines.extend(["", f"### {heading}", ""])
            for failure in failures:
                lines.extend(
                    [
                        f"{failure['sequence']}. Attempt {failure['attempt_number']}",
                        f"   - Revision: {failure['revision_number']}",
                        f"   - Retryable: {str(failure['retryable']).lower()}",
                        f"   - Reason: {failure['reason']}",
                    ]
                )

    lines.extend(
        [
            "",
            "## Generated artifacts",
            "",
            *(f"- `{filename}`" for filename in generated_filenames),
        ]
    )
    return "\n".join(lines) + "\n"


def _append_approval_history(
    lines: list[str], events: list[ApprovalEvent]
) -> None:
    if not events:
        lines.append("No decision recorded.")
        return
    for event in events:
        lines.extend(
            [
                f"{event['sequence']}. {event['decision']}",
                f"   - Revision: {event['revision_number']}",
            ]
        )
        if event["feedback"]:
            lines.append(f"   - Feedback: {event['feedback']}")
