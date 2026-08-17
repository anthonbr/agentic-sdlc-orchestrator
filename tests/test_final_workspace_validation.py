"""Application-required final-workspace validation and publication gating."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunRequest,
    GovernedRunService,
)
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.nodes import exit_gate
from agentic_sdlc.project_delivery import ProjectDeliverableRole
from agentic_sdlc.state import demo_input
from agentic_sdlc.task_execution_contracts import TaskExecutionResult
from agentic_sdlc.validation_execution_contracts import ValidationExecutionOutcome
from agentic_sdlc.workflow import build_workflow, resume_workflow
from agentic_sdlc.workspace_contracts import (
    WorkspaceFileState,
    workspace_file_content_hash,
)
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from tests.final_validation_fakes import ScriptedFinalValidationExecutor
from tests.test_workflow import (
    RecordingTaskExecutor,
    _analysis,
    _approve_requirements,
    _proposal,
    _replace_authoritative_snapshot,
    _start_demo,
)


class BrokenGeneratedTestsExecutor(RecordingTaskExecutor):
    """Materialize a collection-time defect like the real generated project."""

    def execute(self, request: Any) -> TaskExecutionResult:
        result = super().execute(request)
        if ProjectDeliverableRole.AUTOMATED_TESTS not in request.task.deliverable_roles:
            return result
        outputs = tuple(
            output.model_copy(
                update={
                    "content": (
                        "from tests.missing_adapter import provider\n\n"
                        "def test_provider(): assert provider()\n"
                    )
                }
            )
            for output in result.outputs
        )
        return result.model_copy(update={"outputs": outputs})


def _service(
    tmp_path: Path,
    *,
    executor: RecordingTaskExecutor,
    validator: ScriptedFinalValidationExecutor,
    run_id: str,
) -> GovernedRunService:
    workspace_parent = tmp_path / "isolated"
    workspace_parent.mkdir()
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)

    def workflow_factory(
        *, workspace_runtime: GovernedWorkspaceRuntime, task_execution_progress_reporter: Any
    ) -> Any:
        return build_workflow(
            FakeRequirementAnalysisClient([_analysis()]),
            FakeTaskPlanningClient([_proposal()]),
            executor,
            validation_executor=validator,
            workspace_runtime=workspace_runtime,
            task_execution_progress_reporter=task_execution_progress_reporter,
        )

    return GovernedRunService(
        repository_root=tmp_path,
        workflow_factory=workflow_factory,
        workspace_runtime_factory=lambda: runtime,
        run_id_factory=lambda _command: run_id,
        workflow_diagram_writer=lambda _path, *, workflow: None,
    )


def _complete(service: GovernedRunService, *, project_name: str) -> Any:
    first = service.start_run(
        GovernedRunRequest(
            command="run",
            workflow_input={
                **demo_input(),
                "project_name": project_name,
            },
            requested_project_name=project_name,
        )
    )
    assert first.human_gate is not None
    graph = service.resume_run(
        first.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=first.human_gate.gate_token,
    )
    assert graph.human_gate is not None
    return service.resume_run(
        first.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph.human_gate.gate_token,
    )


def test_task_pytest_cannot_replace_failing_final_workspace_pytest(
    tmp_path: Path,
) -> None:
    validator = ScriptedFinalValidationExecutor(
        pytest_outcome=ValidationExecutionOutcome.FAILED,
        task_pytest_outcome=ValidationExecutionOutcome.PASSED,
    )
    service = _service(
        tmp_path,
        executor=BrokenGeneratedTestsExecutor(),
        validator=validator,
        run_id="final-validation-failure",
    )

    result = _complete(service, project_name="must-not-publish")

    graph = result.workflow_state["approved_task_graph"]
    automated_tests = next(
        task
        for task in graph["tasks"]
        if "AUTOMATED_TESTS" in task["deliverable_roles"]
    )
    assert [
        requirement["profile"]
        for requirement in automated_tests["required_validations"]
    ] == ["PYTHON_PYTEST"]
    assert result.application_status is GovernedRunApplicationStatus.SAFE_STOPPED
    assert result.workflow_status == "safe_stopped"
    assert result.export_result is None
    assert not (tmp_path / "projects" / "must-not-publish").exists()
    evidence = result.workflow_state[
        "final_workspace_validation_execution_evidence"
    ]
    assert [item.profile.value for item in evidence] == [
        "PYTHON_COMPILE",
        "PYTHON_PYTEST",
    ]
    assert [item.passed for item in evidence] == [True, False]
    assert "tests.missing_adapter" in validator.observed_contents[2][
        "tests/test_service.py"
    ]
    assert "PYTHON_PYTEST returned FAILED" in result.workflow_state["safe_stop_reason"]
    assert result.workflow_state["task_execution_recovery_decisions"] == ()


def test_task_pytest_preserves_independent_final_validation_before_publication(
    tmp_path: Path,
) -> None:
    validator = ScriptedFinalValidationExecutor()
    service = _service(
        tmp_path,
        executor=RecordingTaskExecutor(),
        validator=validator,
        run_id="final-validation-pass",
    )

    result = _complete(service, project_name="validated-project")

    assert result.application_status is GovernedRunApplicationStatus.SUCCEEDED
    assert result.export_result is not None and result.export_result.succeeded
    assert (tmp_path / "projects" / "validated-project" / "README.md").is_file()
    readiness = result.workflow_state["project_readiness_validation"]
    assert readiness.final_workspace_validation_required_count == 2
    assert readiness.final_workspace_validation_verified_count == 2
    assert readiness.final_workspace_validation_verified is True
    assert readiness.final_workspace_snapshot_id == result.workflow_state[
        "governed_workspace_session"
    ].authoritative_snapshot_id
    assert [call.requirement.profile.value for call in validator.calls] == [
        "PYTHON_PYTEST",
        "PYTHON_COMPILE",
        "PYTHON_PYTEST",
    ]
    final_snapshot_id = result.workflow_state[
        "governed_workspace_session"
    ].authoritative_snapshot_id
    assert all(
        item.source_snapshot_id == final_snapshot_id
        and item.task_id == "TASK-000"
        for item in result.workflow_state[
            "final_workspace_validation_execution_evidence"
        ]
    )
    task_validation = result.workflow_state[
        "task_validation_execution_evidence"
    ]
    assert len(task_validation) == 1
    assert task_validation[0].profile.value == "PYTHON_PYTEST"
    assert task_validation[0].passed is True
    published_evidence = tmp_path / "projects" / "validated-project" / (
        "sdlc-artifacts"
    )
    task_evidence = json.loads(
        (published_evidence / "task_execution.json").read_text()
    )
    assert len(task_evidence["final_workspace_validation_executions"]) == 2
    assert len(task_evidence["final_workspace_validation_provisioning"]) == 1
    summary = (published_evidence / "summary.md").read_text()
    assert "Governed required validations: 3 passed / 3 required" in summary
    assert "Planner-requested task validations: 1 required" in summary
    assert "Application-required final-workspace validations: 2 required" in summary


def test_stale_final_validation_evidence_cannot_authorize_modified_snapshot() -> None:
    validator = ScriptedFinalValidationExecutor()
    workflow, thread_id, _, _, _ = _start_demo(
        final_validation_executor=validator
    )
    _approve_requirements(workflow, thread_id)
    complete = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )
    session = complete["governed_workspace_session"]
    final_snapshot = next(
        item
        for item in complete["workspace_snapshots"]
        if item.snapshot_id == session.authoritative_snapshot_id
    )
    modified = _replace_authoritative_snapshot(
        complete,
        (
            *final_snapshot.files,
            WorkspaceFileState(
                path="src/after-validation.py",
                content_hash=workspace_file_content_hash("VALUE = 2\n"),
            ),
        ),
    )

    result = exit_gate(modified)

    assert result["workflow_status"] == "exit_gate_failed"
    readiness = result["project_readiness_validation"]
    assert readiness.final_workspace_validation_required is True
    assert readiness.final_workspace_validation_verified is False
    assert readiness.final_workspace_validation_verified_count == 0
    assert len(validator.calls) == 3
