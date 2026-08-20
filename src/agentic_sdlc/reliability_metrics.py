"""Deterministic reliability projections over immutable governed-run evidence."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


RATIO_QUANTUM = Decimal("0.0001")
END_TO_END_LATENCY_REASON = (
    "The current audit evidence does not retain authoritative elapsed-time "
    "boundaries suitable for defensible end-to-end latency measurement."
)
MTTR_REASON = (
    "Recovery events are tracked structurally, but authoritative "
    "incident-to-recovery duration boundaries are not retained."
)


class ReliabilityMetricsError(ValueError):
    """Raised when authoritative evidence cannot support one coherent projection."""


class ReliabilityRatio(BaseModel):
    """Exact counts plus a deterministic four-decimal display representation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: str | None

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        if self.numerator > self.denominator:
            raise ValueError(
                "A reliability ratio numerator cannot exceed its denominator."
            )
        expected = _render_ratio(self.numerator, self.denominator)
        if self.value != expected:
            raise ValueError(
                "Reliability ratio value does not match its numerator and denominator."
            )
        return self


class TimingMeasurementStatus(StrEnum):
    """Whether authoritative evidence supports a timing measurement."""

    NOT_MEASURED = "NOT_MEASURED"


class TimingMeasurement(BaseModel):
    """Human-readable explanation for one deliberately unsupported duration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: TimingMeasurementStatus
    reason: str = Field(min_length=1)


class RunReliabilityMetrics(BaseModel):
    """Immutable reliability measures for one governed TaskGraph execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    terminal_run_status: TaskGraphExecutionStatus

    task_count: int = Field(ge=0)
    attempted_task_count: int = Field(ge=0)
    successful_task_count: int = Field(ge=0)
    failed_task_count: int = Field(ge=0)
    aborted_task_count: int = Field(ge=0)
    blocked_task_count: int = Field(ge=0)

    task_attempt_count: int = Field(ge=0)
    successful_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    failed_attempt_count: int = Field(ge=0)
    safe_stopped_attempt_count: int = Field(ge=0)

    task_success_rate: ReliabilityRatio
    attempt_success_rate: ReliabilityRatio
    retry_frequency: ReliabilityRatio

    mutation_transaction_count: int = Field(ge=0)
    applied_mutation_count: int = Field(ge=0)
    rollback_attempt_count: int = Field(ge=0)
    rollback_success_count: int = Field(ge=0)
    rollback_failure_count: int = Field(ge=0)
    rollback_frequency: ReliabilityRatio

    safe_stop_count: int = Field(ge=0, le=1)
    end_to_end_latency: TimingMeasurement
    mttr: TimingMeasurement


class ScenarioReliabilityMetrics(BaseModel):
    """One independently reported demonstration scenario and its evidence root."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scenario: str = Field(min_length=1)
    evidence_root: str = Field(min_length=1)
    metrics: RunReliabilityMetrics


class ReliabilityMetricsArtifact(BaseModel):
    """Cross-scenario index of independent per-run reliability records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(min_length=1)
    runs: tuple[ScenarioReliabilityMetrics, ...]


def derive_reliability_metrics(
    *,
    task_graph_execution: TaskGraphExecutionState,
    task_attempt_exit_decisions: Sequence[TaskAttemptExitDecision],
    workspace_mutation_results: Sequence[WorkspaceMutationResult],
) -> RunReliabilityMetrics:
    """Project one terminal governed run without clocks, I/O, or side effects."""

    if task_graph_execution.status not in {
        TaskGraphExecutionStatus.SUCCEEDED,
        TaskGraphExecutionStatus.SAFE_STOPPED,
    }:
        raise ReliabilityMetricsError(
            "Reliability metrics require a terminal SUCCEEDED or SAFE_STOPPED run."
        )

    task_states = task_graph_execution.task_states
    _validate_attempt_accounting(task_graph_execution, task_attempt_exit_decisions)

    task_count = len(task_states)
    attempted_task_count = sum(state.attempt_count > 0 for state in task_states)
    successful_task_count = _count_task_status(
        task_states, TaskExecutionStatus.SUCCEEDED
    )
    failed_task_count = _count_task_status(task_states, TaskExecutionStatus.FAILED)
    aborted_task_count = _count_task_status(task_states, TaskExecutionStatus.ABORTED)
    blocked_task_count = _count_task_status(task_states, TaskExecutionStatus.BLOCKED)

    task_attempt_count = sum(state.attempt_count for state in task_states)
    successful_attempt_count = _count_disposition(
        task_attempt_exit_decisions, TaskAttemptExitDisposition.SUCCEED_TASK
    )
    retry_count = _count_disposition(
        task_attempt_exit_decisions, TaskAttemptExitDisposition.RETRY_TASK
    )
    failed_attempt_count = _count_disposition(
        task_attempt_exit_decisions, TaskAttemptExitDisposition.FAIL_TASK
    )
    safe_stopped_attempt_count = _count_disposition(
        task_attempt_exit_decisions, TaskAttemptExitDisposition.SAFE_STOP_RUN
    )

    mutation_transaction_count = len(workspace_mutation_results)
    applied_mutation_count = _count_mutation_status(
        workspace_mutation_results, WorkspaceMutationStatus.APPLIED
    )
    rollback_success_count = _count_mutation_status(
        workspace_mutation_results, WorkspaceMutationStatus.ROLLED_BACK
    )
    rollback_failure_count = _count_mutation_status(
        workspace_mutation_results, WorkspaceMutationStatus.ROLLBACK_FAILED
    )
    rollback_attempt_count = rollback_success_count + rollback_failure_count

    return RunReliabilityMetrics(
        terminal_run_status=task_graph_execution.status,
        task_count=task_count,
        attempted_task_count=attempted_task_count,
        successful_task_count=successful_task_count,
        failed_task_count=failed_task_count,
        aborted_task_count=aborted_task_count,
        blocked_task_count=blocked_task_count,
        task_attempt_count=task_attempt_count,
        successful_attempt_count=successful_attempt_count,
        retry_count=retry_count,
        failed_attempt_count=failed_attempt_count,
        safe_stopped_attempt_count=safe_stopped_attempt_count,
        task_success_rate=_ratio(successful_task_count, attempted_task_count),
        attempt_success_rate=_ratio(
            successful_attempt_count, task_attempt_count
        ),
        retry_frequency=_ratio(retry_count, task_attempt_count),
        mutation_transaction_count=mutation_transaction_count,
        applied_mutation_count=applied_mutation_count,
        rollback_attempt_count=rollback_attempt_count,
        rollback_success_count=rollback_success_count,
        rollback_failure_count=rollback_failure_count,
        rollback_frequency=_ratio(
            rollback_attempt_count, mutation_transaction_count
        ),
        safe_stop_count=int(
            task_graph_execution.status is TaskGraphExecutionStatus.SAFE_STOPPED
        ),
        end_to_end_latency=TimingMeasurement(
            status=TimingMeasurementStatus.NOT_MEASURED,
            reason=END_TO_END_LATENCY_REASON,
        ),
        mttr=TimingMeasurement(
            status=TimingMeasurementStatus.NOT_MEASURED,
            reason=MTTR_REASON,
        ),
    )


def _validate_attempt_accounting(
    task_graph_execution: TaskGraphExecutionState,
    decisions: Sequence[TaskAttemptExitDecision],
) -> None:
    task_ids = tuple(state.task_id for state in task_graph_execution.task_states)
    if len(task_ids) != len(set(task_ids)):
        raise ReliabilityMetricsError(
            "TaskGraph execution contains duplicate canonical task IDs."
        )
    if any(
        state.status is TaskExecutionStatus.RUNNING
        for state in task_graph_execution.task_states
    ):
        raise ReliabilityMetricsError(
            "Terminal reliability evidence cannot contain a RUNNING task."
        )

    expected_attempts = {
        (state.task_id, attempt_number)
        for state in task_graph_execution.task_states
        for attempt_number in range(1, state.attempt_count + 1)
    }
    observed_attempts = tuple(
        (decision.task_id, decision.attempt_number) for decision in decisions
    )
    if len(observed_attempts) != len(set(observed_attempts)):
        raise ReliabilityMetricsError(
            "Task-attempt exit evidence contains duplicate attempt decisions."
        )
    if set(observed_attempts) != expected_attempts:
        raise ReliabilityMetricsError(
            "Task-attempt exit decisions must account for every started attempt "
            "exactly once."
        )


def _count_task_status(
    task_states: Sequence[TaskExecutionState], status: TaskExecutionStatus
) -> int:
    return sum(state.status is status for state in task_states)


def _count_disposition(
    decisions: Sequence[TaskAttemptExitDecision],
    disposition: TaskAttemptExitDisposition,
) -> int:
    return sum(decision.disposition is disposition for decision in decisions)


def _count_mutation_status(
    results: Sequence[WorkspaceMutationResult], status: WorkspaceMutationStatus
) -> int:
    return sum(result.status is status for result in results)


def _ratio(numerator: int, denominator: int) -> ReliabilityRatio:
    return ReliabilityRatio(
        numerator=numerator,
        denominator=denominator,
        value=_render_ratio(numerator, denominator),
    )


def _render_ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        RATIO_QUANTUM, rounding=ROUND_HALF_UP
    )
    return f"{value:.4f}"
