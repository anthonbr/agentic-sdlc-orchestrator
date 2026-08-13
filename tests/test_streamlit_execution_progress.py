"""Tests for the thread-safe Streamlit execution telemetry projection."""

from __future__ import annotations

from agentic_sdlc.streamlit_execution_progress import (
    StreamlitExecutionProgressCollector,
)
from agentic_sdlc.task_execution_progress import (
    GovernedTaskExecutionStarted,
    TaskExecutionAttemptSettled,
    TaskExecutionAttemptStarted,
    TaskExecutionHeartbeat,
    TaskExecutionProgressAttempt,
    TaskExecutionSettledOutcome,
    TaskExecutionWaveMode,
    TaskExecutionWaveStarted,
    TaskExecutorCompleted,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


def _graph() -> dict[str, object]:
    return {
        "tasks": [
            {"task_id": "TASK-001", "title": "Prepare foundation"},
            {"task_id": "TASK-002", "title": "Build service"},
            {"task_id": "TASK-003", "title": "Build tests"},
        ]
    }


def _semantics() -> dict[str, object]:
    return {
        "execution_layers": [
            ["TASK-001"],
            ["TASK-002", "TASK-003"],
        ]
    }


def _attempt(task_id: str, attempt_number: int = 1) -> TaskExecutionProgressAttempt:
    return TaskExecutionProgressAttempt(
        task_id=task_id,
        attempt_number=attempt_number,
        title=f"event title for {task_id}",
    )


def _collector(
    clock: ManualClock,
    *,
    history_limit: int = 200,
) -> StreamlitExecutionProgressCollector:
    collector = StreamlitExecutionProgressCollector(
        history_limit=history_limit,
        clock=clock,
    )
    collector.attach_run("run-1")
    assert collector.begin_execution(
        run_id="run-1",
        operation_id="operation-1",
        candidate_task_graph=_graph(),
        graph_semantics=_semantics(),
    )
    return collector


def _tasks_by_id(collector: StreamlitExecutionProgressCollector) -> dict[str, object]:
    view = collector.snapshot(run_id="run-1")
    assert view is not None
    return {task.task_id: task for task in view.tasks}


def test_parallel_structured_start_events_show_multiple_running_tasks() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    parallel_attempts = (_attempt("TASK-002"), _attempt("TASK-003"))

    collector.report(GovernedTaskExecutionStarted())
    clock.advance()
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=2,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=parallel_attempts,
        )
    )
    collector.report(
        TaskExecutionAttemptStarted(wave_number=2, attempt=parallel_attempts[0])
    )
    collector.report(
        TaskExecutionAttemptStarted(wave_number=2, attempt=parallel_attempts[1])
    )

    view = collector.snapshot(run_id="run-1")
    assert view is not None
    tasks = {task.task_id: task for task in view.tasks}
    assert tasks["TASK-001"].status == "AWAITING_EVENT"
    assert tasks["TASK-002"].status == "RUNNING"
    assert tasks["TASK-003"].status == "RUNNING"
    assert tasks["TASK-002"].title == "Build service"
    assert tasks["TASK-003"].title == "Build tests"
    assert view.current_wave_number == 2
    assert view.current_wave_mode == "PARALLEL"
    assert view.current_layer_numbers == (2,)
    assert view.completed_task_count == 0
    assert [event.event_type for event in view.recent_events] == [
        "GovernedTaskExecutionStarted",
        "TaskExecutionWaveStarted",
        "TaskExecutionAttemptStarted",
        "TaskExecutionAttemptStarted",
    ]


def test_completion_retry_and_failure_preserve_existing_outcome_semantics() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    service = _attempt("TASK-002")
    tests = _attempt("TASK-003")
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=2,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=(service, tests),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=2, attempt=service))
    collector.report(TaskExecutionAttemptStarted(wave_number=2, attempt=tests))

    clock.advance(3.5)
    collector.report(TaskExecutorCompleted(wave_number=2, attempt=service))
    assert _tasks_by_id(collector)["TASK-002"].status == "VALIDATING"
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=2,
            attempt=service,
            outcome=TaskExecutionSettledOutcome.SUCCEEDED,
            detail="succeeded",
        )
    )
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=2,
            attempt=tests,
            outcome=TaskExecutionSettledOutcome.RETRY_SCHEDULED,
            detail="scheduled retry after validation",
        )
    )

    retry = _attempt("TASK-003", 2)
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=3,
            mode=TaskExecutionWaveMode.SERIALIZED_RETRY,
            attempts=(retry,),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=3, attempt=retry))
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=3,
            attempt=retry,
            outcome=TaskExecutionSettledOutcome.FAILED,
            detail="terminally failed",
        )
    )

    view = collector.snapshot(run_id="run-1")
    assert view is not None
    tasks = {task.task_id: task for task in view.tasks}
    assert tasks["TASK-002"].status == "SUCCEEDED"
    assert tasks["TASK-002"].attempt_elapsed_seconds == 3.5
    assert tasks["TASK-003"].status == "FAILED"
    assert tasks["TASK-003"].attempt_number == 2
    assert tasks["TASK-003"].retry_count == 1
    assert tasks["TASK-003"].latest_detail == "terminally failed"
    assert view.completed_task_count == 1
    assert view.retry_count == 1
    assert view.failed_task_count == 1


def test_heartbeat_and_elapsed_use_injected_clock_without_sleeping() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    attempt = _attempt("TASK-001")
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=1,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(attempt,),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=1, attempt=attempt))
    clock.advance(12.25)
    collector.report(
        TaskExecutionHeartbeat(
            wave_number=1,
            outstanding_attempts=(attempt,),
        )
    )

    view = collector.snapshot(run_id="run-1")
    assert view is not None
    task = next(task for task in view.tasks if task.task_id == "TASK-001")
    assert view.elapsed_seconds == 12.25
    assert task.attempt_elapsed_seconds == 12.25
    assert task.latest_detail == "Task Agent attempt is still running."


def test_final_progress_is_retained_and_repeated_snapshots_add_no_events() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    attempt = _attempt("TASK-001")
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=1,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(attempt,),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=1, attempt=attempt))
    clock.advance(4.0)
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=1,
            attempt=attempt,
            outcome=TaskExecutionSettledOutcome.SUCCEEDED,
            detail="succeeded",
        )
    )
    assert collector.finish_execution(
        run_id="run-1",
        operation_id="operation-1",
    )

    first = collector.snapshot(run_id="run-1")
    clock.advance(30.0)
    second = collector.snapshot(run_id="run-1")

    assert first is not None and second is not None
    assert first.telemetry_status == "OBSERVATION_COMPLETE"
    assert first.completed_task_count == 1
    assert second.elapsed_seconds == first.elapsed_seconds == 4.0
    assert second.recent_events == first.recent_events


def test_history_is_bounded_while_per_task_summary_remains_current() -> None:
    clock = ManualClock()
    collector = _collector(clock, history_limit=3)
    attempt = _attempt("TASK-001")
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=1,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(attempt,),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=1, attempt=attempt))
    for _ in range(4):
        collector.report(
            TaskExecutionHeartbeat(
                wave_number=1,
                outstanding_attempts=(attempt,),
            )
        )

    view = collector.snapshot(run_id="run-1")
    assert view is not None
    assert len(view.recent_events) == 3
    assert view.dropped_event_count == 4
    assert view.recent_events[0].sequence == 5
    assert next(
        task for task in view.tasks if task.task_id == "TASK-001"
    ).status == "RUNNING"


def test_run_isolation_and_new_run_attachment_reset_old_progress() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    collector.report(GovernedTaskExecutionStarted())

    assert collector.snapshot(run_id="another-run") is None
    assert not collector.begin_execution(
        run_id="another-run",
        operation_id="wrong-operation",
        candidate_task_graph=_graph(),
        graph_semantics=_semantics(),
    )
    retained = collector.snapshot(run_id="run-1")
    assert retained is not None
    assert len(retained.recent_events) == 1

    collector.attach_run("run-2")

    assert collector.snapshot(run_id="run-1") is None
    assert collector.snapshot(run_id="run-2") is None


def test_later_operation_resets_prior_operation_telemetry() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    collector.report(GovernedTaskExecutionStarted())
    assert collector.finish_execution(
        run_id="run-1",
        operation_id="operation-1",
    )

    assert collector.begin_execution(
        run_id="run-1",
        operation_id="operation-2",
        candidate_task_graph=_graph(),
        graph_semantics=_semantics(),
    )
    view = collector.snapshot(run_id="run-1")

    assert view is not None
    assert view.operation_id == "operation-2"
    assert view.telemetry_status == "AWAITING_EVENT"
    assert view.recent_events == ()
    assert all(task.status == "AWAITING_EVENT" for task in view.tasks)


def test_unknown_task_event_is_visible_without_inventing_canonical_metadata() -> None:
    clock = ManualClock()
    collector = _collector(clock)
    unknown = _attempt("TASK-999")
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=1,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(unknown,),
        )
    )
    collector.report(TaskExecutionAttemptStarted(wave_number=1, attempt=unknown))

    view = collector.snapshot(run_id="run-1")
    assert view is not None
    task = next(task for task in view.tasks if task.task_id == "TASK-999")
    assert task.unknown_task
    assert task.title is None
    assert task.layer_number is None
    assert task.status == "RUNNING"
    assert view.total_task_count == 3
