"""Tests for the isolated deterministic TaskGraph execution runtime."""

from __future__ import annotations

from pytest import raises

from agentic_sdlc.requirement_spec import ApprovedRequirementSpec
from agentic_sdlc.task_execution import (
    MAX_PARALLEL_TASK_EXECUTIONS,
    MAX_TASK_EXECUTION_ATTEMPTS,
    TaskExecutionError,
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionState,
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
    abort_running_task,
    decide_task_execution_recovery,
    fail_task_graph_integrity,
    initialize_task_graph_execution,
    mark_task_failed,
    mark_task_succeeded,
    prepare_task_retry,
    ready_task_ids,
    ready_task_wave_ids,
    safe_stop_task_graph_execution,
    start_task,
    start_serialized_task_wave,
    start_task_wave,
)
from agentic_sdlc.task_graph import (
    Task,
    TaskGraph,
    TaskMaterializationPolicy,
    TaskType,
)


def _task(task_id: str, *depends_on: str) -> Task:
    return Task(
        task_id=task_id,
        lineage_id=f"lineage-{task_id}",
        source_key=task_id.casefold().replace("-", "_"),
        title=f"Task {task_id}",
        description=f"Deterministic planning data for {task_id}.",
        task_type=TaskType.IMPLEMENTATION,
        materialization_policy=TaskMaterializationPolicy.REQUIRED,
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


def _spec(
    *, spec_id: str = "SPEC-TEST-V001", version: int = 1
) -> ApprovedRequirementSpec:
    return ApprovedRequirementSpec(
        spec_id=spec_id,
        lineage_id="spec-lineage",
        version=version,
        supersedes_spec_id=None,
        source_analysis_revision=0,
        created_at="2026-08-09T12:00:00+00:00",
        content_hash="1" * 64,
        normalized_problem_statement="Build the governed test service.",
        requirement_type="greenfield",
        assumptions=(),
        functional_requirements=(),
        nonfunctional_requirements=(),
        constraints=(),
        acceptance_criteria=(),
        risks=(),
        ambiguities=(),
    )


def _initialize(graph: TaskGraph) -> TaskGraphExecutionState:
    return initialize_task_graph_execution(
        graph, authoritative_requirement_spec=_spec()
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
    execution = _initialize(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    execution = start_task(graph, execution, "TASK-003")
    return graph, execution


def test_initialization_assigns_ready_blocked_and_zero_attempts() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))

    execution = _initialize(graph)

    assert execution.graph_id == graph.graph_id
    assert execution.status is TaskGraphExecutionStatus.PENDING
    assert _status(execution, "TASK-001") is TaskExecutionStatus.READY
    assert _status(execution, "TASK-002") is TaskExecutionStatus.BLOCKED
    assert [state.attempt_count for state in execution.task_states] == [0, 0]


def test_initialization_accepts_graph_bound_to_current_requirement_authority() -> None:
    graph = _graph(_task("TASK-001"))

    execution = initialize_task_graph_execution(
        graph, authoritative_requirement_spec=_spec()
    )

    assert execution.status is TaskGraphExecutionStatus.PENDING
    assert _status(execution, "TASK-001") is TaskExecutionStatus.READY


def test_initialization_rejects_stale_task_graph_source_authority() -> None:
    graph = _graph(_task("TASK-001"))

    with raises(TaskExecutionError, match="^STALE_TASK_GRAPH:"):
        initialize_task_graph_execution(
            graph,
            authoritative_requirement_spec=_spec(
                spec_id="SPEC-TEST-V002", version=2
            ),
        )


def test_ready_task_ids_preserve_canonical_graph_order() -> None:
    graph = _graph(_task("TASK-010"), _task("TASK-002"), _task("TASK-007"))
    execution = _initialize(graph)

    assert ready_task_ids(graph, execution) == (
        "TASK-010",
        "TASK-002",
        "TASK-007",
    )


def test_ready_task_wave_is_canonical_and_capped_at_fixed_parallel_limit() -> None:
    graph = _graph(_task("TASK-010"), _task("TASK-002"), _task("TASK-007"))
    execution = _initialize(graph)

    assert MAX_PARALLEL_TASK_EXECUTIONS == 2
    assert ready_task_wave_ids(graph, execution) == ("TASK-010", "TASK-002")


def test_ready_task_wave_is_empty_for_terminal_execution() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = _initialize(graph)
    execution = start_task_wave(graph, execution, ("TASK-001", "TASK-002"))
    execution = mark_task_failed(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-002")

    assert execution.status is TaskGraphExecutionStatus.FAILED
    assert ready_task_wave_ids(graph, execution) == ()


def test_start_task_wave_authorizes_all_members_in_order_and_counts_attempts() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"), _task("TASK-003"))
    initial = _initialize(graph)

    started = start_task_wave(graph, initial, ("TASK-001", "TASK-002"))

    assert started.status is TaskGraphExecutionStatus.RUNNING
    assert _status(started, "TASK-001") is TaskExecutionStatus.RUNNING
    assert _status(started, "TASK-002") is TaskExecutionStatus.RUNNING
    assert _status(started, "TASK-003") is TaskExecutionStatus.READY
    assert _attempts(started, "TASK-001") == 1
    assert _attempts(started, "TASK-002") == 1
    assert _attempts(started, "TASK-003") == 0


def test_start_task_wave_rejects_noncanonical_selection_before_authorization() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"), _task("TASK-003"))
    initial = _initialize(graph)

    with raises(TaskExecutionError, match="canonical bounded READY"):
        start_task_wave(graph, initial, ("TASK-002", "TASK-001"))

    assert all(
        state.status is TaskExecutionStatus.READY
        and state.attempt_count == 0
        for state in initial.task_states
    )


def test_serialized_fallback_authorizes_one_explicit_ready_retry() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    initial = _initialize(graph)

    started = start_serialized_task_wave(graph, initial, "TASK-002")

    assert _status(started, "TASK-001") is TaskExecutionStatus.READY
    assert _status(started, "TASK-002") is TaskExecutionStatus.RUNNING
    assert _attempts(started, "TASK-002") == 1


def test_workspace_integrity_failure_aborts_running_peer_before_safe_stop() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    running = start_task_wave(
        graph,
        _initialize(graph),
        ("TASK-001", "TASK-002"),
    )

    aborted = abort_running_task(graph, running, "TASK-001")
    aborted = abort_running_task(graph, aborted, "TASK-002")
    stopped = safe_stop_task_graph_execution(graph, aborted)

    assert stopped.status is TaskGraphExecutionStatus.SAFE_STOPPED
    assert all(
        item.status is TaskExecutionStatus.ABORTED for item in stopped.task_states
    )


def test_predispatch_integrity_failure_freezes_ready_graph() -> None:
    graph = _graph(_task("TASK-001"))
    initial = _initialize(graph)

    failed = fail_task_graph_integrity(graph, initial)

    assert failed.status is TaskGraphExecutionStatus.FAILED
    assert _status(failed, "TASK-001") is TaskExecutionStatus.READY
    assert ready_task_ids(graph, failed) == ()


def test_start_task_transitions_ready_to_running_and_increments_attempt() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    initial = _initialize(graph)

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
    execution = _initialize(graph)

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
    execution = _initialize(graph)

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
    execution = _initialize(graph)

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
    execution = _initialize(graph)
    execution = start_task(graph, execution, "TASK-001")

    failed = mark_task_failed(graph, execution, "TASK-001")

    assert failed.status is TaskGraphExecutionStatus.FAILED
    assert _status(failed, "TASK-001") is TaskExecutionStatus.FAILED
    assert _status(failed, "TASK-002") is TaskExecutionStatus.BLOCKED
    assert ready_task_ids(graph, failed) == ()


def test_failed_execution_can_transition_to_safe_stopped() -> None:
    graph = _graph(_task("TASK-001"))
    execution = start_task(
        graph, _initialize(graph), "TASK-001"
    )
    failed = mark_task_failed(graph, execution, "TASK-001")

    safe_stopped = safe_stop_task_graph_execution(graph, failed)

    assert safe_stopped.status is TaskGraphExecutionStatus.SAFE_STOPPED
    assert safe_stopped.task_states == failed.task_states
    assert ready_task_ids(graph, safe_stopped) == ()


def test_cannot_start_blocked_running_or_succeeded_task() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))
    initial = _initialize(graph)
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
    execution = _initialize(graph)

    with raises(TaskExecutionError, match="cannot succeed from READY"):
        mark_task_succeeded(graph, execution, "TASK-001")
    with raises(TaskExecutionError, match="cannot fail from READY"):
        mark_task_failed(graph, execution, "TASK-001")


def test_unknown_task_ids_are_rejected() -> None:
    graph = _graph(_task("TASK-001"))
    execution = _initialize(graph)

    with raises(TaskExecutionError, match="Unknown task ID"):
        start_task(graph, execution, "TASK-999")
    with raises(TaskExecutionError, match="Unknown task ID"):
        mark_task_succeeded(graph, execution, "TASK-999")
    with raises(TaskExecutionError, match="Unknown task ID"):
        mark_task_failed(graph, execution, "TASK-999")


def test_failed_graph_rejects_dispatch_and_non_running_task_settlement() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    execution = start_task(
        graph, _initialize(graph), "TASK-001"
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
    execution = _initialize(graph)
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
    execution = _initialize(graph)
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
    execution = _initialize(graph)
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
    execution = _initialize(graph)
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
    execution = _initialize(graph)

    with raises(TaskExecutionError, match="Only a FAILED"):
        safe_stop_task_graph_execution(graph, execution)


def test_execution_cannot_be_used_with_another_graph() -> None:
    first = _graph(_task("TASK-001"), graph_id="GRAPH-ONE-V001")
    second = _graph(_task("TASK-001"), graph_id="GRAPH-TWO-V001")
    execution = _initialize(first)

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
    execution = _initialize(graph)
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


def test_prepare_retry_returns_running_task_to_ready_without_counting_attempt() -> None:
    graph = _graph(
        _task("TASK-001"),
        _task("TASK-002"),
        _task("TASK-003", "TASK-001"),
    )
    running = start_task(
        graph, _initialize(graph), "TASK-001"
    )

    retry_ready = prepare_task_retry(graph, running, "TASK-001")

    assert retry_ready.status is TaskGraphExecutionStatus.RUNNING
    assert _status(retry_ready, "TASK-001") is TaskExecutionStatus.READY
    assert _attempts(retry_ready, "TASK-001") == 1
    assert _status(retry_ready, "TASK-002") is TaskExecutionStatus.READY
    assert _status(retry_ready, "TASK-003") is TaskExecutionStatus.BLOCKED
    assert ready_task_ids(graph, retry_ready) == ("TASK-001", "TASK-002")

    second_attempt = start_task(graph, retry_ready, "TASK-001")
    assert _status(second_attempt, "TASK-001") is TaskExecutionStatus.RUNNING
    assert _attempts(second_attempt, "TASK-001") == 2


def test_prepare_retry_rejects_invalid_task_and_graph_states_or_exhaustion() -> None:
    graph = _graph(_task("TASK-001"), _task("TASK-002"))
    initial = _initialize(graph)
    one_running = start_task(graph, initial, "TASK-002")
    with raises(TaskExecutionError, match="READY"):
        prepare_task_retry(graph, one_running, "TASK-001")

    blocked_graph = _graph(_task("TASK-001"), _task("TASK-002", "TASK-001"))
    blocked_initial = _initialize(blocked_graph)
    blocked_running = start_task(blocked_graph, blocked_initial, "TASK-001")
    with raises(TaskExecutionError, match="BLOCKED"):
        prepare_task_retry(blocked_graph, blocked_running, "TASK-002")

    running = start_task(graph, initial, "TASK-001")
    exhausted = running.model_copy(
        update={
            "task_states": (
                running.task_states[0].model_copy(
                    update={"attempt_count": MAX_TASK_EXECUTION_ATTEMPTS}
                ),
                running.task_states[1],
            )
        }
    )
    with raises(TaskExecutionError, match="exhausted"):
        prepare_task_retry(graph, exhausted, "TASK-001")
    exhausted_ready = exhausted.model_copy(
        update={
            "task_states": (
                exhausted.task_states[0].model_copy(
                    update={"status": TaskExecutionStatus.READY}
                ),
                exhausted.task_states[1],
            )
        }
    )
    with raises(TaskExecutionError, match="cannot exceed 3"):
        start_task(graph, exhausted_ready, "TASK-001")

    failed = mark_task_failed(graph, running, "TASK-001")
    with raises(TaskExecutionError, match="RUNNING TaskGraph"):
        prepare_task_retry(graph, failed, "TASK-001")


def test_recovery_policy_separates_retryability_from_budget_action() -> None:
    retry = decide_task_execution_recovery(
        task_id="TASK-001",
        attempt_number=1,
        request_id="request-1",
        attempt_id="attempt-1",
        failure_kind=TaskExecutionRecoveryFailureKind.EXECUTOR,
        retryable=True,
        feedback="Transient provider failure.",
    )
    exhausted = decide_task_execution_recovery(
        task_id="TASK-001",
        attempt_number=MAX_TASK_EXECUTION_ATTEMPTS,
        request_id="request-3",
        attempt_id="attempt-3",
        failure_kind=TaskExecutionRecoveryFailureKind.VALIDATION,
        retryable=True,
        feedback="Blank artifact content.",
    )
    terminal = decide_task_execution_recovery(
        task_id="TASK-001",
        attempt_number=1,
        request_id=None,
        attempt_id=None,
        failure_kind=TaskExecutionRecoveryFailureKind.REQUEST_BUILD,
        retryable=False,
        feedback="Authoritative request evidence is invalid.",
    )

    assert retry.action is TaskExecutionRecoveryAction.RETRY
    assert retry.max_attempts == 3
    assert exhausted.retryable is True
    assert exhausted.action is TaskExecutionRecoveryAction.FAIL_TASK
    assert "exhausted" in exhausted.reason
    assert terminal.action is TaskExecutionRecoveryAction.FAIL_TASK
