"""Governed required-validation success gating, recovery, and evidence tests."""

from __future__ import annotations

import json
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import cast
from uuid import uuid4

from pytest import MonkeyPatch, mark

from agentic_sdlc.artifacts import write_artifacts
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.nodes import exit_gate
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_execution import (
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionStatus,
)
from agentic_sdlc.task_execution_progress import (
    TaskValidationExecutionCompleted,
    TaskValidationExecutionStarted,
)
from agentic_sdlc.task_graph import (
    ProposedTaskGraph,
    ProposedTaskValidationRequirement,
    TaskMaterializationPolicy,
    ValidationExecutionProfile,
)
from agentic_sdlc.validation_execution import (
    GovernedValidationExecutor,
    PythonCompileValidationExecutor,
    ValidationExecutionInfrastructureCode,
    ValidationExecutionInfrastructureError,
)
from agentic_sdlc.validation_execution_contracts import (
    GovernedValidationPolicy,
    TaskValidationExecutionEvidence,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    build_validation_execution_evidence,
    python_compile_validation_policy,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    WorkspaceRuntimeIssueCode,
    snapshot_isolated_workspace,
)
from tests.test_task_execution_workflow import (
    MaterializingExecutor,
    RecordingProgressReporter,
    _analysis,
    _run_approved,
    _task,
)


class ScriptedValidationExecutor:
    """Return trusted deterministic evidence without launching a real process."""

    def __init__(
        self,
        outcomes: dict[str, tuple[str, ...]] | None = None,
        *,
        create_side_effects: bool = False,
    ) -> None:
        self.outcomes = outcomes or {}
        self.create_side_effects = create_side_effects
        self.calls: list[ValidationExecutionRequest] = []
        self.observed_paths: list[tuple[str, tuple[str, ...]]] = []
        self.observed_contents: list[tuple[str, dict[str, str]]] = []

    def execute(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        workspace: IsolatedWorkspace,
    ) -> TaskValidationExecutionEvidence:
        self.calls.append(request)
        snapshot = snapshot_isolated_workspace(workspace)
        paths = tuple(item.path for item in snapshot.files)
        self.observed_paths.append((request.task_id, paths))
        self.observed_contents.append(
            (
                request.task_id,
                {
                    path: (workspace.root / path).read_text()
                    for path in paths
                },
            )
        )
        configured = self.outcomes.get(request.task_id, ("pass",))
        outcome_name = configured[
            min(request.attempt_number - 1, len(configured) - 1)
        ]
        infrastructure_codes = {
            "infrastructure": ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "process_start": ValidationExecutionInfrastructureCode.PROCESS_START,
            "output_capture": ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
        }
        if outcome_name in infrastructure_codes:
            raise ValidationExecutionInfrastructureError(
                infrastructure_codes[outcome_name],
                "Controlled validation backend outage.",
            )
        if outcome_name == "unexpected":
            raise RuntimeError("Unexpected controlled backend defect.")
        if self.create_side_effects:
            (workspace.root / "validation-created.txt").write_text("disposable\n")
            for path in paths:
                if path.endswith(".py"):
                    (workspace.root / path).write_text("VALIDATION SIDE EFFECT\n")
                    break
        if outcome_name == "timeout":
            outcome = ValidationExecutionOutcome.TIMED_OUT
            exit_code = -15
        elif outcome_name == "fail":
            outcome = ValidationExecutionOutcome.FAILED
            exit_code = 1
        else:
            outcome = ValidationExecutionOutcome.PASSED
            exit_code = 0
        evidence = build_validation_execution_evidence(
            request,
            policy,
            started_at="2026-08-14T12:00:00+00:00",
            ended_at="2026-08-14T12:00:01+00:00",
            duration_seconds=1.0,
            outcome=outcome,
            exit_code=exit_code,
            stdout_total_bytes=0,
            stderr_total_bytes=(
                len(b"controlled syntax diagnostic")
                if outcome is not ValidationExecutionOutcome.PASSED
                else 0
            ),
            retained_stdout="",
            retained_stderr=(
                "controlled syntax diagnostic"
                if outcome is not ValidationExecutionOutcome.PASSED
                else ""
            ),
            stdout_sha256=(
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            stderr_sha256=(
                hashlib.sha256(b"controlled syntax diagnostic").hexdigest()
                if outcome is not ValidationExecutionOutcome.PASSED
                else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            stdout_truncated=False,
            stderr_truncated=False,
        )
        if outcome_name == "mismatch":
            return evidence.model_copy(update={"staged_snapshot_id": "STALE"})
        if outcome_name == "missing":
            return cast(TaskValidationExecutionEvidence, None)
        return evidence


def _required_task(
    key: str = "compile_candidate",
    *,
    depends_on: list[str] | None = None,
) -> object:
    return _task(
        key,
        depends_on=depends_on or [],
        materialization_policy=TaskMaterializationPolicy.REQUIRED,
        required_validations=[
            ProposedTaskValidationRequirement(
                profile=ValidationExecutionProfile.PYTHON_COMPILE
            )
        ],
    )


def _proposal() -> ProposedTaskGraph:
    return ProposedTaskGraph(tasks=[_required_task()])


def _run(
    validation_executor: GovernedValidationExecutor,
    *,
    runtime: GovernedWorkspaceRuntime | None = None,
    contents: str = "VALUE = 1\n",
) -> WorkflowState:
    return _run_approved(
        _proposal(),
        MaterializingExecutor(
            {"TASK-001": "src/candidate.py"},
            contents={"TASK-001": contents},
        ),
        workspace_runtime=runtime,
        validation_executor=validation_executor,
    )


def test_human_review_and_approved_graph_retain_exact_validation_authority() -> None:
    proposal = _proposal()
    workflow = build_workflow(
        FakeRequirementAnalysisClient([_analysis()]),
        FakeTaskPlanningClient([proposal]),
        MaterializingExecutor({"TASK-001": "src/candidate.py"}),
        validation_executor=ScriptedValidationExecutor(),
    )
    thread_id = uuid4().hex
    initial = demo_input()
    initial["project_delivery_policy"] = {"mode": "ENGINEERING_ARTIFACTS"}
    analysis_gate = run_workflow(initial, thread_id=thread_id, workflow=workflow)
    graph_gate = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )
    reviewed = graph_gate["__interrupt__"][0].value["candidate_task_graph"]

    assert reviewed["tasks"][0]["required_validations"] == [
        {
            "requirement_id": "TASK-001-VALIDATION-001",
            "profile": "PYTHON_COMPILE",
        }
    ]
    complete = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )
    assert complete["approved_task_graph"]["tasks"][0][
        "required_validations"
    ] == reviewed["tasks"][0]["required_validations"]


def test_task_without_required_validation_never_initializes_process_backend(
    monkeypatch: MonkeyPatch,
) -> None:
    def forbidden_backend() -> None:
        raise AssertionError("No-validation tasks must preserve existing behavior.")

    monkeypatch.setattr(
        "agentic_sdlc.nodes.PythonCompileValidationExecutor",
        forbidden_backend,
    )
    result = _run_approved(
        ProposedTaskGraph(
            tasks=[
                _task(
                    "legacy",
                    depends_on=[],
                    materialization_policy=TaskMaterializationPolicy.REQUIRED,
                )
            ]
        ),
        MaterializingExecutor({"TASK-001": "src/legacy.py"}),
    )

    assert result["workflow_status"] == "success"
    assert result.get("task_validation_execution_evidence", []) == []


def test_required_validation_passes_before_live_mutation_and_task_success() -> None:
    runtime = GovernedWorkspaceRuntime()
    validator = ScriptedValidationExecutor(create_side_effects=True)

    result = _run(validator, runtime=runtime, contents="VALUE = 42\n")

    assert result["workflow_status"] == "success"
    assert result["task_graph_execution"].task_states[0].status is (
        TaskExecutionStatus.SUCCEEDED
    )
    evidence = result["task_validation_execution_evidence"]
    assert len(evidence) == 1 and evidence[0].passed is True
    assert evidence[0].evidence_id in result["task_attempt_exit_decisions"][
        0
    ].evidence_ids
    workspace = runtime.workspace_for_run(result["run_id"])
    assert (workspace.root / "src/candidate.py").read_text() == "VALUE = 42\n"
    assert not (workspace.root / "validation-created.txt").exists()
    readiness = result["project_readiness_validation"]
    assert readiness.runtime_validation_required is True
    assert readiness.runtime_validation_required_count == 1
    assert readiness.runtime_validation_verified_count == 1
    assert readiness.runtime_execution_verified is True


def test_real_python_compile_workflow_discards_bytecode_side_effects(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)

    result = _run(PythonCompileValidationExecutor(), runtime=runtime)

    assert result["workflow_status"] == "success"
    assert result["task_validation_execution_evidence"][0].passed is True
    live = runtime.workspace_for_run(result["run_id"])
    assert (live.root / "src/candidate.py").read_text() == "VALUE = 1\n"
    assert not tuple(live.root.rglob("__pycache__"))
    assert not tuple(live.root.rglob("*.pyc"))


def test_failed_validation_retries_with_untrusted_diagnostics_then_succeeds() -> None:
    validator = ScriptedValidationExecutor(
        {"TASK-001": ("fail", "pass")}
    )

    result = _run(validator)

    assert result["workflow_status"] == "success"
    assert len(validator.calls) == 2
    decision = result["task_execution_recovery_decisions"][0]
    assert decision.action is TaskExecutionRecoveryAction.RETRY
    assert decision.failure_kind is (
        TaskExecutionRecoveryFailureKind.VALIDATION_EXECUTION
    )
    assert decision.feedback.startswith(
        "Untrusted validation diagnostics from the previous governed execution"
    )
    second_request = result["task_execution_requests"][1]
    assert second_request.retry_context is not None
    assert second_request.retry_context.feedback == decision.feedback
    assert result["task_graph_execution"].task_states[0].attempt_count == 2


def test_timeout_with_trusted_cleanup_uses_existing_repair_retry() -> None:
    validator = ScriptedValidationExecutor(
        {"TASK-001": ("timeout", "pass")}
    )

    result = _run(validator)

    assert result["workflow_status"] == "success"
    first = result["task_validation_execution_evidence"][0]
    assert first.outcome is ValidationExecutionOutcome.TIMED_OUT
    assert first.timed_out is True
    assert result["task_execution_recovery_decisions"][0].retryable is True


def test_retry_exhaustion_preserves_terminal_safe_stop_and_live_workspace() -> None:
    runtime = GovernedWorkspaceRuntime()
    validator = ScriptedValidationExecutor(
        {"TASK-001": ("fail", "fail", "fail")},
        create_side_effects=True,
    )

    result = _run(validator, runtime=runtime)

    assert result["workflow_status"] == "safe_stopped"
    assert result["task_graph_execution"].task_states[0].attempt_count == 3
    assert len(validator.calls) == 3
    assert result["task_execution_recovery_decisions"][-1].action is (
        TaskExecutionRecoveryAction.FAIL_TASK
    )
    workspace = runtime.workspace_for_run(result["run_id"])
    assert not (workspace.root / "src/candidate.py").exists()
    assert not (workspace.root / "validation-created.txt").exists()


@mark.parametrize(
    "outcome",
    [
        "infrastructure",
        "process_start",
        "output_capture",
        "unexpected",
        "mismatch",
        "missing",
    ],
)
def test_infrastructure_or_evidence_integrity_failure_fails_closed_without_retry(
    outcome: str,
) -> None:
    runtime = GovernedWorkspaceRuntime()
    validator = ScriptedValidationExecutor({"TASK-001": (outcome,)})

    result = _run(validator, runtime=runtime)

    assert result["workflow_status"] == "safe_stopped"
    assert len(validator.calls) == 1
    assert result["task_graph_execution"].task_states[0].attempt_count == 1
    decisions = result["task_execution_recovery_decisions"]
    assert len(decisions) == 1
    assert decisions[0].retryable is False
    assert decisions[0].action is TaskExecutionRecoveryAction.FAIL_TASK
    workspace = runtime.workspace_for_run(result["run_id"])
    assert not (workspace.root / "src/candidate.py").exists()


def test_staged_workspace_cleanup_failure_fails_closed_without_live_mutation(
    monkeypatch: MonkeyPatch,
) -> None:
    runtime = GovernedWorkspaceRuntime()
    validator = ScriptedValidationExecutor()
    from agentic_sdlc.validation_execution import discard_isolated_workspace

    def fail_cleanup(staged_workspace: IsolatedWorkspace) -> None:
        discard_isolated_workspace(staged_workspace)
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_DESTRUCTION,
            "Controlled cleanup failure.",
        )

    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.discard_isolated_workspace",
        fail_cleanup,
    )
    result = _run(validator, runtime=runtime)

    assert result["workflow_status"] == "safe_stopped"
    assert len(validator.calls) == 1
    assert result["task_execution_recovery_decisions"][0].retryable is False
    workspace = runtime.workspace_for_run(result["run_id"])
    assert not (workspace.root / "src/candidate.py").exists()


def test_parallel_same_wave_staging_cannot_observe_peer_candidate_changes(
    tmp_path: Path,
) -> None:
    proposal = ProposedTaskGraph(
        tasks=[_required_task("first"), _required_task("second")]
    )
    validator = ScriptedValidationExecutor()
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    thread_id = "validation-parallel-isolation"
    live = runtime.establish_workspace_for_run(thread_id)
    (live.root / "src").mkdir()
    (live.root / "src/authoritative.py").write_text("BASELINE = True\n")
    result = _run_approved(
        proposal,
        MaterializingExecutor(
            {"TASK-001": "src/first.py", "TASK-002": "src/second.py"},
            contents={"TASK-001": "FIRST = 1\n", "TASK-002": "SECOND = 2\n"},
        ),
        workspace_runtime=runtime,
        thread_id=thread_id,
        validation_executor=validator,
    )

    assert result["workflow_status"] == "success"
    observed = dict(validator.observed_paths)
    assert observed["TASK-001"] == (
        "src/authoritative.py",
        "src/first.py",
    )
    assert observed["TASK-002"] == (
        "src/authoritative.py",
        "src/second.py",
    )


def test_missing_or_stale_required_evidence_cannot_pass_final_exit_gate() -> None:
    validator = ScriptedValidationExecutor({"TASK-001": ("fail", "pass")})
    complete = _run(validator)
    final_evidence = complete["task_validation_execution_evidence"][-1]

    missing = deepcopy(complete)
    missing["task_validation_execution_evidence"] = []
    missing_result = exit_gate(missing)
    assert missing_result["workflow_status"] == "exit_gate_failed"

    stale = deepcopy(complete)
    stale["task_validation_execution_evidence"] = [
        complete["task_validation_execution_evidence"][0]
    ]
    stale_result = exit_gate(stale)
    assert stale_result["workflow_status"] == "exit_gate_failed"

    final_request = validator.calls[-1]
    evidence_observations = {
        "started_at": final_evidence.started_at,
        "ended_at": final_evidence.ended_at,
        "duration_seconds": final_evidence.duration_seconds,
        "outcome": final_evidence.outcome,
        "exit_code": final_evidence.exit_code,
        "stdout_total_bytes": final_evidence.stdout_total_bytes,
        "stderr_total_bytes": final_evidence.stderr_total_bytes,
        "retained_stdout": final_evidence.retained_stdout,
        "retained_stderr": final_evidence.retained_stderr,
        "stdout_sha256": final_evidence.stdout_sha256,
        "stderr_sha256": final_evidence.stderr_sha256,
        "stdout_truncated": final_evidence.stdout_truncated,
        "stderr_truncated": final_evidence.stderr_truncated,
    }
    for request_update in (
        {"run_id": "OTHER-RUN"},
        {"task_id": "TASK-999"},
        {"staged_snapshot_id": "STALE-STAGED-SNAPSHOT"},
    ):
        foreign_evidence = build_validation_execution_evidence(
            final_request.model_copy(update=request_update),
            python_compile_validation_policy(),
            **evidence_observations,
        )
        mismatched = deepcopy(complete)
        mismatched["task_validation_execution_evidence"] = [foreign_evidence]
        assert exit_gate(mismatched)["workflow_status"] == "exit_gate_failed"


def test_validation_progress_events_remain_presentation_only() -> None:
    reporter = RecordingProgressReporter()

    result = _run_approved(
        _proposal(),
        MaterializingExecutor({"TASK-001": "src/candidate.py"}),
        validation_executor=ScriptedValidationExecutor(),
        progress_reporter=reporter,
    )

    assert result["workflow_status"] == "success"
    validation_events = [
        event
        for event in reporter.events
        if isinstance(
            event,
            (TaskValidationExecutionStarted, TaskValidationExecutionCompleted),
        )
    ]
    assert [type(event) for event in validation_events] == [
        TaskValidationExecutionStarted,
        TaskValidationExecutionCompleted,
    ]
    assert validation_events[0].validation_requirement_id == (
        "TASK-001-VALIDATION-001"
    )
    assert validation_events[1].outcome is ValidationExecutionOutcome.PASSED


def test_validation_evidence_serializes_in_existing_execution_evidence(
    tmp_path: Path,
) -> None:
    result = _run(ScriptedValidationExecutor())

    write_artifacts(result, tmp_path)
    serialized = json.loads((tmp_path / "task_execution.json").read_text())
    restored = TaskValidationExecutionEvidence.model_validate_json(
        json.dumps(serialized["validation_executions"][0])
    )
    summary = (tmp_path / "summary.md").read_text()
    graph_review = (tmp_path / "task_graph.md").read_text()

    assert restored == result["task_validation_execution_evidence"][0]
    assert "Governed required validations: 1 passed / 1 required" in summary
    assert "PYTHON_COMPILE validation executed: yes" in summary
    assert "Generated tests executed: no" in summary
    assert "Generated application executed: no" in summary
    assert "Benchmarks executed: no" in summary
    assert "Required validations: PYTHON_COMPILE" in graph_review
