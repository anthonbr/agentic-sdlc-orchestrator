"""Focused proof for deterministic reliability projections and reviewer output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sdlc.artifacts import write_reliability_metrics_artifact
from agentic_sdlc.reliability_metrics import (
    ReliabilityMetricsError,
    derive_reliability_metrics,
)
from agentic_sdlc.task_execution import (
    TaskExecutionState,
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
)
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDecision,
    TaskAttemptExitDisposition,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationResult,
    WorkspaceMutationStatus,
)


REPOSITORY_ROOT = Path(__file__).parents[1]


def _task_state(
    task_id: str, status: TaskExecutionStatus, attempt_count: int
) -> TaskExecutionState:
    return TaskExecutionState(
        task_id=task_id,
        status=status,
        attempt_count=attempt_count,
    )


def _execution(
    status: TaskGraphExecutionStatus,
    *task_states: TaskExecutionState,
) -> TaskGraphExecutionState:
    return TaskGraphExecutionState(
        graph_id="GRAPH-RELIABILITY-TEST",
        status=status,
        task_states=task_states,
    )


def _decision(
    task_id: str,
    attempt_number: int,
    disposition: TaskAttemptExitDisposition,
) -> TaskAttemptExitDecision:
    return TaskAttemptExitDecision(
        task_id=task_id,
        attempt_number=attempt_number,
        request_id=None,
        attempt_id=None,
        disposition=disposition,
        reason_code="TEST_EVIDENCE",
    )


def _mutation(
    index: int, status: WorkspaceMutationStatus
) -> WorkspaceMutationResult:
    suffix = f"{index:03d}"
    return WorkspaceMutationResult(
        mutation_id=f"MUTATION-{suffix}",
        workspace_id="WORKSPACE-RELIABILITY-TEST",
        change_set_id=f"CHANGE-SET-{suffix}",
        base_snapshot_id="SNAPSHOT-BASE",
        task_id=f"TASK-{suffix}",
        request_id=f"REQUEST-{suffix}",
        attempt_id=f"ATTEMPT-{suffix}",
        pre_mutation_snapshot_id="SNAPSHOT-BEFORE",
        post_mutation_snapshot_id=None,
        rollback_snapshot_id=(
            "SNAPSHOT-ROLLBACK"
            if status is WorkspaceMutationStatus.ROLLED_BACK
            else None
        ),
        status=status,
        file_evidence=(),
        issues=(),
    )


def test_successful_run_with_retry_uses_final_tasks_and_attempt_exits() -> None:
    execution = _execution(
        TaskGraphExecutionStatus.SUCCEEDED,
        _task_state("TASK-001", TaskExecutionStatus.SUCCEEDED, 2),
        _task_state("TASK-002", TaskExecutionStatus.SUCCEEDED, 1),
    )
    decisions = (
        _decision("TASK-001", 1, TaskAttemptExitDisposition.RETRY_TASK),
        _decision("TASK-001", 2, TaskAttemptExitDisposition.SUCCEED_TASK),
        _decision("TASK-002", 1, TaskAttemptExitDisposition.SUCCEED_TASK),
    )

    metrics = derive_reliability_metrics(
        task_graph_execution=execution,
        task_attempt_exit_decisions=decisions,
        workspace_mutation_results=(),
    )

    assert metrics.task_count == 2
    assert metrics.attempted_task_count == 2
    assert metrics.successful_task_count == 2
    assert metrics.task_attempt_count == 3
    assert metrics.successful_attempt_count == 2
    assert metrics.retry_count == 1
    assert metrics.task_success_rate.model_dump() == {
        "numerator": 2,
        "denominator": 2,
        "value": "1.0000",
    }
    assert metrics.attempt_success_rate.model_dump() == {
        "numerator": 2,
        "denominator": 3,
        "value": "0.6667",
    }
    assert metrics.retry_frequency.model_dump() == {
        "numerator": 1,
        "denominator": 3,
        "value": "0.3333",
    }

    with pytest.raises(ReliabilityMetricsError, match="every started attempt"):
        derive_reliability_metrics(
            task_graph_execution=execution,
            task_attempt_exit_decisions=decisions[:-1],
            workspace_mutation_results=(),
        )


def test_rollback_accounting_uses_structured_mutation_statuses() -> None:
    metrics = derive_reliability_metrics(
        task_graph_execution=_execution(TaskGraphExecutionStatus.SAFE_STOPPED),
        task_attempt_exit_decisions=(),
        workspace_mutation_results=(
            _mutation(1, WorkspaceMutationStatus.APPLIED),
            _mutation(2, WorkspaceMutationStatus.ROLLED_BACK),
            _mutation(3, WorkspaceMutationStatus.ROLLBACK_FAILED),
        ),
    )

    assert metrics.mutation_transaction_count == 3
    assert metrics.applied_mutation_count == 1
    assert metrics.rollback_attempt_count == 2
    assert metrics.rollback_success_count == 1
    assert metrics.rollback_failure_count == 1
    assert metrics.rollback_frequency.model_dump() == {
        "numerator": 2,
        "denominator": 3,
        "value": "0.6667",
    }


def test_safe_stop_is_counted_once_without_multiplying_task_outcomes() -> None:
    metrics = derive_reliability_metrics(
        task_graph_execution=_execution(
            TaskGraphExecutionStatus.SAFE_STOPPED,
            _task_state("TASK-001", TaskExecutionStatus.FAILED, 1),
            _task_state("TASK-002", TaskExecutionStatus.ABORTED, 1),
            _task_state("TASK-003", TaskExecutionStatus.BLOCKED, 0),
            _task_state("TASK-004", TaskExecutionStatus.SUCCEEDED, 1),
        ),
        task_attempt_exit_decisions=(
            _decision("TASK-001", 1, TaskAttemptExitDisposition.FAIL_TASK),
            _decision("TASK-002", 1, TaskAttemptExitDisposition.SAFE_STOP_RUN),
            _decision("TASK-004", 1, TaskAttemptExitDisposition.SUCCEED_TASK),
        ),
        workspace_mutation_results=(),
    )

    assert metrics.safe_stop_count == 1
    assert metrics.safe_stopped_attempt_count == 1
    assert metrics.task_attempt_count == 3
    assert metrics.successful_attempt_count == 1
    assert metrics.failed_attempt_count == 1
    assert metrics.failed_task_count == 1
    assert metrics.aborted_task_count == 1
    assert metrics.blocked_task_count == 1


def test_zero_denominators_are_undefined_and_derivation_is_deterministic() -> None:
    arguments = {
        "task_graph_execution": _execution(
            TaskGraphExecutionStatus.SAFE_STOPPED,
            _task_state("TASK-001", TaskExecutionStatus.BLOCKED, 0),
        ),
        "task_attempt_exit_decisions": (),
        "workspace_mutation_results": (),
    }

    first = derive_reliability_metrics(**arguments)
    second = derive_reliability_metrics(**arguments)

    assert first == second
    assert first.task_success_rate.model_dump() == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert first.attempt_success_rate.model_dump() == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert first.retry_frequency.value is None
    assert first.rollback_frequency.value is None


def test_reviewer_artifact_is_byte_stable_and_reports_two_curated_runs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_reliability_metrics_artifact(REPOSITORY_ROOT / "sample_output", first)
    write_reliability_metrics_artifact(REPOSITORY_ROOT / "sample_output", second)

    assert first.read_bytes() == second.read_bytes()
    artifact = json.loads(first.read_text())
    assert artifact["schema_version"] == "reliability-metrics-v1"
    assert [run["scenario"] for run in artifact["runs"]] == [
        "V17 greenfield",
        "V18 brownfield",
    ]
    assert [run["evidence_root"] for run in artifact["runs"]] == [
        "sample_output/url-shortener-v17/sdlc-artifacts/",
        "sample_output/url-shortener-v18-expiration/sdlc-artifacts/",
    ]
    assert len(artifact["runs"]) == 2
    assert "generated_at" not in artifact
    for run in artifact["runs"]:
        assert run["metrics"]["end_to_end_latency"]["status"] == "NOT_MEASURED"
        assert run["metrics"]["mttr"]["status"] == "NOT_MEASURED"
