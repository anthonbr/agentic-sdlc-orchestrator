"""Write compact, reviewable artifacts after a terminal governed workflow run."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.brownfield_baseline import brownfield_baseline_from_value
from agentic_sdlc.brownfield_context import brownfield_codebase_context_from_value
from agentic_sdlc.reliability_metrics import (
    ReliabilityMetricsArtifact,
    RunReliabilityMetrics,
    ScenarioReliabilityMetrics,
    derive_reliability_metrics,
)
from agentic_sdlc.state import ApprovalEvent, TaskGraphData, WorkflowState
from agentic_sdlc.task_execution import TaskGraphExecutionState
from agentic_sdlc.workspace_integration_contracts import TaskAttemptExitDecision
from agentic_sdlc.workspace_mutation import WorkspaceMutationResult


ARTIFACT_FILENAMES = (
    "requirements.json",
    "requirement_analysis.md",
    "approved_requirement_spec.json",
    "task_graph.json",
    "task_graph.md",
    "task_execution.json",
    "workspace_execution.json",
    "engineering_artifacts.json",
    "summary.md",
)
LEGACY_ARTIFACT_FILENAMES = (
    "architecture.md",
    "test_plan.md",
    "decomposition.json",
    "implementation_plan.md",
)
RELIABILITY_METRICS_FILENAME = "reliability_metrics.json"
RELIABILITY_METRICS_SCHEMA_VERSION = "reliability-metrics-v1"
RELIABILITY_SCENARIOS = (
    ("greenfield", "demo-run", "artifacts/demo-run/"),
    ("brownfield", "brownfield-demo-run", "artifacts/brownfield-demo-run/"),
    (
        "ambiguous requirement",
        "ambiguity-demo-run",
        "artifacts/ambiguity-demo-run/",
    ),
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

    requirement_evidence: dict[str, object] = {
        "project_name": state["project_name"],
        "project_delivery_policy": state["project_delivery_policy"],
        "raw_requirement": state["raw_requirement"],
        "submitted_requirements": state["requirements"],
        "normalized_requirements": state["normalized_requirements"],
    }
    if "requirement_submission" in state:
        requirement_evidence["requirement_submission"] = state[
            "requirement_submission"
        ]
    _write_json(paths["requirements.json"], requirement_evidence)
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
    if "workspace_execution.json" in generated:
        _write_json(
            paths["workspace_execution.json"],
            _workspace_execution_evidence(state),
        )
    paths["summary.md"].write_text(
        _summary_markdown(state, generated), encoding="utf-8"
    )
    return [paths[filename] for filename in generated]


def write_reliability_metrics_artifact(
    artifacts_dir: Path, output_path: Path | None = None
) -> Path:
    """Write a deterministic index of independent checked-in scenario metrics."""

    runs = tuple(
        ScenarioReliabilityMetrics(
            scenario=scenario,
            evidence_root=evidence_root,
            metrics=_load_run_reliability_metrics(artifacts_dir / directory),
        )
        for scenario, directory, evidence_root in RELIABILITY_SCENARIOS
    )
    artifact = ReliabilityMetricsArtifact(
        schema_version=RELIABILITY_METRICS_SCHEMA_VERSION,
        runs=runs,
    )
    path = output_path or artifacts_dir / RELIABILITY_METRICS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, artifact.model_dump(mode="json"))
    return path


def _load_run_reliability_metrics(evidence_dir: Path) -> RunReliabilityMetrics:
    task_evidence = _read_json_object(evidence_dir / "task_execution.json")
    workspace_evidence = _read_json_object(evidence_dir / "workspace_execution.json")
    execution_value = task_evidence.get("task_graph_execution")
    decision_values = workspace_evidence.get("task_attempt_exit_decisions")
    mutation_values = workspace_evidence.get("mutations")
    if not isinstance(decision_values, list) or not isinstance(mutation_values, list):
        raise ValueError(
            f"Reliability evidence lists are missing from {evidence_dir}."
        )

    execution = TaskGraphExecutionState.model_validate_json(
        json.dumps(execution_value)
    )
    decisions = tuple(
        TaskAttemptExitDecision.model_validate_json(json.dumps(value))
        for value in decision_values
    )
    mutations = tuple(
        WorkspaceMutationResult.model_validate_json(json.dumps(value))
        for value in mutation_values
    )
    return derive_reliability_metrics(
        task_graph_execution=execution,
        task_attempt_exit_decisions=decisions,
        workspace_mutation_results=mutations,
    )


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _safe_stop_filenames(state: WorkflowState) -> tuple[str, ...]:
    filenames = ["requirements.json"]
    if state.get("requirement_analysis"):
        filenames.append("requirement_analysis.md")
    if state.get("approved_requirement_spec"):
        filenames.append("approved_requirement_spec.json")
    if state.get("candidate_task_graph"):
        filenames.extend(["task_graph.json", "task_graph.md"])
    if state.get("task_graph_execution"):
        filenames.extend(
            [
                "task_execution.json",
                "workspace_execution.json",
                "engineering_artifacts.json",
            ]
        )
    filenames.append("summary.md")
    return tuple(filenames)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _requirement_analysis_markdown(state: WorkflowState) -> str:
    analysis = state["requirement_analysis"]
    readiness = state.get("requirement_planning_readiness")
    readiness_status = readiness["status"] if readiness else "not determined"
    readiness_reason = (
        readiness["reason_code"] or "None" if readiness else "not determined"
    )
    lines = [
        "# Requirement Analysis",
        "",
        *_requirement_input_markdown(state),
        "## Current validated analysis",
        "",
        f"- Requirement type: {analysis['requirement_type']}",
        f"- Needs clarification: {str(analysis['needs_clarification']).lower()}",
        f"- Planning readiness: {readiness_status}",
        f"- Readiness reason: {readiness_reason}",
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
        record_readiness = record.get("planning_readiness")
        record_readiness_status = (
            record_readiness["status"] if record_readiness else "not recorded"
        )
        record_readiness_reason = (
            record_readiness["reason_code"] or "None"
            if record_readiness
            else "not recorded"
        )
        lines.extend(
            [
                f"{record['sequence']}. Revision {record['revision_number']}",
                f"   - Attempt: {record['attempt_number']}",
                f"   - Prompt: {record['prompt_version']}",
                f"   - Model: {record['model_name']}",
                f"   - Planning readiness: {record_readiness_status}",
                f"   - Readiness reason: {record_readiness_reason}",
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


def _requirement_input_markdown(state: WorkflowState) -> list[str]:
    submission = state.get("requirement_submission")
    if submission is None:
        return [
            "## Original requirement",
            "",
            *(f"> {line}" for line in state["raw_requirement"].splitlines()),
            "",
        ]

    original_text = submission["original_text"]
    normalized_text = submission["normalized_text"]
    lines = [
        "## Original submitted requirement",
        "",
        *(f"> {line}" for line in original_text.splitlines()),
        "",
    ]
    if normalized_text != original_text:
        lines.extend(
            [
                "## Normalized workflow requirement",
                "",
                "The following normalized requirement text entered "
                "Requirement Analysis:",
                "",
                *(f"> {line}" for line in normalized_text.splitlines()),
                "",
            ]
        )
    return lines


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
    wave_memberships: dict[str, list[str]] = {}
    for wave in state.get("task_execution_waves", []):
        for attempt in wave.task_attempts:
            wave_memberships.setdefault(attempt.task_id, []).append(
                f"{wave.wave_number} (attempt {attempt.attempt_number})"
            )
    lines = [
        "# Engineering Task Dependency Graph",
        "",
        f"- Graph: {graph['graph_id']}",
        f"- Version: {graph['version']}",
        f"- Requirement specification: {graph['requirement_spec_id']}",
        f"- Project delivery policy: {graph['delivery_policy']['mode']}",
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
                    f"- Materialization policy: {task['materialization_policy']}",
                    "- Delivery roles: "
                    + (", ".join(task["deliverable_roles"]) or "None"),
                    "- Required validations: "
                    + (
                        ", ".join(
                            item["profile"]
                            for item in task.get("required_validations", [])
                        )
                        or "None"
                    ),
                    f"- Depends on: {depends_on}",
                    "- Runtime status: "
                    + (
                        runtime.status.value
                        if runtime is not None
                        else "not started"
                    ),
                    "- Attempts: "
                    + (str(runtime.attempt_count) if runtime is not None else "0"),
                    "- Execution waves: "
                    + (", ".join(wave_memberships.get(task_id, ())) or "None"),
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
        "waves": [
            wave.model_dump(mode="json")
            for wave in state.get("task_execution_waves", [])
        ],
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
        "validation_executions": [
            item.model_dump(mode="json")
            for item in state.get("task_validation_execution_evidence", [])
        ],
        "validation_provisioning": [
            item.model_dump(mode="json")
            for item in state.get("task_validation_provisioning_evidence", [])
        ],
        "final_workspace_validation_executions": [
            item.model_dump(mode="json")
            for item in state.get(
                "final_workspace_validation_execution_evidence", []
            )
        ],
        "final_workspace_validation_provisioning": [
            item.model_dump(mode="json")
            for item in state.get(
                "final_workspace_validation_provisioning_evidence", []
            )
        ],
    }


def _workspace_execution_evidence(state: WorkflowState) -> dict[str, object]:
    session = state.get("governed_workspace_session")
    brownfield_baseline = state.get("brownfield_baseline")
    brownfield_codebase_context = state.get("brownfield_codebase_context")
    readiness = state.get("project_readiness_validation")
    evidence: dict[str, object] = {
        "session": session.model_dump(mode="json") if session is not None else None,
        "snapshots": [
            item.model_dump(mode="json")
            for item in state.get("workspace_snapshots", [])
        ],
        "waves": [
            item.model_dump(mode="json")
            for item in state.get("workspace_execution_waves", [])
        ],
        "bound_requests": [
            item.model_dump(mode="json")
            for item in state.get("workspace_bound_task_execution_requests", [])
        ],
        "materialization_intents": [
            item.model_dump(mode="json")
            for item in state.get("artifact_materialization_intents", [])
        ],
        "materialization_validations": [
            item.model_dump(mode="json")
            for item in state.get("artifact_materialization_validations", [])
        ],
        "change_sets": [
            item.model_dump(mode="json")
            for item in state.get("workspace_change_sets", [])
        ],
        "change_set_validations": [
            item.model_dump(mode="json")
            for item in state.get("workspace_change_set_validations", [])
        ],
        "conflicts": [
            item.model_dump(mode="json")
            for item in state.get("workspace_conflict_evidence", [])
        ],
        "mutations": [
            item.model_dump(mode="json")
            for item in state.get("workspace_mutation_results", [])
        ],
        "task_attempt_exit_decisions": [
            item.model_dump(mode="json")
            for item in state.get("task_attempt_exit_decisions", [])
        ],
        "project_readiness": (
            readiness.model_dump(mode="json") if readiness is not None else None
        ),
    }
    if brownfield_baseline is not None:
        evidence["brownfield_baseline"] = brownfield_baseline_from_value(
            brownfield_baseline
        ).model_dump(mode="json")
    if brownfield_codebase_context is not None:
        evidence["brownfield_codebase_context"] = (
            brownfield_codebase_context_from_value(
                brownfield_codebase_context
            ).model_dump(mode="json")
        )
    return evidence


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
    waves = state.get("task_execution_waves", [])
    maximum_wave_width = max(
        (len(wave.task_attempts) for wave in waves), default=0
    )
    session = state.get("governed_workspace_session")
    delivery_policy = state.get("project_delivery_policy", {}).get(
        "mode", "ENGINEERING_ARTIFACTS"
    )
    readiness = state.get("project_readiness_validation")
    validation_evidence = state.get("task_validation_execution_evidence", [])
    provisioning_evidence = state.get(
        "task_validation_provisioning_evidence", []
    )
    final_validation_evidence = state.get(
        "final_workspace_validation_execution_evidence", []
    )
    final_provisioning_evidence = state.get(
        "final_workspace_validation_provisioning_evidence", []
    )
    final_validation_evidence_ids = {
        item.evidence_id for item in final_validation_evidence
    }
    task_required_validation_count = sum(
        len(task.get("required_validations", []))
        for task in state.get("approved_task_graph", {}).get("tasks", [])
    )
    final_required_validation_count = (
        readiness.final_workspace_validation_required_count
        if readiness is not None
        else 0
    )
    required_validation_count = (
        task_required_validation_count + final_required_validation_count
    )
    successful_exit_evidence_ids = {
        evidence_id
        for decision in state.get("task_attempt_exit_decisions", [])
        if decision.disposition.value == "SUCCEED_TASK"
        for evidence_id in decision.evidence_ids
    }
    passed_validation_count = sum(
        item.passed and item.evidence_id in successful_exit_evidence_ids
        for item in validation_evidence
    ) + sum(item.passed for item in final_validation_evidence)
    successful_pytest_evidence = tuple(
        item
        for item in (*validation_evidence, *final_validation_evidence)
        if item.profile.value == "PYTHON_PYTEST"
        and item.passed
        and (
            item.evidence_id in final_validation_evidence_ids
            or item.evidence_id in successful_exit_evidence_ids
        )
    )
    successful_compile_evidence = tuple(
        item
        for item in (*validation_evidence, *final_validation_evidence)
        if item.profile.value == "PYTHON_COMPILE"
        and item.passed
        and (
            item.evidence_id in final_validation_evidence_ids
            or item.evidence_id in successful_exit_evidence_ids
        )
    )
    mutations = state.get("workspace_mutation_results", [])
    materialized_changes = tuple(
        f"{change.path} ({change.operation.value})"
        for change_set in state.get("workspace_change_sets", [])
        if any(
            result.change_set_id == change_set.change_set_id
            and result.status.value == "APPLIED"
            for result in mutations
        )
        for change in change_set.file_changes
    )
    mutation_outcomes = tuple(result.status.value for result in mutations)
    conflict_count = sum(
        item.analysis.has_conflicts
        for item in state.get("workspace_conflict_evidence", [])
    )
    rollback_count = sum(
        outcome in {"ROLLED_BACK", "ROLLBACK_FAILED"}
        for outcome in mutation_outcomes
    )
    lines = [
        "# Workflow Summary",
        "",
        f"- Project: {state['project_name']}",
        f"- Project delivery policy: {delivery_policy}",
        f"- Workflow result: {state['workflow_status']}",
        f"- Entry gate: {'passed' if state['entry_gate_passed'] else 'failed'}",
        "- Requirement analysis: "
        + state.get("requirement_analysis_status", "not reached"),
        "- Requirement planning readiness: "
        + state.get("requirement_planning_readiness", {}).get(
            "status", "not reached"
        ),
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
        f"- Execution waves: {len(waves)}",
        f"- Maximum parallel wave width: {maximum_wave_width}",
        "- Workspace integrity: "
        + (session.integrity_status.value if session is not None else "not reached"),
        "- Final authoritative workspace snapshot: "
        + (
            session.authoritative_snapshot_id
            if session is not None
            else "not reached"
        ),
        f"- Workspace mutations: {len(mutations)}",
        "- Workspace mutation outcomes: "
        + (", ".join(mutation_outcomes) or "None"),
        f"- Conflicting wave reconciliations: {conflict_count}",
        f"- Rollback outcomes: {rollback_count}",
        "- Materialized desired paths: "
        + (", ".join(sorted(set(materialized_changes))) or "None"),
        f"- Governed required validations: {passed_validation_count} passed / "
        f"{required_validation_count} required",
        "- Planner-requested task validations: "
        f"{task_required_validation_count} required",
        "- Application-required final-workspace validations: "
        f"{final_required_validation_count} required",
        "- PYTHON_COMPILE validation executed: "
        + ("yes" if successful_compile_evidence else "no"),
        "- PYTHON_PYTEST validation executed: "
        + ("yes" if successful_pytest_evidence else "no"),
        "- Dependencies provisioned for validation: "
        + (
            "yes"
            if provisioning_evidence or final_provisioning_evidence
            else "no"
        ),
        "- Generated code/tests executed: "
        + ("yes" if successful_pytest_evidence else "no"),
        "- Generated tests executed: "
        + ("yes" if successful_pytest_evidence else "no"),
        "- Generated application executed: no",
        "- Benchmarks executed: no",
        "- Project readiness: "
        + (
            "not reached"
            if readiness is None
            else ("passed" if readiness.passed else "failed")
        ),
        "- Exit gate: "
        + (
            ("failed" if readiness is not None else "not reached")
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
                "The governed workflow executed bounded READY waves from the "
                "human-approved TaskGraph, joined concurrent executor calls, "
                "canonicalized and reconciled results in deterministic scheduler "
                "order, applied eligible isolated-workspace mutations serially, "
                "and allowed only the complete governed exit gate to settle tasks.",
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
