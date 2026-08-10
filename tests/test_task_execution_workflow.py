"""End-to-end tests for the static governed TaskGraph execution loop."""

from __future__ import annotations

from uuid import uuid4

from agentic_sdlc.llm import (
    FakeRequirementAnalysisClient,
    FakeTaskPlanningClient,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_execution import (
    TaskExecutionFailurePhase,
    TaskExecutionStatus,
    TaskGraphExecutionStatus,
)
from agentic_sdlc.task_execution_contracts import (
    ArtifactOutput,
    EngineeringArtifactType,
    TaskExecutionRequest,
    TaskExecutionResult,
)
from agentic_sdlc.task_executor import TaskExecutorError
from agentic_sdlc.task_graph import ProposedTask, ProposedTaskGraph, TaskType
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
            raise TaskExecutorError("Deterministic provider failure.")
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


def test_validation_failure_retains_evidence_and_safe_stops_without_downstream() -> None:
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
    assert len(executor.calls) == 1
    assert len(result["task_execution_requests"]) == 1
    assert len(result["task_execution_results"]) == 1
    assert len(result["engineering_artifacts"]) == 1
    assert result["task_execution_validations"][0].passed is False
    assert result.get("task_execution_failures", []) == []
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
