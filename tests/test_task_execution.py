"""Tests for the isolated deterministic TaskGraph execution runtime."""

from __future__ import annotations

from pytest import raises

from agentic_sdlc.task_execution import (
    TaskExecutionError,
    TaskExecutionState,
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
    initialize_task_graph_execution,
    mark_task_failed,
    mark_task_succeeded,
    ready_task_ids,
    safe_stop_task_graph_execution,
    start_task,
)
from agentic_sdlc.task_graph import Task, TaskGraph, TaskType


def _task(task_id: str, *depends_on: str) -> Task:
    return Task(
        task_id=task_id,
        lineage_id=f"lineage-{task_id}",
        source_key=task_id.casefold().replace("-", "_"),
        title=f"Task {task_id}",
        description=f"Deterministic planning data for {task_id}.",
        task_type=TaskType.IMPLEMENTATION,
        depends_on=tuple(depends_on),
        requirement_refs=("FR-001",),
        acceptance_criteria_refs=("AC-001",),
        risk_refs=(),
        ambiguity_refs=(),
        expected_outputs=(f"{task_id}.md",),
    )


def _graph(*tasks: Task, graph_id: str = "GRAPH-TEST-V001") -> TaskGraph:
    return TaskGraph(
        graph_id=graph_id,
        lineage_id="graph-lineage",
        version=1,
        requirement_spec_id="SPEC-TEST-V001",
        requirement_spec_version=1,
        supersedes_graph_id=None,
        created_at="2026-08-09T12:00:00+00:00",
        content_hash="0" * 64,
        tasks=tuple(tasks),
    )


def _status(
    execution: TaskGraphExecutionState, task_id: str
) -> TaskExecutionStatus:
    return next(
        state.status for state in execution.task_states if state.task_id == task_id
    )


def _attempts(execution: TaskGraphExecutionState, task_id: str) -> int:
    return next(
        state.attempt_count
        for state in execution.task_states
        if state.task_id == task_id
    )


def _fan_out_with_running_peers() -> tuple[TaskGraph, TaskGraphExecutionState]:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002", "TASK-001"),
        _task("TASK-003", "TASK-001"),
    )
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = start_task(graph, execution, "TASK-003")
    return graph, execution


def test_initialization_assigns_ready_blocked_and_zero_attempts() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))

    execution = initialize_task_graph_execution(graph)

    assert execution.graph_id == graph.graph_id
    assert execution.status is TaskGraphExecutionStatus.PENDING
    assert _status(execution, "TASK-001") is TaskExecutionStatus.READY
    assert _status(execution, "TASK-002") is TaskExecutionStatus.BLOCKED
    assert [state.attempt_count for state in execution.task_states] == [0, 0]


def test_ready_task_ids_preserve_canonical_graph_order() -> None:
    graph = _graph(_task("TASK-010"), _task("TASK-002"), _task("TASK-007"))
    execution = initialize_task_graph_execution(graph)

    assert ready_task_ids(graph, execution) == (
        "TASK-010",
        "TASK-002",
        "TASK-007",
    )


def test_start_task_transitions_ready_to_running_and_increments_attempt() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    initial = initialize_task_graph_execution(graph)

    running = start_task(graph, initial, "TASK-001")

    assert running.status is TaskGraphExecutionStatus.RUNNING
    assert _status(running, "TASK-001") is TaskExecutionStatus.RUNNING
    assert _attempts(running, "TASK-001") == 1
    assert _status(running, "TASK-002") is TaskExecutionStatus.READY
    assert _attempts(running, "TASK-002") == 0
    assert _status(initial, "TASK-001") is TaskExecutionStatus.READY
    assert _attempts(initial, "TASK-001") == 0


def test_linear_chain_unlocks_and_completes_graph() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002", "TASK-001"),
        _task("TASK-003", "TASK-002"),
    )
    execution = initialize_task_graph_execution(graph)

    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    assert ready_task_ids(graph, execution) == ("TASK-002",)
    assert _status(execution, "TASK-003") is TaskExecutionStatus.BLOCKED

    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_succeeded(graph, execution, "TASK-002")
    assert ready_task_ids(graph, execution) == ("TASK-003",)

    execution = start_task(graph, execution, "TASK-003")
    execution = mark_task_succeeded(graph, execution, "TASK-003")
    assert execution.status is TaskGraphExecutionStatus.SUCCEEDED
    assert ready_task_ids(graph, execution) == ()
    assert all(
        state.status is TaskExecutionStatus.SUCCEEDED
        for state in execution.task_states
    )


def test_fan_out_unlocks_multiple_ready_tasks() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002", "TASK-001"),
        _task("TASK-003", "TASK-001"),
    )
    execution = initialize_task_graph_execution(graph)

    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")

    assert ready_task_ids(graph, execution) == ("TASK-002", "TASK-003")
    assert _status(execution, "TASK-002") is TaskExecutionStatus.READY
    assert _status(execution, "TASK-003") is TaskExecutionStatus.READY


def test_fan_in_waits_for_every_dependency() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002"),
        _task("TASK-003", "TASK-001", "TASK-002"),
    )
    execution = initialize_task_graph_execution(graph)

    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    assert _status(execution, "TASK-003") is TaskExecutionStatus.BLOCKED
    assert ready_task_ids(graph, execution) == ("TASK-002",)

    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_succeeded(graph, execution, "TASK-002")
    assert _status(execution, "TASK-003") is TaskExecutionStatus.READY
    assert ready_task_ids(graph, execution) == ("TASK-003",)


def test_failure_marks_graph_failed_and_does_not_unlock_dependents() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")

    failed = mark_task_failed(graph, execution, "TASK-001")

    assert failed.status is TaskGraphExecutionStatus.FAILED
    assert _status(failed, "TASK-001") is TaskExecutionStatus.FAILED
    assert _status(failed, "TASK-002") is TaskExecutionStatus.BLOCKED
    assert ready_task_ids(graph, failed) == ()


def test_failed_execution_can_transition_to_safe_stopped() -> None:
    graph = _graph(_task("TASK-001"))
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    failed = mark_task_failed(graph, execution, "TASK-001")

    safe_stopped = safe_stop_task_graph_execution(graph, failed)

    assert safe_stopped.status is TaskGraphExecutionStatus.SAFE_STOPPED
    assert safe_stopped.task_states == failed.task_states
    assert ready_task_ids(graph, safe_stopped) == ()


def test_cannot_start_blocked_running_or_succeeded_task() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))
    initial = initialize_task_graph_execution(graph)
    with raises(TaskExecutionError, match="BLOCKED"):
        start_task(graph, initial, "TASK-002")

    running = start_task(graph, initial, "TASK-001")
    with raises(TaskExecutionError, match="RUNNING"):
        start_task(graph, running, "TASK-001")

    succeeded = mark_task_succeeded(graph, running, "TASK-001")
    with raises(TaskExecutionError, match="SUCCEEDED"):
        start_task(graph, succeeded, "TASK-001")


def test_non_running_task_cannot_be_marked_succeeded_or_failed() -> None:
    graph = _graph(_task("TASK-001"))
    execution = initialize_task_graph_execution(graph)

    with raises(TaskExecutionError, match="cannot succeed from READY"):
        mark_task_succeeded(graph, execution, "TASK-001")
    with raises(TaskExecutionError, match="cannot fail from READY"):
        mark_task_failed(graph, execution, "TASK-001")


def test_unknown_task_ids_are_rejected() -> None:
    graph = _graph(_task("TASK-001"))
    execution = initialize_task_graph_execution(graph)

    with raises(TaskExecutionError, match="Unknown task ID"):
        start_task(graph, execution, "TASK-999")
    with raises(TaskExecutionError, match="Unknown task ID"):
        mark_task_succeeded(graph, execution, "TASK-999")
    with raises(TaskExecutionError, match="Unknown task ID"):
        mark_task_failed(graph, execution, "TASK-999")


def test_failed_graph_rejects_dispatch_and_non_running_task_settlement() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    failed = mark_task_failed(graph, execution, "TASK-001")

    with raises(TaskExecutionError, match="does not permit new task dispatch: FAILED"):
        start_task(graph, failed, "TASK-002")
    with raises(TaskExecutionError, match="cannot succeed from FAILED"):
        mark_task_succeeded(graph, failed, "TASK-001")


def test_running_peer_may_succeed_after_failure_and_failure_stays_sticky() -> None:
    graph, execution = _fan_out_with_running_peers()

    execution = mark_task_failed(graph, execution, "TASK-002")
    assert execution.status is TaskGraphExecutionStatus.FAILED
    assert _status(execution, "TASK-002") is TaskExecutionStatus.FAILED
    assert _status(execution, "TASK-003") is TaskExecutionStatus.RUNNING

    settled = mark_task_succeeded(graph, execution, "TASK-003")

    assert settled.status is TaskGraphExecutionStatus.FAILED
    assert _status(settled, "TASK-001") is TaskExecutionStatus.SUCCEEDED
    assert _status(settled, "TASK-002") is TaskExecutionStatus.FAILED
    assert _status(settled, "TASK-003") is TaskExecutionStatus.SUCCEEDED


def test_running_peer_may_fail_after_graph_failure() -> None:
    graph, execution = _fan_out_with_running_peers()
    execution = mark_task_failed(graph, execution, "TASK-002")

    settled = mark_task_failed(graph, execution, "TASK-003")

    assert settled.status is TaskGraphExecutionStatus.FAILED
    assert _status(settled, "TASK-002") is TaskExecutionStatus.FAILED
    assert _status(settled, "TASK-003") is TaskExecutionStatus.FAILED


def test_failure_freezes_dispatch_of_other_ready_work() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002"),
        _task("TASK-003"),
    )
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_failed(graph, execution, "TASK-001")

    with raises(TaskExecutionError, match="does not permit new task dispatch: FAILED"):
        start_task(graph, execution, "TASK-002")

    assert _status(execution, "TASK-002") is TaskExecutionStatus.READY
    assert _status(execution, "TASK-003") is TaskExecutionStatus.READY
    assert ready_task_ids(graph, execution) == ()


def test_successful_settlement_after_failure_does_not_unlock_downstream() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002"),
        _task("TASK-003", "TASK-002"),
    )
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_failed(graph, execution, "TASK-001")

    settled = mark_task_succeeded(graph, execution, "TASK-002")

    assert settled.status is TaskGraphExecutionStatus.FAILED
    assert _status(settled, "TASK-002") is TaskExecutionStatus.SUCCEEDED
    assert _status(settled, "TASK-003") is TaskExecutionStatus.BLOCKED
    assert ready_task_ids(graph, settled) == ()


def test_safe_stop_is_rejected_while_a_peer_is_still_running() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_failed(graph, execution, "TASK-001")

    with raises(
        TaskExecutionError,
        match=(
            "Cannot safe-stop TaskGraph execution while tasks are still running: "
            "TASK-002"
        ),
    ):
        safe_stop_task_graph_execution(graph, execution)


def test_safe_stop_succeeds_after_running_peers_settle() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_failed(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-002")

    safe_stopped = safe_stop_task_graph_execution(graph, execution)

    assert safe_stopped.status is TaskGraphExecutionStatus.SAFE_STOPPED
    assert _status(safe_stopped, "TASK-001") is TaskExecutionStatus.FAILED
    assert _status(safe_stopped, "TASK-002") is TaskExecutionStatus.SUCCEEDED
    assert all(
        state.status is not TaskExecutionStatus.RUNNING
        for state in safe_stopped.task_states
    )


def test_safe_stop_requires_failed_graph_execution() -> None:
    graph = _graph(_task("TASK-001"))
    execution = initialize_task_graph_execution(graph)

    with raises(TaskExecutionError, match="Only a FAILED"):
        safe_stop_task_graph_execution(graph, execution)


def test_execution_cannot_be_used_with_another_graph() -> None:
    first = _graph(_task("TASK-001"), graph_id="GRAPH-ONE-V001")
    second = _graph(_task("TASK-001"), graph_id="GRAPH-TWO-V001")
    execution = initialize_task_graph_execution(first)

    with raises(TaskExecutionError, match="does not match"):
        ready_task_ids(second, execution)
    with raises(TaskExecutionError, match="does not match"):
        start_task(second, execution, "TASK-001")


def test_execution_task_order_must_match_canonical_graph() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = TaskGraphExecutionState(
        graph_id=graph.graph_id,
        status=TaskGraphExecutionStatus.PENDING,
        task_states=(
            TaskExecutionState(
                task_id="TASK-002", status=TaskExecutionStatus.READY
            ),
            TaskExecutionState(
                task_id="TASK-001", status=TaskExecutionStatus.READY
            ),
        ),
    )

    with raises(TaskExecutionError, match="identities/order"):
        ready_task_ids(graph, execution)


def test_runtime_transitions_do_not_mutate_approved_task_graph() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002", "TASK-001"),
    )
    planning_snapshot = graph.model_dump(mode="json")
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = mark_task_succeeded(graph, execution, "TASK-002")

    assert execution.status is TaskGraphExecutionStatus.SUCCEEDED
    assert graph.model_dump(mode="json") == planning_snapshot
    assert graph.tasks[1].depends_on == ("TASK-001",)
    assert graph.tasks[1].requirement_refs == ("FR-001",)
    assert graph.tasks[1].description == (
        "Deterministic planning data for TASK-002."
    )
