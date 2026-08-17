"""Focused tests for the shared governed run lifecycle service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunLifecycleError,
    GovernedRunRequest,
    GovernedRunService,
    UnknownGovernedRunError,
)
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_execution_progress import TaskExecutionProgressReporter
from agentic_sdlc.workflow import build_workflow
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from tests.test_workflow import (
    RecordingTaskExecutor,
    _analysis,
    _blocked_analysis,
    _clarified_analysis,
    _proposal,
    _proposal_without_ambiguity,
)
from tests.final_validation_fakes import ScriptedFinalValidationExecutor


def _service(
    tmp_path: Path,
    *,
    analyst: FakeRequirementAnalysisClient,
    planner: FakeTaskPlanningClient,
    run_suffix: str,
    run_event_log_factory: Any | None = None,
) -> tuple[GovernedRunService, GovernedWorkspaceRuntime, RecordingTaskExecutor]:
    workspace_parent = tmp_path / "isolated"
    workspace_parent.mkdir(exist_ok=True)
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)
    executor = RecordingTaskExecutor()

    def workflow_factory(
        *,
        workspace_runtime: GovernedWorkspaceRuntime,
        task_execution_progress_reporter: TaskExecutionProgressReporter,
    ) -> Any:
        assert workspace_runtime is runtime
        return build_workflow(
            analyst,
            planner,
            executor,
            validation_executor=ScriptedFinalValidationExecutor(),
            workspace_runtime=workspace_runtime,
            task_execution_progress_reporter=task_execution_progress_reporter,
        )

    def write_stub_diagram(
        output_path: Path,
        *,
        workflow: Any,
    ) -> None:
        assert workflow is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    return (
        GovernedRunService(
            repository_root=tmp_path,
            workflow_factory=workflow_factory,
            workspace_runtime_factory=lambda: runtime,
            run_id_factory=lambda command: f"{command}-{run_suffix}",
            workflow_diagram_writer=write_stub_diagram,
            run_event_log_factory=run_event_log_factory,
        ),
        runtime,
        executor,
    )


def test_start_run_creates_owned_context_and_read_only_human_gate(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="first-gate",
    )
    workflow_input = demo_input()

    snapshot = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=workflow_input)
    )
    workflow_input["project_name"] = "caller-tampered"

    assert snapshot.run_id == "demo-first-gate"
    assert snapshot.application_status is GovernedRunApplicationStatus.AWAITING_HUMAN
    assert snapshot.workflow_status == "awaiting_approval"
    assert snapshot.human_gate is not None
    assert snapshot.human_gate.gate_token == "demo-first-gate:human-gate:1"
    assert snapshot.human_gate.stage == "requirement_analysis_review"
    assert snapshot.human_gate.checkpoint == "requirement_analysis"
    assert snapshot.human_gate.allowed_decisions == (
        "APPROVE",
        "REQUEST_CHANGES",
        "REJECT",
    )
    assert snapshot.manifest_path is None
    assert snapshot.export_result is None
    assert snapshot.workflow_diagram_generated
    assert snapshot.artifact_bundle.workflow_diagram_path.is_file()

    with pytest.raises(TypeError):
        snapshot.workflow_state["workflow_status"] = "success"  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.human_gate.payload["stage"] = "task_graph_review"  # type: ignore[index]

    inspected = service.inspect_run(snapshot.run_id)
    assert inspected.workflow_state["project_name"] == "URL Shortener"
    assert inspected.workflow_status == "awaiting_approval"
    assert inspected.human_gate is not None
    assert inspected.human_gate.stage == "requirement_analysis_review"


def test_repeated_governance_resumes_preserve_authority_and_reach_export(
    tmp_path: Path,
) -> None:
    analyst = FakeRequirementAnalysisClient(
        [_blocked_analysis("blocked"), _clarified_analysis("clarified")]
    )
    planner = FakeTaskPlanningClient([_proposal_without_ambiguity()])
    service, runtime, executor = _service(
        tmp_path,
        analyst=analyst,
        planner=planner,
        run_suffix="revisions",
    )

    blocked = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )

    assert blocked.human_gate is not None
    assert blocked.human_gate.allowed_decisions == ("REQUEST_CHANGES", "REJECT")
    with pytest.raises(GovernedRunLifecycleError, match="not allowed"):
        service.resume_run(
            blocked.run_id,
            {"decision": "APPROVE", "feedback": ""},
            gate_token=blocked.human_gate.gate_token,
        )
    still_blocked = service.inspect_run(blocked.run_id)
    assert still_blocked.human_gate is not None
    assert still_blocked.human_gate.gate_token == blocked.human_gate.gate_token

    revised = service.resume_run(
        blocked.run_id,
        {
            "decision": "REQUEST_CHANGES",
            "feedback": "API clients do not authenticate in the initial scope.",
        },
        gate_token=blocked.human_gate.gate_token,
    )
    assert revised.human_gate is not None
    assert revised.human_gate.stage == "requirement_analysis_review"
    assert revised.workflow_state["requirement_analysis_revision_count"] == 1
    with pytest.raises(GovernedRunLifecycleError, match="current human gate"):
        service.resume_run(
            revised.run_id,
            {"decision": "APPROVE", "feedback": ""},
            gate_token=blocked.human_gate.gate_token,
        )

    graph_review = service.resume_run(
        revised.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=revised.human_gate.gate_token,
    )
    assert graph_review.human_gate is not None
    assert graph_review.human_gate.stage == "task_graph_review"

    terminal = service.resume_run(
        graph_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_review.human_gate.gate_token,
    )

    assert terminal.application_status is GovernedRunApplicationStatus.SUCCEEDED
    assert terminal.workflow_status == "success"
    assert terminal.human_gate is None
    assert terminal.application_error is None
    assert terminal.manifest_path is not None
    assert terminal.manifest_path.is_file()
    assert terminal.export_result is not None
    assert terminal.export_result.succeeded
    assert terminal.export_result.destination_directory is not None
    assert terminal.export_result.destination_directory.is_dir()
    assert len(executor.calls) == 5
    assert runtime.workspace_for_run(terminal.run_id).root != (
        terminal.export_result.destination_directory
    )
    manifest = json.loads(terminal.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == terminal.run_id
    assert manifest["workflow_status"] == "success"
    assert manifest["exit_gate_passed"] is True
    assert len(terminal.workflow_state["requirement_analysis_history"]) == 2
    assert len(terminal.workflow_state["requirement_review_history"]) == 2


def test_invalid_feedback_does_not_consume_active_human_gate(tmp_path: Path) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient(
            [_analysis(), _clarified_analysis("clarified after feedback")]
        ),
        planner=FakeTaskPlanningClient([_proposal_without_ambiguity()]),
        run_suffix="feedback-validation",
    )
    paused = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )

    assert paused.human_gate is not None
    gate_token = paused.human_gate.gate_token
    baseline = service.inspect_run(paused.run_id)

    for invalid_feedback in ("", "   \t\n"):
        with pytest.raises(
            GovernedRunLifecycleError,
            match="REQUEST_CHANGES requires non-empty human feedback",
        ):
            service.resume_run(
                paused.run_id,
                {"decision": "REQUEST_CHANGES", "feedback": invalid_feedback},
                gate_token=gate_token,
            )

        inspected = service.inspect_run(paused.run_id)
        assert inspected.human_gate == baseline.human_gate
        assert inspected.human_gate is not None
        assert inspected.human_gate.gate_token == gate_token
        assert inspected.workflow_state == baseline.workflow_state

    invalid_runtime_response: Any = {"decision": "APPROVE", "feedback": 7}
    with pytest.raises(GovernedRunLifecycleError, match="feedback must be a string"):
        service.resume_run(
            paused.run_id,
            invalid_runtime_response,
            gate_token=gate_token,
        )

    after_non_string = service.inspect_run(paused.run_id)
    assert after_non_string.human_gate == baseline.human_gate
    assert after_non_string.human_gate is not None
    assert after_non_string.human_gate.gate_token == gate_token
    assert after_non_string.workflow_state == baseline.workflow_state

    revised = service.resume_run(
        paused.run_id,
        {
            "decision": "REQUEST_CHANGES",
            "feedback": "Clarify the authentication scope.",
        },
        gate_token=gate_token,
    )

    assert revised.human_gate is not None
    assert revised.human_gate.stage == "requirement_analysis_review"
    assert revised.human_gate.gate_token != gate_token
    assert revised.workflow_state["requirement_analysis_revision_count"] == 1
    assert revised.workflow_state["requirement_review_history"][0]["feedback"] == (
        "Clarify the authentication scope."
    )


def test_safe_stop_writes_manifest_never_exports_and_rejects_duplicate_resume(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="rejected",
    )
    paused = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )

    stopped = service.resume_run(
        paused.run_id,
        {"decision": "REJECT", "feedback": ""},
        gate_token=paused.human_gate.gate_token,
    )

    assert stopped.application_status is GovernedRunApplicationStatus.SAFE_STOPPED
    assert stopped.workflow_status == "safe_stopped"
    assert stopped.manifest_path is not None
    assert stopped.manifest_path.is_file()
    assert stopped.export_result is None
    assert not (tmp_path / "projects").exists()
    with pytest.raises(GovernedRunLifecycleError, match="already terminal"):
        service.resume_run(
            stopped.run_id,
            {"decision": "APPROVE", "feedback": ""},
            gate_token=paused.human_gate.gate_token,
        )


def test_failed_entry_gate_is_terminal_without_manifest_or_export(tmp_path: Path) -> None:
    service, _, executor = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([]),
        planner=FakeTaskPlanningClient([]),
        run_suffix="invalid-input",
    )
    invalid_input: WorkflowState = {
        "project_name": "",
        "requirements": [],
        "raw_requirement": "",
    }

    failed = service.start_run(
        GovernedRunRequest(command="run", workflow_input=invalid_input)
    )

    assert failed.application_status is GovernedRunApplicationStatus.FAILED
    assert failed.workflow_status == "entry_gate_failed"
    assert failed.is_terminal
    assert failed.human_gate is None
    assert failed.manifest_path is None
    assert failed.export_result is None
    assert executor.calls == []
    assert not (tmp_path / "projects").exists()


def test_unknown_and_duplicate_run_ids_are_rejected(tmp_path: Path) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis(), _analysis()]),
        planner=FakeTaskPlanningClient([_proposal(), _proposal()]),
        run_suffix="fixed-id",
    )

    with pytest.raises(UnknownGovernedRunError, match="missing"):
        service.inspect_run("missing")
    with pytest.raises(UnknownGovernedRunError, match="missing"):
        service.resume_run(
            "missing",
            {"decision": "APPROVE", "feedback": ""},
            gate_token="missing:human-gate:1",
        )

    service.start_run(GovernedRunRequest(command="demo", workflow_input=demo_input()))
    with pytest.raises(GovernedRunLifecycleError, match="already exists"):
        service.start_run(
            GovernedRunRequest(command="demo", workflow_input=demo_input())
        )
