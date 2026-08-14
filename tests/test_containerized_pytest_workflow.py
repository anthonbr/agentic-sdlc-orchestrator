"""Workflow-level Docker pytest success gating and existing retry integration."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from pytest import MonkeyPatch

from agentic_sdlc.artifacts import write_artifacts
from agentic_sdlc.nodes import exit_gate
from agentic_sdlc.docker_validation import DockerPytestValidationExecutor
from agentic_sdlc.task_execution import (
    TaskExecutionRecoveryAction,
    TaskExecutionStatus,
)
from agentic_sdlc.task_execution_contracts import TaskExecutionResult
from agentic_sdlc.task_graph import (
    ProposedTaskGraph,
    ProposedTaskValidationRequirement,
    TaskMaterializationPolicy,
    ValidationExecutionProfile,
)
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from tests.test_containerized_pytest_validation import (
    ScriptedDockerRunner,
    _operation,
)
from tests.test_task_execution_workflow import (
    MaterializingExecutor,
    _run_approved,
    _task,
)


class RepairingCandidateExecutor(MaterializingExecutor):
    """Return a different governed candidate after validation feedback."""

    def execute(self, request) -> TaskExecutionResult:
        result = super().execute(request)
        output = result.outputs[0].model_copy(
            update={"content": f"VALUE = {request.attempt_number}\n"}
        )
        return result.model_copy(update={"outputs": (output,)})


class WorkspaceObservingDockerRunner(ScriptedDockerRunner):
    """Prove each staged copy precedes any mutation of the live workspace."""

    def __init__(self, live_file: Path) -> None:
        super().__init__(pytest_exit_codes=(1, 0))
        self.live_file = live_file
        self.live_contents_at_copy: list[str] = []
        self.staged_contents_at_copy: list[str] = []

    def run(self, argv, **kwargs):
        if _operation(argv) == "cp":
            source = Path(argv[2][:-2])
            self.live_contents_at_copy.append(self.live_file.read_text())
            self.staged_contents_at_copy.append((source / "src/candidate.py").read_text())
        return super().run(argv, **kwargs)


class CopySetObservingDockerRunner(ScriptedDockerRunner):
    """Record the exact per-task staged files copied into each container."""

    def __init__(self) -> None:
        super().__init__()
        self.copied_path_sets: list[tuple[str, ...]] = []

    def run(self, argv, **kwargs):
        if _operation(argv) == "cp":
            source = Path(argv[2][:-2])
            self.copied_path_sets.append(
                tuple(
                    sorted(
                        str(path.relative_to(source))
                        for path in source.rglob("*")
                        if path.is_file()
                    )
                )
            )
        return super().run(argv, **kwargs)


def _pytest_proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(
        tasks=[
            _task(
                "execute_generated_tests",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                required_validations=[
                    ProposedTaskValidationRequirement(
                        profile=ValidationExecutionProfile.PYTHON_PYTEST
                    )
                ],
            )
        ]
    )


def test_failed_first_pytest_attempt_retries_then_passes_before_live_mutation(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-repair-story"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "src").mkdir()
    (live.root / "tests").mkdir()
    (live.root / "src/candidate.py").write_text("VALUE = 0\n")
    (live.root / "tests/test_candidate.py").write_text(
        "from candidate import VALUE\n\ndef test_value(): assert VALUE == 2\n"
    )
    (live.root / "pyproject.toml").write_text(
        "[project]\nname='candidate'\nversion='0.1.0'\ndependencies=[]\n"
    )
    runner = WorkspaceObservingDockerRunner(live.root / "src/candidate.py")
    validator = DockerPytestValidationExecutor(
        runner=runner,
        docker_executable="/application/docker",
    )

    result = _run_approved(
        _pytest_proposal(),
        RepairingCandidateExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=validator,
    )

    assert result["workflow_status"] == "success"
    assert result["task_graph_execution"].task_states[0].status is (
        TaskExecutionStatus.SUCCEEDED
    )
    assert result["task_graph_execution"].task_states[0].attempt_count == 2
    assert runner.live_contents_at_copy == ["VALUE = 0\n", "VALUE = 0\n"]
    assert runner.staged_contents_at_copy == ["VALUE = 1\n", "VALUE = 2\n"]
    assert (live.root / "src/candidate.py").read_text() == "VALUE = 2\n"
    assert len(result["workspace_mutation_results"]) == 1
    assert len(result["task_validation_provisioning_evidence"]) == 2
    execution_evidence = result["task_validation_execution_evidence"]
    assert [item.passed for item in execution_evidence] == [False, True]
    recovery = result["task_execution_recovery_decisions"][0]
    assert recovery.action is TaskExecutionRecoveryAction.RETRY
    assert recovery.feedback.startswith(
        "Untrusted validation diagnostics from the previous governed execution"
    )
    assert result["task_execution_requests"][1].retry_context is not None
    assert result["task_execution_requests"][1].retry_context.feedback == (
        recovery.feedback
    )
    final_exit = result["task_attempt_exit_decisions"][-1]
    final_provisioning = result["task_validation_provisioning_evidence"][-1]
    final_execution = execution_evidence[-1]
    assert final_provisioning.evidence_id in final_exit.evidence_ids
    assert final_execution.evidence_id in final_exit.evidence_ids
    readiness = result["project_readiness_validation"]
    assert readiness.runtime_validation_required is True
    assert readiness.runtime_validation_verified_count == 1
    assert readiness.runtime_execution_verified is True
    assert readiness.python_compile_verified_count == 0
    assert readiness.python_pytest_verified_count == 1
    assert readiness.dependency_provisioning_verified_count == 1


def test_docker_cleanup_failure_is_nonretryable_and_leaves_live_workspace_unchanged(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-cleanup-failure"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_candidate.py").write_text("def test_ok(): assert True\n")
    validator = DockerPytestValidationExecutor(
        runner=ScriptedDockerRunner(cleanup_failure=True),
        docker_executable="/application/docker",
    )

    result = _run_approved(
        _pytest_proposal(),
        RepairingCandidateExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=validator,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].task_states[0].attempt_count == 1
    assert result["task_execution_recovery_decisions"][0].retryable is False
    assert not (live.root / "src/candidate.py").exists()
    assert result.get("workspace_mutation_results", []) == []


def test_pip_failure_uses_existing_bounded_task_agent_retry(tmp_path: Path) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-provisioning-retry"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_candidate.py").write_text("def test_ok(): assert True\n")
    validator = DockerPytestValidationExecutor(
        runner=ScriptedDockerRunner(pip_exit_codes=(1, 0)),
        docker_executable="/application/docker",
    )

    result = _run_approved(
        _pytest_proposal(),
        RepairingCandidateExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=validator,
    )

    assert result["workflow_status"] == "success"
    assert result["task_graph_execution"].task_states[0].attempt_count == 2
    assert [
        item.passed for item in result["task_validation_provisioning_evidence"]
    ] == [False, True]
    assert len(result["task_validation_execution_evidence"]) == 1
    decision = result["task_execution_recovery_decisions"][0]
    assert decision.action is TaskExecutionRecoveryAction.RETRY
    assert decision.feedback.startswith(
        "Untrusted dependency-provisioning diagnostics from the previous governed "
        "execution"
    )


def test_same_wave_pytest_containers_receive_only_their_own_candidate(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-same-wave-isolation"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_placeholder.py").write_text("def test_ok(): pass\n")
    runner = CopySetObservingDockerRunner()
    requirement = [
        ProposedTaskValidationRequirement(
            profile=ValidationExecutionProfile.PYTHON_PYTEST
        )
    ]
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                required_validations=requirement,
            ),
            _task(
                "second",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                required_validations=requirement,
            ),
        ]
    )

    result = _run_approved(
        proposal,
        MaterializingExecutor(
            {"TASK-001": "src/first.py", "TASK-002": "src/second.py"}
        ),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=DockerPytestValidationExecutor(
            runner=runner,
            docker_executable="/application/docker",
        ),
    )

    assert result["workflow_status"] == "success"
    assert runner.copied_path_sets == [
        ("src/first.py", "tests/test_placeholder.py"),
        ("src/second.py", "tests/test_placeholder.py"),
    ]


def test_pytest_authority_and_provisioning_serialize_in_existing_run_evidence(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-artifacts"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_placeholder.py").write_text("def test_ok(): pass\n")
    result = _run_approved(
        _pytest_proposal(),
        RepairingCandidateExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=DockerPytestValidationExecutor(
            runner=ScriptedDockerRunner(),
            docker_executable="/application/docker",
        ),
    )

    output = tmp_path / "evidence"
    write_artifacts(result, output)
    task_evidence = json.loads((output / "task_execution.json").read_text())
    summary = (output / "summary.md").read_text()
    graph_review = (output / "task_graph.md").read_text()

    assert len(task_evidence["validation_provisioning"]) == 1
    assert len(task_evidence["validation_executions"]) == 1
    assert task_evidence["validation_executions"][0][
        "provisioning_evidence_ids"
    ] == [task_evidence["validation_provisioning"][0]["evidence_id"]]
    assert "Required validations: PYTHON_PYTEST" in graph_review
    assert "PYTHON_PYTEST validation executed: yes" in summary
    assert "Dependencies provisioned for validation: yes" in summary
    assert "Generated tests executed: yes" in summary
    assert "Benchmarks executed: no" in summary

    missing_provisioning = deepcopy(result)
    missing_provisioning["task_validation_provisioning_evidence"] = []
    failed_exit = exit_gate(missing_provisioning)
    assert failed_exit["workflow_status"] == "exit_gate_failed"
    assert failed_exit["project_readiness_validation"].runtime_execution_verified is False

    missing_provisioning_exit = deepcopy(result)
    decision = missing_provisioning_exit["task_attempt_exit_decisions"][-1]
    provisioning_id = result["task_validation_provisioning_evidence"][0].evidence_id
    missing_provisioning_exit["task_attempt_exit_decisions"][-1] = decision.model_copy(
        update={
            "evidence_ids": tuple(
                item for item in decision.evidence_ids if item != provisioning_id
            )
        }
    )
    assert exit_gate(missing_provisioning_exit)["workflow_status"] == (
        "exit_gate_failed"
    )


def test_default_workflow_selects_docker_backend_only_for_approved_pytest_profile(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "containerized-pytest-default-backend"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_placeholder.py").write_text("def test_ok(): pass\n")
    validator = DockerPytestValidationExecutor(
        runner=ScriptedDockerRunner(),
        docker_executable="/application/docker",
    )
    monkeypatch.setattr(
        "agentic_sdlc.nodes.DockerPytestValidationExecutor",
        lambda: validator,
    )

    result = _run_approved(
        _pytest_proposal(),
        RepairingCandidateExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=thread_id,
    )

    assert result["workflow_status"] == "success"
    assert len(result["task_validation_execution_evidence"]) == 1
