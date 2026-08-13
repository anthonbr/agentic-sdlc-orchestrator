"""Deterministic tests for the Streamlit session background runtime."""

from __future__ import annotations

import ast
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

import pytest

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
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
from agentic_sdlc.state import ApprovalResponse, demo_input
from agentic_sdlc.streamlit_runtime import (
    StreamlitOperationKind,
    StreamlitRunRuntime,
    governed_run_request_from_inline_requirement,
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


class RecordingService:
    def __init__(
        self,
        start_snapshot: GovernedRunSnapshot,
        resume_snapshots: list[GovernedRunSnapshot],
    ) -> None:
        self.start_snapshot = start_snapshot
        self.resume_snapshots = deque(resume_snapshots)
        self.current_snapshot: GovernedRunSnapshot | None = None
        self.start_calls: list[GovernedRunRequest] = []
        self.resume_calls: list[tuple[str, ApprovalResponse, str]] = []
        self.inspect_calls: list[str] = []

    def start_run(self, request: GovernedRunRequest) -> GovernedRunSnapshot:
        self.start_calls.append(request)
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
) -> GovernedRunSnapshot:
    analysis = {
        "normalized_problem_statement": f"revision-{revision}: Build a scheduler.",
        "requirement_type": "greenfield",
        "functional_requirements": ["Create scheduled jobs."],
        "nonfunctional_requirements": ["Execute jobs reliably."],
        "constraints": ["Use the existing runtime."],
        "ambiguities": [],
        "assumptions": ["Users supply valid schedules."],
        "acceptance_criteria": ["A scheduled job runs."],
        "risks": ["Clock drift can delay a job."],
        "needs_clarification": False,
        "confidence": 0.91,
    }
    payload = {
        "stage": stage,
        "checkpoint": (
            "requirement_analysis"
            if stage == "requirement_analysis_review"
            else "task_graph"
        ),
        "allowed_decisions": list(allowed_decisions),
        "revision_number": revision,
        "requirement_analysis": analysis,
        "planning_readiness": {
            "analysis_revision": revision,
            "status": "READY",
            "needs_clarification": False,
            "blocking_ambiguities": [],
            "reason_code": None,
        },
    }
    gate = (
        HumanGovernanceGate(
            gate_token=gate_token,
            stage=stage,
            checkpoint=payload["checkpoint"],
            allowed_decisions=allowed_decisions,  # type: ignore[arg-type]
            payload=payload,
        )
        if gate_token is not None
        else None
    )
    workflow_state: dict[str, Any] = {
        "workflow_status": workflow_status,
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


def test_inline_request_uses_submission_evidence_and_deterministic_name() -> None:
    original = "\ufeff  Build a scheduler.\r\n\r\n- Run jobs.  \r\n"
    submission = resolve_inline_requirement(original)

    request = governed_run_request_from_inline_requirement(original, "")

    assert request.command == "run"
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


@pytest.mark.parametrize("requirement", ["", "  \t\r\n"])
def test_empty_inline_request_fails_before_a_runtime_can_schedule(
    requirement: str,
) -> None:
    with pytest.raises(RequirementSubmissionError, match="non-whitespace"):
        governed_run_request_from_inline_requirement(requirement, "")


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


def test_background_runtime_has_no_streamlit_api_imports_or_calls() -> None:
    runtime_path = (
        Path(__file__).parents[1]
        / "src"
        / "agentic_sdlc"
        / "streamlit_runtime.py"
    )
    tree = ast.parse(runtime_path.read_text(encoding="utf-8"))

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
    assert all(not module.startswith("streamlit") for module in imported_from_modules)
    assert "st" not in attribute_roots
