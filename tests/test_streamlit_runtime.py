"""Deterministic tests for the Streamlit session background runtime."""

from __future__ import annotations

import ast
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

import pytest

from agentic_sdlc.application import (
    EligibleBrownfieldProject,
    GovernedRunApplicationStatus,
    GovernedRunLifecycleError,
    GovernedRunMode,
    GovernedRunRequest,
    GovernedRunSnapshot,
    HumanGovernanceGate,
)
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.project_export import ProjectNameError
from agentic_sdlc.requirement_submission import (
    RequirementSubmissionError,
    deterministic_project_name,
    resolve_inline_requirement,
)
from agentic_sdlc.run_artifacts import LiveRunArtifactBundle
from agentic_sdlc.state import (
    ApprovalResponse,
    TASK_GRAPH_REJECTED_REASON,
    demo_input,
)
from agentic_sdlc.streamlit_runtime import (
    StreamlitOperationKind,
    StreamlitRunRuntime,
    governed_run_request_from_inline_requirement,
)
from agentic_sdlc.task_execution_progress import (
    TaskExecutionAttemptStarted,
    TaskExecutionProgressAttempt,
    TaskExecutionProgressReporter,
    TaskExecutionWaveMode,
    TaskExecutionWaveStarted,
)
from tests.test_application import _service
from tests.test_workflow import _analysis, _proposal


class QueuedExecutor:
    """Run submitted work only when a test explicitly settles the next job."""

    def __init__(self) -> None:
        self.jobs: deque[
            tuple[Future[Any], Callable[..., Any], tuple[Any, ...], dict[str, Any]]
        ] = deque()

    def submit(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        future: Future[Any] = Future()
        self.jobs.append((future, function, args, kwargs))
        return future

    def run_next(self) -> None:
        future, function, args, kwargs = self.jobs.popleft()
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)


class ManualClock:
    """Deterministic monotonic presentation clock without timing sleeps."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingService:
    def __init__(
        self,
        start_snapshot: GovernedRunSnapshot,
        resume_snapshots: list[GovernedRunSnapshot],
        eligible_projects: tuple[EligibleBrownfieldProject, ...] = (),
    ) -> None:
        self.start_snapshot = start_snapshot
        self.resume_snapshots = deque(resume_snapshots)
        self.current_snapshot: GovernedRunSnapshot | None = None
        self.start_calls: list[GovernedRunRequest] = []
        self.resume_calls: list[tuple[str, ApprovalResponse, str]] = []
        self.inspect_calls: list[str] = []
        self.progress_reporter: TaskExecutionProgressReporter | None = None
        self.eligible_projects = eligible_projects
        self.list_eligible_calls = 0

    def list_eligible_brownfield_projects(
        self,
    ) -> tuple[EligibleBrownfieldProject, ...]:
        self.list_eligible_calls += 1
        return self.eligible_projects

    def start_run(
        self,
        request: GovernedRunRequest,
        *,
        progress_reporter: TaskExecutionProgressReporter | None = None,
    ) -> GovernedRunSnapshot:
        self.start_calls.append(request)
        self.progress_reporter = progress_reporter
        self.current_snapshot = self.start_snapshot
        return self.start_snapshot

    def inspect_run(self, run_id: str) -> GovernedRunSnapshot:
        self.inspect_calls.append(run_id)
        assert self.current_snapshot is not None
        assert self.current_snapshot.run_id == run_id
        return self.current_snapshot

    def resume_run(
        self,
        run_id: str,
        decision: ApprovalResponse,
        *,
        gate_token: str,
    ) -> GovernedRunSnapshot:
        self.resume_calls.append((run_id, decision, gate_token))
        self.current_snapshot = self.resume_snapshots.popleft()
        return self.current_snapshot


def _snapshot(
    tmp_path: Path,
    *,
    run_id: str = "run-streamlit",
    gate_token: str | None = "run-streamlit:human-gate:1",
    stage: str = "requirement_analysis_review",
    allowed_decisions: tuple[str, ...] = (
        "APPROVE",
        "REQUEST_CHANGES",
        "REJECT",
    ),
    revision: int = 0,
    application_status: GovernedRunApplicationStatus = (
        GovernedRunApplicationStatus.AWAITING_HUMAN
    ),
    workflow_status: str = "awaiting_approval",
    blocked: bool = False,
) -> GovernedRunSnapshot:
    ambiguities = (
        [
            "How long should scheduled jobs be retained?",
            "Is authentication required for this prototype?",
        ]
        if blocked
        else []
    )
    analysis = {
        "normalized_problem_statement": f"revision-{revision}: Build a scheduler.",
        "requirement_type": "greenfield",
        "functional_requirements": ["Create scheduled jobs."],
        "nonfunctional_requirements": ["Execute jobs reliably."],
        "constraints": ["Use the existing runtime."],
        "ambiguities": ambiguities,
        "assumptions": ["Users supply valid schedules."],
        "acceptance_criteria": ["A scheduled job runs."],
        "risks": ["Clock drift can delay a job."],
        "needs_clarification": blocked,
        "confidence": 0.91,
    }
    payload = {
        "stage": stage,
        "checkpoint": (
            "requirement_analysis"
            if stage == "requirement_analysis_review"
            else "task_graph"
        ),
        "allowed_decisions": [
            decision
            for decision in allowed_decisions
            if not blocked or decision != "APPROVE"
        ],
        "revision_number": revision,
        "requirement_analysis": analysis,
        "planning_readiness": {
            "analysis_revision": revision,
            "status": "BLOCKED" if blocked else "READY",
            "needs_clarification": blocked,
            "blocking_ambiguities": ambiguities,
            "reason_code": (
                "UNRESOLVED_REQUIREMENT_AMBIGUITY" if blocked else None
            ),
        },
    }
    effective_allowed_decisions = tuple(payload["allowed_decisions"])
    gate = (
        HumanGovernanceGate(
            gate_token=gate_token,
            stage=stage,
            checkpoint=payload["checkpoint"],
            allowed_decisions=effective_allowed_decisions,  # type: ignore[arg-type]
            payload=payload,
        )
        if gate_token is not None
        else None
    )
    workflow_state: dict[str, Any] = {
        "workflow_status": workflow_status,
        "raw_requirement": "Build a scheduler for recurring jobs.",
        "requirement_submission": resolve_inline_requirement(
            "  Build a scheduler for recurring jobs.  "
        ).as_state_data(),
        "requirement_analysis": analysis,
        "requirement_analysis_revision_count": revision,
        "requirement_analysis_history": [
            {
                "revision_number": revision,
                "attempt_number": 1,
                "reviewer_feedback": "",
                "analysis": analysis,
            }
        ],
        "requirement_review_history": [],
    }
    return GovernedRunSnapshot(
        run_id=run_id,
        application_status=application_status,
        workflow_status=workflow_status,
        human_gate=gate,
        workflow_state=workflow_state,
        artifact_bundle=LiveRunArtifactBundle.under_repository(tmp_path, run_id),
        workflow_diagram_generated=False,
        manifest_path=None,
        export_result=None,
        application_error=None,
        warnings=(),
    )


def _task_graph_snapshot(
    tmp_path: Path,
    *,
    gate_token: str = "run-streamlit:human-gate:2",
    allowed_decisions: tuple[str, ...] = (
        "APPROVE",
        "REQUEST_CHANGES",
        "REJECT",
    ),
    revision: int = 0,
    title_suffix: str = "",
    prior_feedback: str | None = None,
    required_validation_task_id: str | None = None,
) -> GovernedRunSnapshot:
    spec = {
        "spec_id": "SPEC-DEMO-V001",
        "lineage_id": "spec-lineage",
        "version": 1,
        "supersedes_spec_id": None,
        "source_analysis_revision": 2,
        "created_at": "2026-08-12T12:00:00+00:00",
        "content_hash": "a" * 64,
        "normalized_problem_statement": "Build a URL shortener.",
        "requirement_type": "greenfield",
        "assumptions": [],
        "functional_requirements": [
            {"item_id": "FR-001", "lineage_id": "fr-1", "text": "Accept a URL."},
            {
                "item_id": "FR-002",
                "lineage_id": "fr-2",
                "text": "Redirect a short code.",
            },
        ],
        "nonfunctional_requirements": [
            {
                "item_id": "NFR-001",
                "lineage_id": "nfr-1",
                "text": "Short-code lookup is reliable.",
            }
        ],
        "constraints": [
            {
                "item_id": "CON-001",
                "lineage_id": "con-1",
                "text": "Use the governed workspace.",
            }
        ],
        "acceptance_criteria": [
            {
                "item_id": "AC-001",
                "lineage_id": "ac-1",
                "text": "A valid URL receives a short code.",
            },
            {
                "item_id": "AC-002",
                "lineage_id": "ac-2",
                "text": "A known code redirects correctly.",
            },
        ],
        "risks": [
            {
                "item_id": "RISK-001",
                "lineage_id": "risk-1",
                "text": "Code collisions can misdirect users.",
            }
        ],
        "ambiguities": [
            {
                "item_id": "AMB-001",
                "lineage_id": "amb-1",
                "text": "Expiration behavior is unspecified.",
            }
        ],
    }
    graph = _task_graph_data(
        revision=revision,
        title_suffix=title_suffix,
        required_validation_task_id=required_validation_task_id,
    )
    semantics = {
        "topological_order": ["TASK-001", "TASK-002", "TASK-003", "TASK-004"],
        "execution_layers": [
            ["TASK-001"],
            ["TASK-002", "TASK-003"],
            ["TASK-004"],
        ],
        "entry_ready_tasks": ["TASK-001"],
        "exit_predecessor_tasks": ["TASK-004"],
        "synchronization_points": ["TASK-004"],
    }
    payload = {
        "stage": "task_graph_review",
        "checkpoint": "task_graph",
        "message": "Engineering task graph requires human review.",
        "approved_requirement_spec": spec,
        "project_delivery_policy": {"mode": "RUNNABLE_PROJECT"},
        "candidate_task_graph": graph,
        "graph_semantics": semantics,
        "revision_number": revision,
        "allowed_decisions": list(allowed_decisions),
    }
    history = []
    review_history = []
    if revision > 0:
        history.append(
            {
                "sequence": 1,
                "revision_number": 0,
                "attempt_number": 1,
                "prompt_version": "task-planning-v1.4",
                "model_name": "fake-task-planner",
                "reviewer_feedback": "",
                "task_graph": _task_graph_data(
                    revision=0,
                    required_validation_task_id=required_validation_task_id,
                ),
            }
        )
        review_history.append(
            {
                "sequence": 1,
                "checkpoint": "task_graph",
                "decision": "REQUEST_CHANGES",
                "feedback": prior_feedback or "Revise the graph.",
                "revision_number": 0,
            }
        )
    history.append(
        {
            "sequence": len(history) + 1,
            "revision_number": revision,
            "attempt_number": 1,
            "prompt_version": "task-planning-v1.4",
            "model_name": "fake-task-planner",
            "reviewer_feedback": prior_feedback or "" if revision > 0 else "",
            "task_graph": graph,
        }
    )
    return GovernedRunSnapshot(
        run_id="run-streamlit",
        application_status=GovernedRunApplicationStatus.AWAITING_HUMAN,
        workflow_status="awaiting_approval",
        human_gate=HumanGovernanceGate(
            gate_token=gate_token,
            stage="task_graph_review",
            checkpoint="task_graph",
            allowed_decisions=allowed_decisions,  # type: ignore[arg-type]
            payload=payload,
        ),
        workflow_state={
            "workflow_status": "awaiting_approval",
            "approved_requirement_spec": spec,
            "candidate_task_graph": graph,
            "task_graph_semantics": semantics,
            "task_graph_revision_count": revision,
            "task_graph_history": history,
            "task_graph_review_history": review_history,
        },
        artifact_bundle=LiveRunArtifactBundle.under_repository(
            tmp_path,
            "run-streamlit",
        ),
        workflow_diagram_generated=False,
        manifest_path=None,
        export_result=None,
        application_error=None,
        warnings=(),
    )


def _task_graph_data(
    *,
    revision: int,
    title_suffix: str = "",
    required_validation_task_id: str | None = None,
) -> dict[str, Any]:
    def task(
        task_id: str,
        source_key: str,
        title: str,
        *,
        description: str,
        task_type: str,
        materialization_policy: str,
        depends_on: list[str],
        requirement_refs: list[str],
        acceptance_refs: list[str],
        risk_refs: list[str] | None = None,
        ambiguity_refs: list[str] | None = None,
        expected_outputs: list[str],
        deliverable_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "lineage_id": f"lineage-{task_id}",
            "source_key": source_key,
            "title": f"{title}{title_suffix}",
            "description": description,
            "task_type": task_type,
            "materialization_policy": materialization_policy,
            "depends_on": depends_on,
            "requirement_refs": requirement_refs,
            "acceptance_criteria_refs": acceptance_refs,
            "risk_refs": risk_refs or [],
            "ambiguity_refs": ambiguity_refs or [],
            "expected_outputs": expected_outputs,
            "deliverable_roles": deliverable_roles or [],
            "required_validations": (
                [
                    {
                        "requirement_id": f"{task_id}-VALIDATION-001",
                        "profile": "PYTHON_COMPILE",
                    }
                ]
                if task_id == required_validation_task_id
                else []
            ),
        }

    return {
        "graph_id": f"GRAPH-DEMO-V{revision + 1:03d}",
        "lineage_id": "graph-lineage",
        "version": revision + 1,
        "requirement_spec_id": "SPEC-DEMO-V001",
        "requirement_spec_version": 1,
        "supersedes_graph_id": "GRAPH-DEMO-V001" if revision > 0 else None,
        "created_at": "2026-08-12T12:00:00+00:00",
        "content_hash": str(revision + 1) * 64,
        "delivery_policy": {"mode": "RUNNABLE_PROJECT"},
        "tasks": [
            task(
                "TASK-001",
                "define_contract",
                "Define API contract",
                description="Define shortening and redirect behavior.",
                task_type="DESIGN",
                materialization_policy="FORBIDDEN",
                depends_on=[],
                requirement_refs=["FR-001"],
                acceptance_refs=["AC-001"],
                expected_outputs=["docs/api-contract.md"],
            ),
            task(
                "TASK-002",
                "build_service",
                "Implement shortener",
                description="Build the governed URL-shortening service.",
                task_type="IMPLEMENTATION",
                materialization_policy="REQUIRED",
                depends_on=["TASK-001"],
                requirement_refs=["FR-002", "NFR-001"],
                acceptance_refs=["AC-002"],
                risk_refs=["RISK-001"],
                expected_outputs=["src/url_shortener/app.py"],
                deliverable_roles=["RUNNABLE_ENTRYPOINT"],
            ),
            task(
                "TASK-003",
                "build_tests",
                "Build validation suite",
                description="Test contract behavior and ambiguity boundaries.",
                task_type="TEST",
                materialization_policy="REQUIRED",
                depends_on=["TASK-001"],
                requirement_refs=["CON-001"],
                acceptance_refs=["AC-001", "AC-002"],
                ambiguity_refs=["AMB-001"],
                expected_outputs=["tests/test_service.py"],
                deliverable_roles=["AUTOMATED_TESTS"],
            ),
            task(
                "TASK-004",
                "publish_guide",
                "Publish run guide",
                description="Document how to run the validated service.",
                task_type="DOCUMENTATION",
                materialization_policy="REQUIRED",
                depends_on=["TASK-002", "TASK-003"],
                requirement_refs=["FR-001"],
                acceptance_refs=[],
                expected_outputs=["README.md"],
                deliverable_roles=["RUN_INSTRUCTIONS"],
            ),
        ],
    }


def _advance_runtime_to_task_graph(
    runtime: StreamlitRunRuntime,
    executor: QueuedExecutor,
    *,
    operation_prefix: str,
) -> GovernedRunSnapshot:
    assert runtime.schedule_start(
        f"{operation_prefix}-start",
        GovernedRunRequest(command="demo", workflow_input=demo_input()),
    )
    executor.run_next()
    requirement_gate = runtime.poll().snapshot
    assert requirement_gate is not None
    assert requirement_gate.human_gate is not None
    assert requirement_gate.human_gate.stage == "requirement_analysis_review"

    assert runtime.schedule_resume(
        f"{operation_prefix}-requirements-approve",
        requirement_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_gate.human_gate.gate_token,
    )
    executor.run_next()
    task_graph_gate = runtime.poll().snapshot
    assert task_graph_gate is not None
    assert task_graph_gate.human_gate is not None
    assert task_graph_gate.human_gate.stage == "task_graph_review"
    return task_graph_gate


def test_inline_request_uses_submission_evidence_and_deterministic_name() -> None:
    original = "\ufeff  Build a scheduler.\r\n\r\n- Run jobs.  \r\n"
    submission = resolve_inline_requirement(original)

    request = governed_run_request_from_inline_requirement(original, "")

    assert request.command == "run"
    assert request.run_mode is GovernedRunMode.GREENFIELD
    assert request.baseline_project_name is None
    assert request.requested_project_name is None
    assert request.workflow_input["project_name"] == deterministic_project_name(
        submission
    )
    assert request.workflow_input["raw_requirement"] == submission.normalized_text
    assert request.workflow_input["requirements"] == [submission.normalized_text]
    assert request.workflow_input["requirement_submission"] == (
        submission.as_state_data()
    )
    assert request.workflow_input["requirement_submission"]["original_text"] == (
        original
    )


def test_inline_request_uses_existing_explicit_project_name_normalization() -> None:
    request = governed_run_request_from_inline_requirement(
        "Build a scheduler.",
        "  My Scheduler App  ",
    )

    assert request.requested_project_name == "  My Scheduler App  "
    assert request.workflow_input["project_name"] == "my-scheduler-app"

    with pytest.raises(ProjectNameError, match="single safe folder name"):
        governed_run_request_from_inline_requirement(
            "Build a scheduler.",
            "../escape",
        )


def test_inline_brownfield_request_uses_logical_baseline_and_required_output() -> None:
    request = governed_run_request_from_inline_requirement(
        "Add expiration while preserving existing behavior.",
        "enhanced-project",
        run_mode=GovernedRunMode.BROWNFIELD,
        baseline_project_name="published-project",
    )

    assert request.run_mode is GovernedRunMode.BROWNFIELD
    assert request.baseline_project_name == "published-project"
    assert request.requested_project_name == "enhanced-project"
    assert request.workflow_input["project_name"] == "enhanced-project"

    with pytest.raises(ValueError, match="new output project name"):
        governed_run_request_from_inline_requirement(
            "Change the existing project.",
            "",
            run_mode=GovernedRunMode.BROWNFIELD,
            baseline_project_name="published-project",
        )


def test_runtime_delegates_brownfield_listing_to_application_service(
    tmp_path: Path,
) -> None:
    project = EligibleBrownfieldProject(
        project_name="published-project",
        originating_run_id="published-run",
        workflow_project_name="Published Project",
        source_snapshot_id="WORKSPACE-SNAPSHOT-BASELINE",
        engineering_file_count=3,
        publication_bundle_sha256="a" * 64,
    )
    service = RecordingService(_snapshot(tmp_path), [], (project,))
    runtime = StreamlitRunRuntime(service, executor=QueuedExecutor())

    assert runtime.list_eligible_brownfield_projects() == (project,)
    assert service.list_eligible_calls == 1


@pytest.mark.parametrize("requirement", ["", "  \t\r\n"])
def test_empty_inline_request_fails_before_a_runtime_can_schedule(
    requirement: str,
) -> None:
    with pytest.raises(RequirementSubmissionError, match="non-whitespace"):
        governed_run_request_from_inline_requirement(requirement, "")


def test_runtime_exposes_monotonic_elapsed_only_while_operation_is_in_flight(
    tmp_path: Path,
) -> None:
    first_gate = _snapshot(tmp_path)
    service = RecordingService(first_gate, [])
    executor = QueuedExecutor()
    clock = ManualClock()
    runtime = StreamlitRunRuntime(service, executor=executor, clock=clock)
    request = GovernedRunRequest(command="demo", workflow_input=demo_input())

    assert runtime.poll().operation_elapsed_seconds is None
    assert runtime.schedule_start("timed-start", request)
    assert runtime.poll().operation_elapsed_seconds == 0.0

    clock.advance(402.75)
    in_flight = runtime.poll()
    assert in_flight.in_flight
    assert in_flight.operation_elapsed_seconds == 402.75
    assert len(executor.jobs) == 1
    assert service.start_calls == []

    executor.run_next()
    completed = runtime.poll()
    assert not completed.in_flight
    assert completed.snapshot == first_gate
    assert completed.operation_elapsed_seconds is None


def test_runtime_serializes_start_and_resume_with_gate_token_idempotency(
    tmp_path: Path,
) -> None:
    first_gate = _snapshot(tmp_path)
    revised_gate = _snapshot(
        tmp_path,
        gate_token="run-streamlit:human-gate:2",
        revision=1,
    )
    service = RecordingService(first_gate, [revised_gate])
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)
    request = GovernedRunRequest(command="demo", workflow_input=demo_input())

    assert runtime.schedule_start("start-1", request)
    assert not runtime.schedule_start("start-1", request)
    assert not runtime.schedule_start("start-2", request)
    assert runtime.poll().in_flight
    assert service.start_calls == []

    executor.run_next()
    started = runtime.poll()
    assert not started.in_flight
    assert started.snapshot == first_gate
    assert started.operation_kind is StreamlitOperationKind.START
    assert service.start_calls == [request]
    assert service.inspect_calls == [first_gate.run_id]

    feedback = "  Preserve this indentation.\nAnd this line.  "
    response: ApprovalResponse = {
        "decision": "REQUEST_CHANGES",
        "feedback": feedback,
    }
    first_token = first_gate.human_gate.gate_token
    assert runtime.schedule_resume(
        "resume-1",
        first_gate.run_id,
        response,
        gate_token=first_token,
    )
    assert not runtime.schedule_resume(
        "resume-1",
        first_gate.run_id,
        response,
        gate_token=first_token,
    )
    assert not runtime.schedule_resume(
        "resume-2",
        first_gate.run_id,
        response,
        gate_token=first_token,
    )
    assert service.resume_calls == []

    executor.run_next()
    revised = runtime.poll()
    assert revised.snapshot == revised_gate
    assert service.resume_calls == [
        (first_gate.run_id, response, first_token)
    ]
    assert service.resume_calls[0][1]["feedback"] == feedback
    assert revised.snapshot.human_gate.gate_token != first_token


def test_reject_reaches_real_governed_safe_stop_through_runtime(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="streamlit-reject",
    )
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)

    assert runtime.schedule_start(
        "start-real",
        GovernedRunRequest(command="demo", workflow_input=demo_input()),
    )
    executor.run_next()
    paused = runtime.poll().snapshot
    assert paused is not None
    assert paused.human_gate is not None

    assert runtime.schedule_resume(
        "reject-real",
        paused.run_id,
        {"decision": "REJECT", "feedback": ""},
        gate_token=paused.human_gate.gate_token,
    )
    executor.run_next()
    stopped = runtime.poll().snapshot

    assert stopped is not None
    assert stopped.application_status is GovernedRunApplicationStatus.SAFE_STOPPED
    assert stopped.workflow_status == "safe_stopped"
    assert stopped.human_gate is None
    assert stopped.workflow_state["safe_stop_reason"] == (
        "Requirement analysis rejected by human."
    )


def test_task_graph_approve_runs_real_governed_lifecycle_to_terminal(
    tmp_path: Path,
) -> None:
    service, _, task_executor = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="streamlit-task-graph-approve",
    )
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)
    graph_gate = _advance_runtime_to_task_graph(
        runtime,
        executor,
        operation_prefix="approve",
    )
    gate_token = graph_gate.human_gate.gate_token

    assert runtime.schedule_resume(
        "approve-task-graph",
        graph_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=gate_token,
    )
    assert runtime.poll().in_flight
    executor.run_next()
    terminal_view = runtime.poll()
    terminal = terminal_view.snapshot

    assert terminal is not None
    assert terminal.application_status is GovernedRunApplicationStatus.SUCCEEDED
    assert terminal.workflow_status == "success"
    assert terminal.human_gate is None
    assert terminal.workflow_state["approved_task_graph"] == (
        terminal.workflow_state["candidate_task_graph"]
    )
    assert len(task_executor.calls) == 5
    assert executor.jobs == deque()
    assert terminal_view.execution_progress is not None
    assert terminal_view.execution_progress.telemetry_status == (
        "OBSERVATION_COMPLETE"
    )
    assert terminal_view.execution_progress.completed_task_count == 5
    assert all(
        task.status == "SUCCEEDED"
        for task in terminal_view.execution_progress.tasks
        if not task.unknown_task
    )


def test_runtime_exposes_parallel_structured_progress_while_resume_is_in_flight(
    tmp_path: Path,
) -> None:
    graph_gate = _task_graph_snapshot(tmp_path)
    terminal = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
    )
    service = RecordingService(graph_gate, [terminal])
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)

    assert runtime.schedule_start(
        "start-for-progress",
        GovernedRunRequest(command="demo", workflow_input=demo_input()),
    )
    executor.run_next()
    assert runtime.poll().snapshot == graph_gate
    assert service.progress_reporter is not None

    assert runtime.schedule_resume(
        "approve-for-progress",
        graph_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_gate.human_gate.gate_token,
    )
    attempts = (
        TaskExecutionProgressAttempt(
            task_id="TASK-002",
            attempt_number=1,
            title="Untrusted event title",
        ),
        TaskExecutionProgressAttempt(
            task_id="TASK-003",
            attempt_number=1,
            title="Another event title",
        ),
    )
    service.progress_reporter.report(
        TaskExecutionWaveStarted(
            wave_number=2,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=attempts,
        )
    )
    for attempt in attempts:
        service.progress_reporter.report(
            TaskExecutionAttemptStarted(wave_number=2, attempt=attempt)
        )

    active = runtime.poll()

    assert active.in_flight
    assert active.execution_progress is not None
    assert active.execution_progress.run_id == graph_gate.run_id
    assert active.execution_progress.operation_id == "approve-for-progress"
    assert active.execution_progress.current_wave_number == 2
    assert active.execution_progress.current_wave_mode == "PARALLEL"
    assert active.execution_progress.current_layer_numbers == (2,)
    running = [
        task
        for task in active.execution_progress.tasks
        if task.status == "RUNNING"
    ]
    assert [(task.task_id, task.title) for task in running] == [
        ("TASK-002", "Implement shortener"),
        ("TASK-003", "Build validation suite"),
    ]

    executor.run_next()
    completed = runtime.poll()

    assert not completed.in_flight
    assert completed.execution_progress is not None
    assert completed.execution_progress.telemetry_status == "OBSERVATION_COMPLETE"
    assert len(completed.execution_progress.recent_events) == 3


def test_task_graph_revision_uses_new_token_and_rejects_stale_prior_token(
    tmp_path: Path,
) -> None:
    feedback = "  Add explicit validation before documentation.\nKeep this line.  "
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal("v1"), _proposal("v2")]),
        run_suffix="streamlit-task-graph-revision",
    )
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)
    first_graph = _advance_runtime_to_task_graph(
        runtime,
        executor,
        operation_prefix="revision",
    )
    old_token = first_graph.human_gate.gate_token

    assert runtime.schedule_resume(
        "request-task-graph-changes",
        first_graph.run_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        gate_token=old_token,
    )
    executor.run_next()
    revised_view = runtime.poll()
    revised = revised_view.snapshot

    assert revised is not None
    assert revised_view.execution_progress is None
    assert revised.human_gate is not None
    assert revised.human_gate.stage == "task_graph_review"
    assert revised.human_gate.gate_token != old_token
    assert revised.workflow_state["task_graph_revision_count"] == 1
    assert len(revised.workflow_state["task_graph_history"]) == 2
    assert revised.workflow_state["task_graph_history"][1][
        "reviewer_feedback"
    ] == feedback.strip()
    assert revised.workflow_state["task_graph_review_history"][0][
        "feedback"
    ] == feedback.strip()

    with pytest.raises(GovernedRunLifecycleError, match="no longer current"):
        runtime.schedule_resume(
            "stale-task-graph-approval",
            revised.run_id,
            {"decision": "APPROVE", "feedback": ""},
            gate_token=old_token,
        )


def test_task_graph_reject_uses_real_governed_safe_stop(
    tmp_path: Path,
) -> None:
    service, _, task_executor = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="streamlit-task-graph-reject",
    )
    executor = QueuedExecutor()
    runtime = StreamlitRunRuntime(service, executor=executor)
    graph_gate = _advance_runtime_to_task_graph(
        runtime,
        executor,
        operation_prefix="reject",
    )

    assert runtime.schedule_resume(
        "reject-task-graph",
        graph_gate.run_id,
        {"decision": "REJECT", "feedback": ""},
        gate_token=graph_gate.human_gate.gate_token,
    )
    executor.run_next()
    stopped = runtime.poll().snapshot

    assert stopped is not None
    assert stopped.application_status is GovernedRunApplicationStatus.SAFE_STOPPED
    assert stopped.workflow_status == "safe_stopped"
    assert stopped.human_gate is None
    assert stopped.workflow_state["safe_stop_reason"] == TASK_GRAPH_REJECTED_REASON
    assert task_executor.calls == []


def test_background_runtime_has_no_streamlit_api_imports_or_calls() -> None:
    source_root = Path(__file__).parents[1] / "src" / "agentic_sdlc"
    for filename in (
        "streamlit_runtime.py",
        "streamlit_execution_progress.py",
    ):
        tree = ast.parse((source_root / filename).read_text(encoding="utf-8"))

        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_from_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        attribute_roots = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }

        assert "streamlit" not in imported_modules
        assert all(
            not module.startswith("streamlit")
            for module in imported_from_modules
        )
        assert "st" not in attribute_roots
