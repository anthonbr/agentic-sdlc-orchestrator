"""End-to-end tests for the static governed TaskGraph execution loop."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import uuid4

from pytest import MonkeyPatch, mark

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
    ready_task_ids,
    start_task,
)
from agentic_sdlc.task_execution_contracts import (
    ArtifactMaterializationProposal,
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
    TaskMaterializationPolicy,
    TaskType,
    normalize_and_validate_task_graph,
)
from agentic_sdlc.nodes import (
    _has_complete_final_execution_evidence,
    execute_task_graph_step,
    safe_stop,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationIssueCode,
    ArtifactMaterializationValidationIssue,
    WorkspaceChangeOperation,
    WorkspaceChangeSet,
    WorkspaceSnapshot,
)
from agentic_sdlc.workspace_integration import (
    DeterministicRepositoryContextPathProvider,
    GovernedWorkspaceRuntime,
    WorkspaceIntegrationError,
    WorkspaceIntegrationIssueCode,
    establish_governed_workspace_session,
)
from agentic_sdlc.workspace_integration_contracts import (
    GovernedWorkspaceSession,
    TaskAttemptExitDisposition,
    TaskAttemptExitDecision,
    WorkspaceBoundTaskExecutionRequest,
    WorkspaceDispatchMode,
    WorkspaceExecutionWave,
    WorkspaceIntegrityStatus,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationIssue,
    WorkspaceMutationIssueCode,
    WorkspaceMutationResult,
    WorkspaceMutationStatus,
)
from agentic_sdlc.workspace_runtime import snapshot_isolated_workspace


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
        self.calls: list[WorkspaceBoundTaskExecutionRequest] = []
        self._lock = threading.Lock()

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
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
        self.calls: list[WorkspaceBoundTaskExecutionRequest] = []
        self._lock = threading.Lock()

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
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
        self.calls: list[WorkspaceBoundTaskExecutionRequest] = []
        self.completions: list[str] = []
        self.active = 0
        self.maximum_active = 0

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
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


class MaterializingExecutor:
    """Propose deterministic desired files while recording exact bindings."""

    model_name = "materializing-executor"

    def __init__(
        self,
        targets: dict[str, str],
        *,
        contents: dict[str, str] | None = None,
    ) -> None:
        self.targets = targets
        self.contents = contents or {}
        self.calls: list[WorkspaceBoundTaskExecutionRequest] = []
        self.lock = threading.Lock()

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        with self.lock:
            self.calls.append(request)
        target_path = self.targets.get(request.task_id)
        return TaskExecutionResult(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            task_id=request.task_id,
            summary=f"Prepared desired state for {request.task_id}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=EngineeringArtifactType.TEST,
                    logical_name=f"semantic-{request.task_id}",
                    content=self.contents.get(
                        request.task_id,
                        f"desired {request.task_id} attempt {request.attempt_number}\n",
                    ),
                ),
            ),
            materialization_proposals=(
                (
                    ArtifactMaterializationProposal(
                        output_index=1,
                        target_path=target_path,
                    ),
                )
                if target_path is not None
                else ()
            ),
            assumptions=(),
            risks=(),
        )


class InvalidProposalIndexExecutor(MaterializingExecutor):
    """Return a bounded but uncorrelatable executor proposal for recovery tests."""

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        result = super().execute(request)
        return result.model_copy(
            update={
                "materialization_proposals": (
                    ArtifactMaterializationProposal(
                        output_index=2,
                        target_path="src/service.py",
                    ),
                )
            }
        )


class DuplicateMaterializationProposalExecutor(MaterializingExecutor):
    """Return two desired paths for one output to exercise proposal recovery."""

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        result = super().execute(request)
        return result.model_copy(
            update={
                "materialization_proposals": (
                    ArtifactMaterializationProposal(
                        output_index=1,
                        target_path="src/first.py",
                    ),
                    ArtifactMaterializationProposal(
                        output_index=1,
                        target_path="src/second.py",
                    ),
                )
            }
        )


class TerminalPeerMaterializingExecutor(MaterializingExecutor):
    """Terminally fail one peer while another produces desired workspace state."""

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        if request.task_id == "TASK-001":
            with self.lock:
                self.calls.append(request)
            raise TaskExecutorError(
                "Deterministic terminal peer failure.", retryable=False
            )
        return super().execute(request)


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement="Produce governed URL-shortener engineering artifacts.",
        requirement_type="greenfield",
        functional_requirements=["Define governed URL creation behavior."],
        nonfunctional_requirements=[],
        constraints=[],
        ambiguities=["URL expiration behavior remains unspecified."],
        assumptions=[
            "Generated artifacts may propose governed isolated-workspace state."
        ],
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
    materialization_policy: TaskMaterializationPolicy = (
        TaskMaterializationPolicy.FORBIDDEN
    ),
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=key.replace("_", " ").title(),
        description=f"Produce the {key} engineering artifact.",
        task_type=task_type,
        materialization_policy=materialization_policy,
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
) -> tuple[WorkflowState, TaskGraph, GovernedWorkspaceRuntime]:
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
    run_id = uuid4().hex
    runtime = GovernedWorkspaceRuntime()
    session, snapshot = establish_governed_workspace_session(
        runtime.establish_workspace_for_run(run_id), run_id=run_id
    )
    state: WorkflowState = {
        "run_id": run_id,
        "approved_requirement_spec": spec.model_dump(mode="json"),
        "approved_task_graph": graph.model_dump(mode="json"),
        "task_graph_execution": initialize_task_graph_execution(graph),
        "governed_workspace_session": session,
        "workspace_snapshots": [snapshot],
        "serialized_conflict_retry_task_ids": [],
    }
    return state, graph, runtime


def _run_approved(
    proposal: ProposedTaskGraph,
    executor: TaskExecutor,
    *,
    workspace_runtime: GovernedWorkspaceRuntime | None = None,
    thread_id: str | None = None,
) -> WorkflowState:
    workflow = build_workflow(
        FakeRequirementAnalysisClient([_analysis()]),
        FakeTaskPlanningClient([proposal]),
        executor,
        workspace_runtime=workspace_runtime,
    )
    thread_id = thread_id or uuid4().hex
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


def _controlled_mutation_result(
    change_set: WorkspaceChangeSet,
    status: WorkspaceMutationStatus,
    code: WorkspaceMutationIssueCode,
) -> WorkspaceMutationResult:
    return WorkspaceMutationResult(
        mutation_id=f"MUTATION-CONTROLLED-{status.value}",
        workspace_id=change_set.workspace_id,
        change_set_id=change_set.change_set_id,
        base_snapshot_id=change_set.base_snapshot_id,
        task_id=change_set.task_id,
        request_id=change_set.request_id,
        attempt_id=change_set.attempt_id,
        pre_mutation_snapshot_id=change_set.base_snapshot_id,
        post_mutation_snapshot_id=None,
        rollback_snapshot_id=(
            change_set.base_snapshot_id
            if status is WorkspaceMutationStatus.ROLLED_BACK
            else None
        ),
        status=status,
        file_evidence=(),
        issues=(
            WorkspaceMutationIssue(
                code=code,
                path=change_set.file_changes[0].path,
                detail=f"Controlled {code.value} outcome.",
            ),
        ),
    )


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
    state, _, runtime = _direct_execution_state(proposal)
    executor = DeterministicExecutor()
    real_builder = nodes_module.build_task_execution_request

    def controlled_builder(*args: object, **kwargs: object) -> TaskExecutionRequest:
        if args[3] == "TASK-002":
            raise TaskExecutionContractError(
                "Controlled authoritative request-build failure."
            )
        return real_builder(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(nodes_module, "build_task_execution_request", controlled_builder)

    update = execute_task_graph_step(
        state,
        executor=executor,
        workspace_runtime=runtime,
        repository_context_path_provider=DeterministicRepositoryContextPathProvider(),
    )

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

    state, _, runtime = _direct_execution_state(_single_proposal())
    terminal_executor = ScriptedRecoveryExecutor({})

    def fail_application_invariant(*args: object, **kwargs: object) -> object:
        raise TaskExecutionContractError("Application timestamp invariant failed.")

    monkeypatch.setattr(
        "agentic_sdlc.nodes.canonicalize_execution_result",
        fail_application_invariant,
    )
    terminal = execute_task_graph_step(
        state,
        executor=terminal_executor,
        workspace_runtime=runtime,
        repository_context_path_provider=DeterministicRepositoryContextPathProvider(),
    )
    assert terminal["task_graph_execution"].status is (
        TaskGraphExecutionStatus.FAILED
    )
    assert terminal["task_execution_recovery_decisions"][0].retryable is False
    assert terminal["task_execution_recovery_decisions"][0].action is (
        TaskExecutionRecoveryAction.FAIL_TASK
    )
    assert len(terminal_executor.calls) == 1


def test_missing_retry_history_is_non_retryable_request_build_failure() -> None:
    state, graph, runtime = _direct_execution_state(_single_proposal())
    running = start_task(
        graph, state["task_graph_execution"], "TASK-001"
    )
    state["task_graph_execution"] = prepare_task_retry(
        graph, running, "TASK-001"
    )
    executor = ScriptedRecoveryExecutor({})

    update = execute_task_graph_step(
        state,
        executor=executor,
        workspace_runtime=runtime,
        repository_context_path_provider=DeterministicRepositoryContextPathProvider(),
    )
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


def test_disjoint_parallel_materialization_advances_authority_serially(
    tmp_path: Path,
) -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )
    runtime = GovernedWorkspaceRuntime()
    executor = MaterializingExecutor(
        {"TASK-001": "src/a.py", "TASK-002": "src/b.py"}
    )

    result = _run_approved(
        proposal, executor, workspace_runtime=runtime
    )

    assert result["workflow_status"] == "success"
    assert [item.status for item in result["workspace_mutation_results"]] == [
        WorkspaceMutationStatus.APPLIED,
        WorkspaceMutationStatus.APPLIED,
    ]
    assert [item.task_id for item in result["workspace_mutation_results"]] == [
        "TASK-001",
        "TASK-002",
    ]
    first_wave = result["workspace_bound_task_execution_requests"][:2]
    assert first_wave[0].workspace_binding == first_wave[1].workspace_binding
    assert len(result["workspace_snapshots"]) == 3
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.VERIFIED
    )
    workspace = runtime.workspace_for_run(result["run_id"])
    assert (workspace.root / "src/a.py").read_text() == "desired TASK-001 attempt 1\n"
    assert (workspace.root / "src/b.py").read_text() == "desired TASK-002 attempt 1\n"
    assert all(
        item.disposition is TaskAttemptExitDisposition.SUCCEED_TASK
        for item in result["task_attempt_exit_decisions"]
    )
    write_artifacts(result, tmp_path)
    workspace_evidence = json.loads(
        (tmp_path / "workspace_execution.json").read_text()
    )
    summary = (tmp_path / "summary.md").read_text()
    GovernedWorkspaceSession.model_validate_json(
        json.dumps(workspace_evidence["session"])
    )
    for contract, values in (
        (WorkspaceSnapshot, workspace_evidence["snapshots"]),
        (WorkspaceExecutionWave, workspace_evidence["waves"]),
        (WorkspaceMutationResult, workspace_evidence["mutations"]),
        (
            TaskAttemptExitDecision,
            workspace_evidence["task_attempt_exit_decisions"],
        ),
    ):
        for value in values:
            contract.model_validate_json(json.dumps(value))
    assert workspace_evidence["session"]["authoritative_snapshot_id"] == (
        result["governed_workspace_session"].authoritative_snapshot_id
    )
    assert len(workspace_evidence["mutations"]) == 2
    assert len(workspace_evidence["task_attempt_exit_decisions"]) == 2
    assert "src/a.py (CREATE)" in summary
    assert "src/b.py (CREATE)" in summary
    assert "Generated code/tests executed: no" in summary


def test_same_path_conflict_retries_serially_against_latest_snapshots() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )
    runtime = GovernedWorkspaceRuntime()
    executor = MaterializingExecutor(
        {"TASK-001": "src/shared.py", "TASK-002": "src/shared.py"}
    )

    result = _run_approved(
        proposal, executor, workspace_runtime=runtime
    )

    assert result["workflow_status"] == "success"
    assert [wave.dispatch_mode for wave in result["workspace_execution_waves"]] == [
        WorkspaceDispatchMode.PARALLEL,
        WorkspaceDispatchMode.SERIALIZED_CONFLICT_RETRY,
        WorkspaceDispatchMode.SERIALIZED_CONFLICT_RETRY,
    ]
    assert [state.attempt_count for state in result["task_graph_execution"].task_states] == [2, 2]
    assert len(result["workspace_conflict_evidence"]) == 1
    conflict_id = result["workspace_conflict_evidence"][0].conflict_evidence_id
    assert all(
        conflict_id in item.evidence_ids
        for item in result["task_attempt_exit_decisions"][:2]
    )
    assert len(result["workspace_mutation_results"]) == 2
    calls = {(item.task_id, item.attempt_number): item for item in executor.calls}
    assert calls[("TASK-001", 1)].workspace_binding == calls[("TASK-002", 1)].workspace_binding
    assert calls[("TASK-001", 2)].workspace_binding.snapshot_id != calls[("TASK-002", 2)].workspace_binding.snapshot_id
    second_retry_context = calls[("TASK-002", 2)].repository_context.observations
    assert tuple(item.path for item in second_retry_context) == ("src/shared.py",)
    assert second_retry_context[0].content == "desired TASK-001 attempt 2\n"
    workspace = runtime.workspace_for_run(result["run_id"])
    assert (workspace.root / "src/shared.py").read_text() == "desired TASK-002 attempt 2\n"


def test_direct_dependency_materialized_paths_are_bounded_child_context() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "parent",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task("child", depends_on=["parent"]),
        ]
    )
    executor = MaterializingExecutor({"TASK-001": "src/service.py"})

    result = _run_approved(proposal, executor)

    assert result["workflow_status"] == "success"
    child_request = next(item for item in executor.calls if item.task_id == "TASK-002")
    assert tuple(
        observation.path
        for observation in child_request.repository_context.observations
    ) == ("src/service.py",)
    assert child_request.repository_context.observations[0].content == (
        "desired TASK-001 attempt 1\n"
    )
    assert "root" not in child_request.model_dump(mode="json")


def test_required_task_without_proposal_never_succeeds() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )
    result = _run_approved(proposal, DeterministicExecutor())

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].task_states[0].status is (
        TaskExecutionStatus.FAILED
    )
    assert [item.disposition for item in result["task_attempt_exit_decisions"]] == [
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.FAIL_TASK,
    ]
    assert result.get("workspace_mutation_results", []) == []
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.VERIFIED
    )
    assert [
        item.action for item in result["task_execution_recovery_decisions"]
    ] == [
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.FAIL_TASK,
    ]
    assert all(
        "POLICY" in item.feedback
        for item in result["task_execution_recovery_decisions"]
    )


def test_invalid_proposal_output_index_is_bounded_materialization_failure() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal,
        InvalidProposalIndexExecutor({"TASK-001": "src/service.py"}),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result.get("workspace_mutation_results", []) == []
    assert [
        item.failure_kind
        for item in result["task_execution_recovery_decisions"]
    ] == [TaskExecutionRecoveryFailureKind.MATERIALIZATION] * 3
    assert all(
        "unknown output index" in item.feedback
        for item in result["task_execution_recovery_decisions"]
    )


def test_duplicate_materialization_proposal_receives_bounded_retry() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal,
        DuplicateMaterializationProposalExecutor({"TASK-001": "unused.py"}),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result.get("workspace_mutation_results", []) == []
    assert [
        item.action for item in result["task_execution_recovery_decisions"]
    ] == [
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.RETRY,
        TaskExecutionRecoveryAction.FAIL_TASK,
    ]
    assert all(
        "DUPLICATE_ARTIFACT" in item.feedback
        for item in result["task_execution_recovery_decisions"]
    )


@mark.parametrize(
    "issue_code",
    (
        ArtifactMaterializationIssueCode.ARTIFACT_SET,
        ArtifactMaterializationIssueCode.LINEAGE,
        ArtifactMaterializationIssueCode.ARTIFACT_REFERENCE,
    ),
)
def test_trusted_materialization_evidence_inconsistency_is_terminal(
    monkeypatch: MonkeyPatch,
    issue_code: ArtifactMaterializationIssueCode,
) -> None:
    original = nodes_module.validate_artifact_materialization

    def artifact_set_failure(*args: object, **kwargs: object) -> object:
        validation = original(*args, **kwargs)  # type: ignore[arg-type]
        return validation.model_copy(
            update={
                "passed": False,
                "issues": (
                    ArtifactMaterializationValidationIssue(
                        code=issue_code,
                        artifact_id=None,
                        path=None,
                        detail="Controlled trusted-evidence inconsistency.",
                    ),
                ),
            }
        )

    monkeypatch.setattr(
        nodes_module, "validate_artifact_materialization", artifact_set_failure
    )
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "src/service.py"}),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].task_states[0].attempt_count == 1
    assert len(result["task_execution_recovery_decisions"]) == 1
    assert result["task_execution_recovery_decisions"][0].action is (
        TaskExecutionRecoveryAction.FAIL_TASK
    )
    assert issue_code.value in (
        result["task_execution_recovery_decisions"][0].feedback
    )
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.FAIL_TASK
    )


def test_allowed_task_without_proposal_uses_lighter_success_gate() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "allowed",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.ALLOWED,
            )
        ]
    )

    result = _run_approved(proposal, DeterministicExecutor())

    assert result["workflow_status"] == "success"
    assert result.get("workspace_mutation_results", []) == []
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.SUCCEED_TASK
    )


def test_allowed_task_with_proposal_uses_full_mutation_gate() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "allowed",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.ALLOWED,
            )
        ]
    )

    result = _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "docs/optional.md"}),
    )

    assert result["workflow_status"] == "success"
    assert result["workspace_mutation_results"][0].status is (
        WorkspaceMutationStatus.APPLIED
    )
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.SUCCEED_TASK
    )


def test_forbidden_task_proposal_never_reaches_mutation() -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "forbidden",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
            )
        ]
    )

    result = _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "src/forbidden.py"}),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result.get("workspace_mutation_results", []) == []
    assert [item.disposition for item in result["task_attempt_exit_decisions"]] == [
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.FAIL_TASK,
    ]


def test_predispatch_workspace_drift_hard_safe_stops() -> None:
    state, graph, runtime = _direct_execution_state(_single_proposal())
    workspace = runtime.workspace_for_run(state["run_id"])
    (workspace.root / "external.txt").write_text("drift\n")

    update = execute_task_graph_step(
        state,
        executor=DeterministicExecutor(),
        workspace_runtime=runtime,
        repository_context_path_provider=DeterministicRepositoryContextPathProvider(),
    )

    assert update["task_graph_execution"].status is TaskGraphExecutionStatus.FAILED
    assert update["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.UNPROVABLE
    )
    assert ready_task_ids(graph, update["task_graph_execution"]) == ()


def test_missing_runtime_capability_hard_safe_stops() -> None:
    state, graph, _ = _direct_execution_state(_single_proposal())

    update = execute_task_graph_step(
        state,
        executor=DeterministicExecutor(),
        workspace_runtime=GovernedWorkspaceRuntime(),
        repository_context_path_provider=DeterministicRepositoryContextPathProvider(),
    )

    assert update["task_graph_execution"].status is TaskGraphExecutionStatus.FAILED
    assert update["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.UNPROVABLE
    )
    assert ready_task_ids(graph, update["task_graph_execution"]) == ()


def test_workspace_initialization_failure_routes_to_safe_stop(
    monkeypatch: MonkeyPatch,
) -> None:
    runtime = GovernedWorkspaceRuntime()

    def unavailable(run_id: str) -> object:
        del run_id
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.RUNTIME,
            "Controlled workspace creation failure.",
        )

    monkeypatch.setattr(runtime, "establish_workspace_for_run", unavailable)
    executor = DeterministicExecutor()

    result = _run_approved(
        _single_proposal(),
        executor,
        workspace_runtime=runtime,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SAFE_STOPPED
    )
    assert result.get("governed_workspace_session") is None
    assert executor.calls == []


def test_rollback_failed_maps_to_hard_safe_stop(
    monkeypatch: MonkeyPatch,
) -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    def rollback_failed(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        del workspace, validation
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.ROLLBACK_FAILED,
            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
        )

    monkeypatch.setattr(nodes_module, "apply_workspace_change_set", rollback_failed)
    result = _run_approved(
        proposal, MaterializingExecutor({"TASK-001": "src/a.py"})
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.UNPROVABLE
    )
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.SAFE_STOP_RUN
    )
    assert len(result["workspace_mutation_results"]) == 1


def test_stale_mutation_rejection_retries_then_succeeds(
    monkeypatch: MonkeyPatch,
) -> None:
    original = nodes_module.apply_workspace_change_set
    call_count = 0

    def stale_once(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _controlled_mutation_result(
                change_set,
                WorkspaceMutationStatus.REJECTED,
                WorkspaceMutationIssueCode.STALE_PRECONDITION,
            )
        return original(workspace, change_set, validation)  # type: ignore[arg-type]

    monkeypatch.setattr(nodes_module, "apply_workspace_change_set", stale_once)
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal, MaterializingExecutor({"TASK-001": "src/service.py"})
    )

    assert result["workflow_status"] == "success"
    assert call_count == 2
    assert [item.disposition for item in result["task_attempt_exit_decisions"]] == [
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.SUCCEED_TASK,
    ]
    assert result["task_graph_execution"].task_states[0].attempt_count == 2


def test_terminal_mutation_rejection_fails_task_and_keeps_child_blocked(
    monkeypatch: MonkeyPatch,
) -> None:
    def terminal_rejection(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        del workspace, validation
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            WorkspaceMutationIssueCode.SYMLINK_DETECTED,
        )

    monkeypatch.setattr(
        nodes_module, "apply_workspace_change_set", terminal_rejection
    )
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "parent",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task("child", depends_on=["parent"]),
        ]
    )
    executor = MaterializingExecutor({"TASK-001": "src/service.py"})

    result = _run_approved(proposal, executor)

    assert result["workflow_status"] == "safe_stopped"
    assert [item.status for item in result["task_graph_execution"].task_states] == [
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.BLOCKED,
    ]
    assert [item.task_id for item in executor.calls] == ["TASK-001"]
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.FAIL_TASK
    )
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.VERIFIED
    )


def test_retryable_rolled_back_mutation_retries_then_succeeds(
    monkeypatch: MonkeyPatch,
) -> None:
    original = nodes_module.apply_workspace_change_set
    call_count = 0

    def rolled_back_once(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _controlled_mutation_result(
                change_set,
                WorkspaceMutationStatus.ROLLED_BACK,
                WorkspaceMutationIssueCode.MODIFY_FAILURE,
            )
        return original(workspace, change_set, validation)  # type: ignore[arg-type]

    monkeypatch.setattr(
        nodes_module, "apply_workspace_change_set", rolled_back_once
    )
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal, MaterializingExecutor({"TASK-001": "src/service.py"})
    )

    assert result["workflow_status"] == "success"
    assert [item.disposition for item in result["task_attempt_exit_decisions"]] == [
        TaskAttemptExitDisposition.RETRY_TASK,
        TaskAttemptExitDisposition.SUCCEED_TASK,
    ]


def test_nonretryable_rolled_back_mutation_fails_task(
    monkeypatch: MonkeyPatch,
) -> None:
    def rolled_back_terminal(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        del workspace, validation
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.ROLLED_BACK,
            WorkspaceMutationIssueCode.RUNTIME_PATH_POLICY,
        )

    monkeypatch.setattr(
        nodes_module, "apply_workspace_change_set", rolled_back_terminal
    )
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "required",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )

    result = _run_approved(
        proposal, MaterializingExecutor({"TASK-001": "src/service.py"})
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_attempt_exit_decisions"][0].disposition is (
        TaskAttemptExitDisposition.FAIL_TASK
    )
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.VERIFIED
    )


def test_same_path_no_change_proposals_are_compatible_and_keep_snapshot(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "same-path-no-change"
    workspace = runtime.establish_workspace_for_run(thread_id)
    (workspace.root / "src").mkdir()
    desired = "already authoritative\n"
    (workspace.root / "src/shared.py").write_text(desired)
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )
    executor = MaterializingExecutor(
        {"TASK-001": "src/shared.py", "TASK-002": "src/shared.py"},
        contents={"TASK-001": desired, "TASK-002": desired},
    )

    result = _run_approved(
        proposal,
        executor,
        workspace_runtime=runtime,
        thread_id=thread_id,
    )

    assert result["workflow_status"] == "success"
    assert len(result["workspace_conflict_evidence"]) == 1
    assert result["workspace_conflict_evidence"][0].analysis.has_conflicts is False
    assert len(result["workspace_snapshots"]) == 1
    assert {
        change.operation
        for change_set in result["workspace_change_sets"]
        for change in change_set.file_changes
    } == {WorkspaceChangeOperation.NO_CHANGE}
    assert all(
        item.status is WorkspaceMutationStatus.APPLIED
        for item in result["workspace_mutation_results"]
    )


def test_hard_stop_prevents_later_same_wave_mutation(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[str] = []

    def rollback_failed(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        del workspace, validation
        calls.append(change_set.task_id)
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.ROLLBACK_FAILED,
            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
        )

    monkeypatch.setattr(nodes_module, "apply_workspace_change_set", rollback_failed)
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )

    result = _run_approved(
        proposal,
        MaterializingExecutor(
            {"TASK-001": "src/a.py", "TASK-002": "src/b.py"}
        ),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert calls == ["TASK-001"]
    assert all(
        item.disposition is TaskAttemptExitDisposition.SAFE_STOP_RUN
        for item in result["task_attempt_exit_decisions"]
    )
    assert all(
        item.status is TaskExecutionStatus.ABORTED
        for item in result["task_graph_execution"].task_states
    )


def test_later_same_wave_integrity_loss_preserves_verified_peer_success(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = nodes_module.apply_workspace_change_set
    calls: list[str] = []

    def apply_then_lose_integrity(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        calls.append(change_set.task_id)
        if change_set.task_id == "TASK-001":
            return original(workspace, change_set, validation)  # type: ignore[arg-type]
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.ROLLBACK_FAILED,
            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
        )

    monkeypatch.setattr(
        nodes_module, "apply_workspace_change_set", apply_then_lose_integrity
    )
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "later",
                depends_on=["first", "second"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "preserve-succeeded-peer"

    result = _run_approved(
        proposal,
        MaterializingExecutor(
            {
                "TASK-001": "src/a.py",
                "TASK-002": "src/b.py",
                "TASK-003": "src/c.py",
            }
        ),
        workspace_runtime=runtime,
        thread_id=thread_id,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert calls == ["TASK-001", "TASK-002"]
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.UNPROVABLE
    )
    assert [
        item.status for item in result["task_graph_execution"].task_states
    ] == [
        TaskExecutionStatus.SUCCEEDED,
        TaskExecutionStatus.ABORTED,
        TaskExecutionStatus.BLOCKED,
    ]
    assert [
        item.disposition for item in result["task_attempt_exit_decisions"]
    ] == [
        TaskAttemptExitDisposition.SUCCEED_TASK,
        TaskAttemptExitDisposition.SAFE_STOP_RUN,
    ]
    assert [item.status for item in result["workspace_mutation_results"]] == [
        WorkspaceMutationStatus.APPLIED,
        WorkspaceMutationStatus.ROLLBACK_FAILED,
    ]
    first_mutation = result["workspace_mutation_results"][0]
    assert first_mutation.mutation_id in (
        result["task_attempt_exit_decisions"][0].evidence_ids
    )
    assert (runtime.workspace_for_run(thread_id).root / "src/a.py").is_file()
    assert not (runtime.workspace_for_run(thread_id).root / "src/b.py").exists()
    assert not (runtime.workspace_for_run(thread_id).root / "src/c.py").exists()


def test_peer_integrity_loss_preserves_prior_terminal_task_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    def rollback_failed(
        workspace: object,
        change_set: WorkspaceChangeSet,
        validation: object,
    ) -> WorkspaceMutationResult:
        del workspace, validation
        return _controlled_mutation_result(
            change_set,
            WorkspaceMutationStatus.ROLLBACK_FAILED,
            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
        )

    monkeypatch.setattr(nodes_module, "apply_workspace_change_set", rollback_failed)
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "terminal",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "integrity_loss",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )

    result = _run_approved(
        proposal,
        TerminalPeerMaterializingExecutor({"TASK-002": "src/b.py"}),
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["governed_workspace_session"].integrity_status is (
        WorkspaceIntegrityStatus.UNPROVABLE
    )
    assert [
        item.status for item in result["task_graph_execution"].task_states
    ] == [TaskExecutionStatus.FAILED, TaskExecutionStatus.ABORTED]
    assert [
        item.disposition for item in result["task_attempt_exit_decisions"]
    ] == [
        TaskAttemptExitDisposition.FAIL_TASK,
        TaskAttemptExitDisposition.SAFE_STOP_RUN,
    ]
