"""End-to-end tests for the static governed TaskGraph execution loop."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import uuid4

from pytest import MonkeyPatch

import agentic_sdlc.nodes as nodes_module
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
from agentic_sdlc.task_executor import TaskExecutor, TaskExecutorError
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
        self._lock = threading.Lock()

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        with self._lock:
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
        self._lock = threading.Lock()

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        with self._lock:
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


class CoordinatedExecutor:
    """Prove bounded overlap while supporting deterministic peer outcomes."""

    model_name = "coordinated-executor"

    def __init__(
        self,
        *,
        outcomes: dict[str, tuple[str, ...]] | None = None,
        parallel_task_ids: tuple[str, ...] = ("TASK-002", "TASK-003"),
        reverse_completion: bool = False,
    ) -> None:
        self.outcomes = outcomes or {}
        self.parallel_task_ids = parallel_task_ids
        self.reverse_completion = reverse_completion
        self.barrier = threading.Barrier(len(parallel_task_ids))
        self.task_three_completed = threading.Event()
        self.lock = threading.Lock()
        self.calls: list[TaskExecutionRequest] = []
        self.completions: list[str] = []
        self.active = 0
        self.maximum_active = 0

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        with self.lock:
            self.calls.append(request)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        coordinated = (
            request.task_id in self.parallel_task_ids
            and request.attempt_number == 1
        )
        try:
            if coordinated:
                try:
                    self.barrier.wait(timeout=2)
                except threading.BrokenBarrierError as error:
                    raise AssertionError(
                        "Parallel task attempts did not overlap."
                    ) from error
            if self.reverse_completion and request.task_id == "TASK-002":
                if not self.task_three_completed.wait(timeout=2):
                    raise AssertionError("TASK-003 did not complete first.")

            configured = self.outcomes.get(request.task_id, ("valid",))
            outcome = configured[
                min(request.attempt_number - 1, len(configured) - 1)
            ]
            if outcome == "retryable_error":
                raise TaskExecutorError(
                    "Temporary deterministic provider failure.", retryable=True
                )
            if outcome == "terminal_error":
                raise TaskExecutorError(
                    "Deterministic configuration rejection.", retryable=False
                )
            if outcome == "unexpected_error":
                raise RuntimeError("provider-secret-token must be sanitized")
            return TaskExecutionResult(
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                task_id=request.task_id,
                summary=f"Executed {request.task_id}.",
                outputs=(
                    ArtifactOutput(
                        artifact_type=EngineeringArtifactType.DESIGN,
                        logical_name=request.task.expected_outputs[0],
                        content=(
                            ""
                            if outcome == "blank"
                            else f"Accepted output for {request.task_id}."
                        ),
                    ),
                ),
                assumptions=(),
                risks=(),
            )
        finally:
            with self.lock:
                self.completions.append(request.task_id)
                self.active -= 1
            if request.task_id == "TASK-003":
                self.task_three_completed.set()


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


def _three_branch_proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(
        tasks=[
            _task("foundation", depends_on=[]),
            _task("branch_one", depends_on=["foundation"]),
            _task("branch_two", depends_on=["foundation"]),
            _task("branch_three", depends_on=["foundation"]),
        ]
    )


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
    executor: TaskExecutor,
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


def test_static_loop_runs_bounded_fanout_wave_and_propagates_dependency_evidence() -> None:
    executor = DeterministicExecutor()

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SUCCEEDED
    )
    assert [request.task_id for request in result["task_execution_requests"]] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
    ]
    artifacts_by_task = {
        artifact.task_id: artifact for artifact in result["engineering_artifacts"]
    }
    requests_by_task = {
        request.task_id: request for request in result["task_execution_requests"]
    }
    assert tuple(
        artifact.artifact_id
        for artifact in requests_by_task["TASK-002"].dependency_artifacts
    ) == (artifacts_by_task["TASK-001"].artifact_id,)
    assert tuple(
        artifact.artifact_id
        for artifact in requests_by_task["TASK-003"].dependency_artifacts
    ) == (artifacts_by_task["TASK-001"].artifact_id,)
    assert tuple(
        artifact.task_id
        for artifact in requests_by_task["TASK-004"].dependency_artifacts
    ) == ("TASK-002", "TASK-003")
    assert [
        tuple(attempt.task_id for attempt in wave.task_attempts)
        for wave in result["task_execution_waves"]
    ] == [("TASK-001",), ("TASK-002", "TASK-003"), ("TASK-004",)]
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


def test_parallel_calls_overlap_while_persisted_evidence_stays_canonical() -> None:
    executor = CoordinatedExecutor(reverse_completion=True)

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert executor.maximum_active == 2
    assert executor.completions.index("TASK-003") < executor.completions.index(
        "TASK-002"
    )
    for field in (
        "task_execution_requests",
        "task_execution_results",
        "engineering_artifacts",
        "task_execution_validations",
    ):
        assert [
            item.task_id
            for item in result[field]
            if item.task_id in {"TASK-002", "TASK-003"}
        ] == ["TASK-002", "TASK-003"]
    assert tuple(
        attempt.task_id for attempt in result["task_execution_waves"][1].task_attempts
    ) == ("TASK-002", "TASK-003")
    assert [
        event.split()[3]
        for event in result["trace"]
        if event.startswith("[execute_task_graph_step] wave 2")
    ] == ["TASK-002", "TASK-003"]


def test_parallel_wave_cap_defers_third_ready_branch() -> None:
    executor = CoordinatedExecutor()

    result = _run_approved(_three_branch_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert executor.maximum_active == 2
    assert [
        tuple(attempt.task_id for attempt in wave.task_attempts)
        for wave in result["task_execution_waves"]
    ] == [
        ("TASK-001",),
        ("TASK-002", "TASK-003"),
        ("TASK-004",),
    ]
    assert all(len(wave.task_attempts) <= 2 for wave in result["task_execution_waves"])


def test_retryable_parallel_peer_retries_after_successful_peer_settles() -> None:
    executor = CoordinatedExecutor(
        outcomes={"TASK-002": ("blank", "valid")}
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "success"
    assert [
        tuple(
            (attempt.task_id, attempt.attempt_number)
            for attempt in wave.task_attempts
        )
        for wave in result["task_execution_waves"]
    ] == [
        (("TASK-001", 1),),
        (("TASK-002", 1), ("TASK-003", 1)),
        (("TASK-002", 2),),
        (("TASK-004", 1),),
    ]
    retry_request = next(
        request
        for request in result["task_execution_requests"]
        if request.task_id == "TASK-002" and request.attempt_number == 2
    )
    assert retry_request.retry_context is not None
    assert _statuses(result)["TASK-003"] is TaskExecutionStatus.SUCCEEDED
    join_request = result["task_execution_requests"][-1]
    assert tuple(
        (artifact.task_id, artifact.attempt_number)
        for artifact in join_request.dependency_artifacts
    ) == (("TASK-002", 2), ("TASK-003", 1))


def test_terminal_parallel_failure_retains_and_settles_successful_peer() -> None:
    executor = CoordinatedExecutor(
        outcomes={"TASK-002": ("terminal_error",)}
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )
    assert _statuses(result) == {
        "TASK-001": TaskExecutionStatus.SUCCEEDED,
        "TASK-002": TaskExecutionStatus.FAILED,
        "TASK-003": TaskExecutionStatus.SUCCEEDED,
        "TASK-004": TaskExecutionStatus.BLOCKED,
    }
    assert tuple(
        attempt.task_id for attempt in result["task_execution_waves"][1].task_attempts
    ) == ("TASK-002", "TASK-003")
    assert any(
        semantic_result.task_id == "TASK-003"
        for semantic_result in result["task_execution_results"]
    )
    assert any(
        artifact.task_id == "TASK-003" for artifact in result["engineering_artifacts"]
    )
    assert all(
        state.status is not TaskExecutionStatus.RUNNING
        for state in result["task_graph_execution"].task_states
    )
    assert not any(
        request.task_id == "TASK-004" for request in result["task_execution_requests"]
    )


def test_retryable_peer_remains_ready_when_terminal_peer_freezes_graph() -> None:
    executor = CoordinatedExecutor(
        outcomes={
            "TASK-002": ("retryable_error",),
            "TASK-003": ("terminal_error",),
        }
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    assert _statuses(result)["TASK-002"] is TaskExecutionStatus.READY
    assert _statuses(result)["TASK-003"] is TaskExecutionStatus.FAILED
    assert [
        (decision.task_id, decision.action)
        for decision in result["task_execution_recovery_decisions"]
    ] == [
        ("TASK-002", TaskExecutionRecoveryAction.RETRY),
        ("TASK-003", TaskExecutionRecoveryAction.FAIL_TASK),
    ]
    assert [
        request.task_id for request in result["task_execution_requests"]
    ].count("TASK-002") == 1
    assert all(
        state.status is not TaskExecutionStatus.RUNNING
        for state in result["task_graph_execution"].task_states
    )


def test_request_build_failure_does_not_abandon_valid_authorized_peer(
    monkeypatch: MonkeyPatch,
) -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task("first", depends_on=[]),
            _task("second", depends_on=[]),
        ]
    )
    state, _ = _direct_execution_state(proposal)
    executor = DeterministicExecutor()
    real_builder = nodes_module.build_task_execution_request

    def controlled_builder(*args: object, **kwargs: object) -> TaskExecutionRequest:
        if args[3] == "TASK-002":
            raise TaskExecutionContractError(
                "Controlled authoritative request-build failure."
            )
        return real_builder(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(nodes_module, "build_task_execution_request", controlled_builder)

    update = execute_task_graph_step(state, executor=executor)

    assert update["task_graph_execution"].status is TaskGraphExecutionStatus.FAILED
    assert _statuses(update) == {
        "TASK-001": TaskExecutionStatus.SUCCEEDED,
        "TASK-002": TaskExecutionStatus.FAILED,
    }
    assert tuple(
        attempt.task_id for attempt in update["task_execution_waves"][0].task_attempts
    ) == ("TASK-001", "TASK-002")
    assert [request.task_id for request in update["task_execution_requests"]] == [
        "TASK-001"
    ]
    assert update["task_execution_failures"][0].task_id == "TASK-002"
    assert update["task_execution_failures"][0].phase is (
        TaskExecutionFailurePhase.REQUEST_BUILD
    )
    assert [request.task_id for request in executor.calls] == ["TASK-001"]
    stopped = safe_stop({**state, **update})
    assert stopped["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )


def test_multiple_parallel_failures_are_both_retained_in_canonical_order() -> None:
    executor = CoordinatedExecutor(
        outcomes={
            "TASK-002": ("terminal_error",),
            "TASK-003": ("terminal_error",),
        }
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    assert [failure.task_id for failure in result["task_execution_failures"]] == [
        "TASK-002",
        "TASK-003",
    ]
    assert [
        decision.task_id for decision in result["task_execution_recovery_decisions"]
    ] == ["TASK-002", "TASK-003"]
    assert _statuses(result)["TASK-002"] is TaskExecutionStatus.FAILED
    assert _statuses(result)["TASK-003"] is TaskExecutionStatus.FAILED


def test_unexpected_custom_executor_exception_is_sanitized_after_peer_finishes() -> None:
    executor = CoordinatedExecutor(
        outcomes={"TASK-002": ("unexpected_error",)}
    )

    result = _run_approved(_fanout_fanin_proposal(), executor)

    assert result["workflow_status"] == "safe_stopped"
    failure = next(
        failure
        for failure in result["task_execution_failures"]
        if failure.task_id == "TASK-002"
    )
    assert failure.error_type == "TaskExecutorError"
    assert failure.message == "TaskExecutor raised an unexpected exception."
    assert "provider-secret-token" not in failure.model_dump_json()
    assert _statuses(result)["TASK-003"] is TaskExecutionStatus.SUCCEEDED


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
        (request.task_id, request.attempt_number)
        for request in result["task_execution_requests"]
    ] == [
        ("TASK-001", 1),
        ("TASK-002", 1),
        ("TASK-003", 1),
        ("TASK-002", 2),
        ("TASK-004", 1),
    ]
    task_two_artifacts = [
        artifact
        for artifact in result["engineering_artifacts"]
        if artifact.task_id == "TASK-002"
    ]
    join_request = result["task_execution_requests"][-1]
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
    assert [wave["wave_number"] for wave in evidence["waves"]] == [1, 2]
    assert [
        wave["task_attempts"][0]["attempt_number"] for wave in evidence["waves"]
    ] == [1, 2]
    assert len(evidence["failures"]) == 1
    assert len(evidence["recovery_decisions"]) == 1
    assert evidence["recovery_decisions"][0]["action"] == "RETRY"
    assert evidence["requests"][1]["retry_context"]["prior_attempt_number"] == 1
    assert "Task attempts: 2 across 1 tasks" in summary
    assert "Retries performed: 1" in summary
    assert "Execution waves: 2" in summary
    assert "Maximum parallel wave width: 1" in summary
    assert "Runtime status: SUCCEEDED" in task_graph
    assert "Attempts: 2" in task_graph
    assert "Execution waves: 1 (attempt 1), 2 (attempt 2)" in task_graph
