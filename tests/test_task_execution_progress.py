"""Focused tests for ephemeral Task Agent progress rendering."""

from io import StringIO

from agentic_sdlc.task_execution_progress import (
    ConsoleTaskExecutionProgressReporter,
    GovernedTaskExecutionStarted,
    TaskExecutionAttemptSettled,
    TaskExecutionHeartbeat,
    TaskExecutionProgressAttempt,
    TaskExecutionSettledOutcome,
    TaskExecutionWaveMode,
    TaskExecutionWaveStarted,
    TaskExecutorCompleted,
    TaskValidationExecutionCompleted,
    TaskValidationExecutionStarted,
)
from agentic_sdlc.task_graph import ValidationExecutionProfile
from agentic_sdlc.validation_execution_contracts import ValidationExecutionOutcome


def _attempt(
    task_id: str,
    attempt_number: int,
    title: str,
) -> TaskExecutionProgressAttempt:
    return TaskExecutionProgressAttempt(
        task_id=task_id,
        attempt_number=attempt_number,
        title=title,
    )


def test_console_reporter_renders_singleton_execution_lifecycle() -> None:
    stream = StringIO()
    reporter = ConsoleTaskExecutionProgressReporter(stream=stream)
    attempt = _attempt("TASK-002", 2, "Implement runnable service")

    reporter.report(GovernedTaskExecutionStarted())
    reporter.report(
        TaskExecutionWaveStarted(
            wave_number=3,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(attempt,),
        )
    )
    reporter.report(
        TaskExecutionHeartbeat(
            wave_number=3,
            outstanding_attempts=(attempt,),
        )
    )
    reporter.report(TaskExecutorCompleted(wave_number=3, attempt=attempt))
    reporter.report(
        TaskValidationExecutionStarted(
            wave_number=3,
            attempt=attempt,
            validation_requirement_id="TASK-002-VALIDATION-001",
            profile=ValidationExecutionProfile.PYTHON_COMPILE,
        )
    )
    reporter.report(
        TaskValidationExecutionCompleted(
            wave_number=3,
            attempt=attempt,
            validation_requirement_id="TASK-002-VALIDATION-001",
            profile=ValidationExecutionProfile.PYTHON_COMPILE,
            outcome=ValidationExecutionOutcome.PASSED,
        )
    )
    reporter.report(
        TaskExecutionAttemptSettled(
            wave_number=3,
            attempt=attempt,
            outcome=TaskExecutionSettledOutcome.RETRY_SCHEDULED,
            detail="scheduled retry after validation",
        )
    )

    output = stream.getvalue()
    assert "TaskGraph approved. Beginning governed Task Agent execution..." in output
    assert "[wave 3] Starting 1 Task Agent attempt:" in output
    assert "TASK-002 attempt 2 — Implement runnable service" in output
    assert "[wave 3] TASK-002 attempt 2 still running..." in output
    assert (
        "[wave 3] TASK-002 attempt 2 executor completed; validating result..."
        in output
    )
    assert (
        "[wave 3] TASK-002 attempt 2 executing required PYTHON_COMPILE "
        "validation..." in output
    )
    assert "[wave 3] TASK-002 attempt 2 PYTHON_COMPILE passed." in output
    assert (
        "[wave 3] TASK-002 attempt 2 scheduled retry after validation" in output
    )


def test_console_reporter_makes_parallel_outstanding_attempts_visible() -> None:
    stream = StringIO()
    reporter = ConsoleTaskExecutionProgressReporter(stream=stream)
    attempts = (
        _attempt("TASK-003", 1, "Materialize tests"),
        _attempt("TASK-005", 1, "Document operation"),
    )

    reporter.report(
        TaskExecutionWaveStarted(
            wave_number=4,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=attempts,
        )
    )
    reporter.report(
        TaskExecutionHeartbeat(
            wave_number=4,
            outstanding_attempts=attempts,
        )
    )

    output = stream.getvalue()
    assert "[wave 4] Starting 2 Task Agent attempts in parallel:" in output
    assert "TASK-003 attempt 1 — Materialize tests" in output
    assert "TASK-005 attempt 1 — Document operation" in output
    assert (
        "[wave 4] 2 Task Agent attempts still running: "
        "TASK-003 attempt 1, TASK-005 attempt 1" in output
    )
