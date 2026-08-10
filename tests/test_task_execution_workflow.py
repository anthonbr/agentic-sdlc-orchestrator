"""End-to-end tests for the static governed TaskGraph execution loop."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from pytest import MonkeyPatch

from agentic_sdlc.artifacts import write_artifacts
from agentic_sdlc.llm import (
    FakeRequirementAnalysisClient,
    FakeTaskPlanningClient,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import build_approved_requirement_spec
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_execution import (
    TaskExecutionFailurePhase,
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionStatus,
    TaskGraphExecutionStatus,
    initialize_task_graph_execution,
    prepare_task_retry,
    start_task,
)
from agentic_sdlc.task_execution_contracts import (
    ArtifactOutput,
    EngineeringArtifactType,
    TaskExecutionContractError,
    TaskExecutionRequest,
    TaskExecutionResult,
)
from agentic_sdlc.task_executor import TaskExecutorError
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    TaskGraph,
    TaskType,
    normalize_and_validate_task_graph,
)
from agentic_sdlc.nodes import (
    _has_complete_final_execution_evidence,
    execute_task_graph_step,
    safe_stop,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow


class DeterministicExecutor:
    """Produce one correlated artifact per call and record bounded requests."""

    model_name = "deterministic-executor"

    def __init__(
        self,
        *,
        blank_content_for: str | None = None,
        error_for: str | None = None,
    ) -> None:
        self.blank_content_for = blank_content_for
        self.error_for = error_for
        self.calls: list[TaskExecutionRequest] = []

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        self.calls.append(request)
        if request.task_id == self.error_for:
            raise TaskExecutorError(
                "Deterministic provider failure.", retryable=False
            )
        dependency_ids = ", ".join(
            artifact.artifact_id for artifact in request.dependency_artifacts
        )
        content = (
            ""
            if request.task_id == self.blank_content_for
            else f"Output for {request.task_id}; dependencies: {dependency_ids or 'none'}."
        )
        return TaskExecutionResult(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            task_id=request.task_id,
            summary=f"Executed {request.task_id}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=EngineeringArtifactType.DESIGN,
                    logical_name=request.task.expected_outputs[0],
                    content=content,
                ),
            ),
            assumptions=(),
            risks=(),
        )


class ScriptedRecoveryExecutor:
    """Apply deterministic per-task attempt outcomes without network access."""

    model_name = "scripted-recovery-executor"

    def __init__(self, outcomes: dict[str, tuple[str, ...]]) -> None:
        self.outcomes = outcomes
        self.calls: list[TaskExecutionRequest] = []

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        self.calls.append(request)
        configured = self.outcomes.get(request.task_id, ("valid",))
        outcome = configured[min(request.attempt_number - 1, len(configured) - 1)]
        if outcome == "retryable_error":
            raise TaskExecutorError(
                "Temporary deterministic provider failure.", retryable=True
            )
        if outcome == "terminal_error":
            raise TaskExecutorError(
                "Deterministic configuration rejection.", retryable=False
            )
        result = TaskExecutionResult(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            task_id=request.task_id,
            summary=f"Executed {request.task_id} attempt {request.attempt_number}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=EngineeringArtifactType.DESIGN,
                    logical_name=(
                        " "
                        if outcome == "blank_name"
                        else request.task.expected_outputs[0]
                    ),
                    content=(
                        ""
                        if outcome == "blank"
                        else (
                            "REJECTED ARTIFACT CONTENT MUST REMAIN AUDIT ONLY."
                            if outcome == "blank_name"
                            else (
                                f"Accepted output for {request.task_id} attempt "
                                f"{request.attempt_number}."
                            )
                        )
                    ),
                ),
            ),
            assumptions=(),
            risks=(),
        )
        if outcome == "bad_correlation":
            return result.model_copy(update={"request_id": "wrong-request"})
        return result


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement="Produce governed URL-shortener engineering artifacts.",
        requirement_type="greenfield",
        functional_requirements=["Define governed URL creation behavior."],
        nonfunctional_requirements=[],
        constraints=[],
        ambiguities=["URL expiration behavior remains unspecified."],
        assumptions=["Generated engineering artifacts remain data only."],
        acceptance_criteria=["Every planned task produces reviewable output."],
        risks=["Inconsistent predecessor artifacts could break downstream work."],
        needs_clarification=True,
        confidence=0.9,
    )


def _task(
    key: str,
    *,
    depends_on: list[str],
    task_type: TaskType = TaskType.DESIGN,
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=key.replace("_", " ").title(),
        description=f"Produce the {key} engineering artifact.",
        task_type=task_type,
        depends_on=depends_on,
        requirement_refs=["FR-001"],
        acceptance_criteria_refs=["AC-001"],
        risk_refs=["RISK-001"],
        ambiguity_refs=["AMB-001"],
        expected_outputs=[f"{key}-output"],
    )


def _fanout_fanin_proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(
        tasks=[
            _task("foundation", depends_on=[]),
            _task("design_branch", depends_on=["foundation"]),
            _task(
                "test_branch",
                depends_on=["foundation"],
                task_type=TaskType.TEST,
            ),
            _task(
                "join_outputs",
                depends_on=["design_branch", "test_branch"],
                task_type=TaskType.DOCUMENTATION,
            ),
        ]
    )


def _linear_proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(
        tasks=[
            _task("upstream", depends_on=[]),
            _task("downstream", depends_on=["upstream"]),
        ]
    )


def _single_proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(tasks=[_task("only_task", depends_on=[])])


def _direct_execution_state(
    proposal: ProposedTaskGraph,
) -> tuple[WorkflowState, TaskGraph]:
    spec = build_approved_requirement_spec(
        _analysis(),
        source_analysis_revision=0,
        created_at="2026-08-10T12:00:00+00:00",
    )
    graph, _ = normalize_and_validate_task_graph(
        proposal,
        spec,
        version=1,
        created_at="2026-08-10T12:00:00+00:00",
    )
    state: WorkflowState = {
        "approved_requirement_spec": spec.model_dump(mode="json"),
        "approved_task_graph": graph.model_dump(mode="json"),
        "task_graph_execution": initialize_task_graph_execution(graph),
    }
    return state, graph


def _run_approved(
    proposal: ProposedTaskGraph,
    executor: DeterministicExecutor,
) -> WorkflowState:
    workflow = build_workflow(
        FakeRequirementAnalysisClient([_analysis()]),
        FakeTaskPlanningClient([proposal]),
        executor,
    )
    thread_id = uuid4().hex
    requirement_review = run_workflow(
        demo_input(), thread_id=thread_id, workflow=workflow
    )
    assert requirement_review["__interrupt__"][0].value["stage"] == (
        "requirement_analysis_review"
    )
    graph_review = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )
    assert "task_graph_execution" not in graph_review
    assert graph_review["__interrupt__"][0].value["stage"] == "task_graph_review"
    return resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )


def _statuses(state: WorkflowState) -> dict[str, TaskExecutionStatus]:
    return {
        item.task_id: item.status
        for item in state["task_graph_execution"].task_states
    }


def test_compiled_topology_uses_one_fixed_loop_and_no_dynamic_task_nodes() -> None:
    workflow = build_workflow(
        FakeRequirementAnalysisClient([_analysis()]),
        FakeTaskPlanningClient([_fanout_fanin_proposal()]),
        DeterministicExecutor(),
    )

    nodes = set(workflow.get_graph().nodes)
    assert "initialize_task_graph_execution" in nodes
    assert "execute_task_graph_step" in nodes
    assert "architecture_task" not in nodes
    assert "test_plan_task" not in nodes
    assert "synchronize" not in nodes
    assert not any(node.startswith("TASK-") for node in nodes)
    assert {
        "architecture",
        "test_plan",
        "synchronization_complete",
    }.isdisjoint(WorkflowState.__annotations__)


def test_static_loop_serializes_fanout_fanin_and_propagates_dependency_evidence() -> None:
    executor = DeterministicExecutor()

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SUCCEEDED
    )
    assert [request.task_id for request in executor.calls] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
    ]
    assert [request.task_id for request in result["task_execution_requests"]] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
    ]
    artifacts_by_task = {
        artifact.task_id: artifact for artifact in result["engineering_artifacts"]
    }
    assert tuple(
        artifact.artifact_id for artifact in executor.calls[1].dependency_artifacts
    ) == (artifacts_by_task["TASK-001"].artifact_id,)
    assert tuple(
        artifact.artifact_id for artifact in executor.calls[2].dependency_artifacts
    ) == (artifacts_by_task["TASK-001"].artifact_id,)
    assert tuple(
        artifact.task_id for artifact in executor.calls[3].dependency_artifacts
    ) == ("TASK-002", "TASK-003")
    assert all(
        validation.passed
        for validation in result["task_execution_validations"]
    )
    for request, semantic_result, validation in zip(
        result["task_execution_requests"],
        result["task_execution_results"],
        result["task_execution_validations"],
        strict=True,
    ):
        assert semantic_result.request_id == request.request_id
        assert semantic_result.attempt_id == request.attempt_id
        assert validation.request_id == request.request_id
        assert validation.attempt_id == request.attempt_id
        assert validation.task_id == request.task_id
    assert set(_statuses(result).values()) == {TaskExecutionStatus.SUCCEEDED}
    assert result["exit_gate_passed"] is True
    assert sum(
        event.startswith("[execute_task_graph_step]")
        for event in result["trace"]
    ) == 4


def test_retry_budget_exhaustion_retains_every_failed_validation_attempt() -> None:
    executor = DeterministicExecutor(blank_content_for="TASK-001")

    result = _run_approved(_linear_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )
    assert _statuses(result) == {
        "TASK-001": TaskExecutionStatus.FAILED,
        "TASK-002": TaskExecutionStatus.BLOCKED,
    }
    assert len(executor.calls) == 3
    assert len(result["task_execution_requests"]) == 3
    assert len(result["task_execution_results"]) == 3
    assert len(result["engineering_artifacts"]) == 3
    assert len(result["task_execution_validations"]) == 3
    assert all(
        not validation.passed
        for validation in result["task_execution_validations"]
    )
    assert result.get("task_execution_failures", []) == []
    decisions = result["task_execution_recovery_decisions"]
    assert [decision.action for decision in decisions] == [
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.FAIL_TASK,
    ]
    assert decisions[-1].retryable is True
    assert "exhausted" in decisions[-1].reason
    assert [request.attempt_number for request in executor.calls] == [1, 2, 3]
    assert executor.calls[1].retry_context is not None
    assert executor.calls[2].retry_context is not None
    assert "artifact contents" in " ".join(
        result["task_execution_validations"][0].errors
    ).casefold()


def test_executor_error_retains_request_and_failure_then_safe_stops_once() -> None:
    executor = DeterministicExecutor(error_for="TASK-001")

    result = _run_approved(_linear_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )
    assert _statuses(result) == {
        "TASK-001": TaskExecutionStatus.FAILED,
        "TASK-002": TaskExecutionStatus.BLOCKED,
    }
    assert len(executor.calls) == 1
    assert len(result["task_execution_requests"]) == 1
    assert result.get("task_execution_results", []) == []
    assert result.get("engineering_artifacts", []) == []
    assert result.get("task_execution_validations", []) == []
    failure = result["task_execution_failures"][0]
    assert failure.phase is TaskExecutionFailurePhase.EXECUTOR
    assert failure.request_id == result["task_execution_requests"][0].request_id
    assert failure.attempt_id == result["task_execution_requests"][0].attempt_id
    assert failure.error_type == "TaskExecutorError"
    assert "Deterministic provider failure" in failure.message
    decision = result["task_execution_recovery_decisions"][0]
    assert decision.retryable is False
    assert decision.action is TaskExecutionRecoveryAction.FAIL_TASK


def test_retryable_validation_failure_then_success_preserves_audit_only_artifact() -> None:
    executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("blank", "valid")}
    )

    result = _run_approved(_linear_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert result["exit_gate_passed"] is True
    assert [
        (request.task_id, request.attempt_number) for request in executor.calls
    ] == [("TASK-001", 1), ("TASK-001", 2), ("TASK-002", 1)]
    first_request, second_request, downstream_request = executor.calls
    assert first_request.request_id != second_request.request_id
    assert first_request.attempt_id != second_request.attempt_id
    assert second_request.retry_context is not None
    assert second_request.retry_context.prior_request_id == first_request.request_id
    assert "Blank artifact contents" in second_request.retry_context.feedback
    task_one_artifacts = [
        artifact
        for artifact in result["engineering_artifacts"]
        if artifact.task_id == "TASK-001"
    ]
    assert [artifact.attempt_number for artifact in task_one_artifacts] == [1, 2]
    assert task_one_artifacts[0].artifact_id != task_one_artifacts[1].artifact_id
    assert task_one_artifacts[0].lineage_id == task_one_artifacts[1].lineage_id
    assert task_one_artifacts[0].artifact_id not in (
        second_request.retry_context.model_dump_json()
    )
    assert tuple(
        artifact.artifact_id
        for artifact in downstream_request.dependency_artifacts
    ) == (task_one_artifacts[1].artifact_id,)
    assert task_one_artifacts[0].artifact_id not in {
        artifact.artifact_id
        for artifact in downstream_request.dependency_artifacts
    }
    decisions = result["task_execution_recovery_decisions"]
    assert len(decisions) == 1
    assert decisions[0].failure_kind is TaskExecutionRecoveryFailureKind.VALIDATION
    assert decisions[0].action is TaskExecutionRecoveryAction.RETRY
    assert result["task_execution_validations"][0].passed is False
    assert all(
        validation.passed
        for validation in result["task_execution_validations"][1:]
    )


def test_retryable_executor_failure_then_success_has_no_fabricated_attempt_output() -> None:
    executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("retryable_error", "valid")}
    )

    result = _run_approved(_single_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert len(executor.calls) == 2
    assert len(result["task_execution_requests"]) == 2
    assert len(result["task_execution_failures"]) == 1
    assert len(result["task_execution_results"]) == 1
    assert len(result["engineering_artifacts"]) == 1
    assert len(result["task_execution_validations"]) == 1
    assert executor.calls[1].retry_context is not None
    assert executor.calls[1].retry_context.failure_kind is (
        TaskExecutionRecoveryFailureKind.EXECUTOR
    )
    assert result["task_execution_recovery_decisions"][0].action is (
        TaskExecutionRecoveryAction.RETRY
    )
    assert result["exit_gate_passed"] is True


def test_correlation_failure_retries_but_other_canonicalization_failure_does_not(
    monkeypatch: MonkeyPatch,
) -> None:
    correlation_executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("bad_correlation", "valid")}
    )
    recovered = _run_approved(_single_proposal(), correlation_executor)

    assert recovered["workflow_status"] == "success"
    assert len(correlation_executor.calls) == 2
    assert recovered["task_execution_recovery_decisions"][0].retryable is True
    assert recovered["task_execution_failures"][0].phase is (
        TaskExecutionFailurePhase.CANONICALIZATION
    )

    state, _ = _direct_execution_state(_single_proposal())
    terminal_executor = ScriptedRecoveryExecutor({})

    def fail_application_invariant(*args: object, **kwargs: object) -> object:
        raise TaskExecutionContractError("Application timestamp invariant failed.")

    monkeypatch.setattr(
        "agentic_sdlc.nodes.canonicalize_execution_result",
        fail_application_invariant,
    )
    terminal = execute_task_graph_step(state, executor=terminal_executor)
    assert terminal["task_graph_execution"].status is (
        TaskGraphExecutionStatus.FAILED
    )
    assert terminal["task_execution_recovery_decisions"][0].retryable is False
    assert terminal["task_execution_recovery_decisions"][0].action is (
        TaskExecutionRecoveryAction.FAIL_TASK
    )
    assert len(terminal_executor.calls) == 1


def test_missing_retry_history_is_non_retryable_request_build_failure() -> None:
    state, graph = _direct_execution_state(_single_proposal())
    running = start_task(
        graph, state["task_graph_execution"], "TASK-001"
    )
    state["task_graph_execution"] = prepare_task_retry(
        graph, running, "TASK-001"
    )
    executor = ScriptedRecoveryExecutor({})

    update = execute_task_graph_step(state, executor=executor)
    terminal_state = {**state, **update}
    stopped = safe_stop(terminal_state)

    assert executor.calls == []
    failure = update["task_execution_failures"][0]
    decision = update["task_execution_recovery_decisions"][0]
    assert failure.phase is TaskExecutionFailurePhase.REQUEST_BUILD
    assert failure.request_id is None
    assert decision.failure_kind is TaskExecutionRecoveryFailureKind.REQUEST_BUILD
    assert decision.retryable is False
    assert decision.action is TaskExecutionRecoveryAction.FAIL_TASK
    assert stopped["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )


def test_fanout_fanin_retry_preserves_order_and_uses_only_final_artifact() -> None:
    executor = ScriptedRecoveryExecutor(
        {"TASK-002": ("blank", "valid")}
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert [
        (request.task_id, request.attempt_number) for request in executor.calls
    ] == [
        ("TASK-001", 1),
        ("TASK-002", 1),
        ("TASK-002", 2),
        ("TASK-003", 1),
        ("TASK-004", 1),
    ]
    task_two_artifacts = [
        artifact
        for artifact in result["engineering_artifacts"]
        if artifact.task_id == "TASK-002"
    ]
    join_request = executor.calls[-1]
    assert tuple(
        (artifact.task_id, artifact.attempt_number)
        for artifact in join_request.dependency_artifacts
    ) == (("TASK-002", 2), ("TASK-003", 1))
    assert task_two_artifacts[0].artifact_id not in {
        artifact.artifact_id
        for artifact in join_request.dependency_artifacts
    }
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SUCCEEDED
    )


def test_retry_aware_exit_gate_requires_exact_final_attempt_evidence() -> None:
    executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("retryable_error", "valid")}
    )
    result = _run_approved(_single_proposal(), executor)

    assert _has_complete_final_execution_evidence(result) is True
    assert result["task_execution_failures"]
    for field in (
        "task_execution_requests",
        "task_execution_results",
        "task_execution_validations",
        "engineering_artifacts",
    ):
        incomplete = {**result, field: result[field][:-1]}
        assert _has_complete_final_execution_evidence(incomplete) is False

    failed_validation_executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("blank", "valid")}
    )
    recovered = _run_approved(_single_proposal(), failed_validation_executor)
    assert recovered["task_execution_validations"][0].passed is False
    assert _has_complete_final_execution_evidence(recovered) is True


def test_execution_audit_artifacts_include_recovery_history(
    tmp_path: Path,
) -> None:
    executor = ScriptedRecoveryExecutor(
        {"TASK-001": ("retryable_error", "valid")}
    )
    result = _run_approved(_single_proposal(), executor)

    write_artifacts(result, tmp_path)
    evidence = json.loads((tmp_path / "task_execution.json").read_text())
    summary = (tmp_path / "summary.md").read_text()
    task_graph = (tmp_path / "task_graph.md").read_text()

    assert len(evidence["requests"]) == 2
    assert len(evidence["failures"]) == 1
    assert len(evidence["recovery_decisions"]) == 1
    assert evidence["recovery_decisions"][0]["action"] == "RETRY"
    assert evidence["requests"][1]["retry_context"]["prior_attempt_number"] == 1
    assert "Task attempts: 2 across 1 tasks" in summary
    assert "Retries performed: 1" in summary
    assert "Runtime status: SUCCEEDED" in task_graph
    assert "Attempts: 2" in task_graph
