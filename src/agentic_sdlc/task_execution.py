"""Deterministic runtime state and scheduling for an approved TaskGraph."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agentic_sdlc.task_graph import TaskGraph


MAX_TASK_EXECUTION_ATTEMPTS = 3
MAX_PARALLEL_TASK_EXECUTIONS = 2


class TaskExecutionStatus(StrEnum):
    """Runtime status of one canonical engineering task."""

    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class TaskGraphExecutionStatus(StrEnum):
    """Runtime status of one attempt to interpret an approved TaskGraph."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SAFE_STOPPED = "SAFE_STOPPED"


class TaskExecutionState(BaseModel):
    """Runtime state for one task; planning fields remain on the canonical Task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    status: TaskExecutionStatus
    attempt_count: int = Field(default=0, ge=0)


class TaskGraphExecutionState(BaseModel):
    """Immutable runtime snapshot for one approved TaskGraph execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    graph_id: str
    status: TaskGraphExecutionStatus
    task_states: tuple[TaskExecutionState, ...]


class TaskExecutionWaveAttempt(BaseModel):
    """Immutable identity for one task attempt authorized in a wave."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    attempt_number: int = Field(ge=1)


class TaskExecutionWave(BaseModel):
    """Immutable audit evidence for one bounded deterministic dispatch wave."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    wave_number: int = Field(ge=1)
    task_attempts: tuple[TaskExecutionWaveAttempt, ...] = Field(
        min_length=1,
        max_length=MAX_PARALLEL_TASK_EXECUTIONS,
    )


class TaskExecutionError(ValueError):
    """Raised when runtime state or a requested transition is invalid."""


class TaskExecutionFailurePhase(StrEnum):
    """Restrained phases for application-owned execution failure evidence."""

    REQUEST_BUILD = "REQUEST_BUILD"
    EXECUTOR = "EXECUTOR"
    CANONICALIZATION = "CANONICALIZATION"


class TaskExecutionFailure(BaseModel):
    """Immutable evidence for a task attempt that produced no validation record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    attempt_number: int = Field(ge=1)
    request_id: str | None
    attempt_id: str | None
    phase: TaskExecutionFailurePhase
    error_type: str
    message: str


class TaskExecutionRecoveryFailureKind(StrEnum):
    """Failure categories considered by deterministic recovery policy."""

    REQUEST_BUILD = "REQUEST_BUILD"
    EXECUTOR = "EXECUTOR"
    CANONICALIZATION = "CANONICALIZATION"
    VALIDATION = "VALIDATION"
    MATERIALIZATION = "MATERIALIZATION"
    WORKSPACE_CONFLICT = "WORKSPACE_CONFLICT"
    WORKSPACE_MUTATION = "WORKSPACE_MUTATION"


class TaskExecutionRecoveryAction(StrEnum):
    """Application-owned settlement choice after one failed attempt."""

    RETRY = "RETRY"
    FAIL_TASK = "FAIL_TASK"


class TaskExecutionRecoveryDecision(BaseModel):
    """Immutable policy evidence for recovery after one failed task attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    attempt_number: int = Field(ge=1)
    request_id: str | None
    attempt_id: str | None
    failure_kind: TaskExecutionRecoveryFailureKind
    retryable: bool
    max_attempts: int = Field(ge=1)
    action: TaskExecutionRecoveryAction
    reason: str
    feedback: str


def initialize_task_graph_execution(graph: TaskGraph) -> TaskGraphExecutionState:
    """Create a side-effect-free runtime snapshot from canonical dependencies."""

    if not graph.tasks:
        raise TaskExecutionError("Cannot initialize an empty TaskGraph.")
    return TaskGraphExecutionState(
        graph_id=graph.graph_id,
        status=TaskGraphExecutionStatus.PENDING,
        task_states=tuple(
            TaskExecutionState(
                task_id=task.task_id,
                status=(
                    TaskExecutionStatus.BLOCKED
                    if task.depends_on
                    else TaskExecutionStatus.READY
                ),
            )
            for task in graph.tasks
        ),
    )


def ready_task_ids(
    graph: TaskGraph, execution: TaskGraphExecutionState
) -> tuple[str, ...]:
    """Return runnable task IDs in canonical TaskGraph order."""

    _validate_execution_matches_graph(graph, execution)
    if execution.status not in {
        TaskGraphExecutionStatus.PENDING,
        TaskGraphExecutionStatus.RUNNING,
    }:
        return ()
    return tuple(
        state.task_id
        for state in execution.task_states
        if state.status is TaskExecutionStatus.READY
    )


def ready_task_wave_ids(
    graph: TaskGraph, execution: TaskGraphExecutionState
) -> tuple[str, ...]:
    """Select one bounded READY wave in canonical TaskGraph order."""

    return ready_task_ids(graph, execution)[:MAX_PARALLEL_TASK_EXECUTIONS]


def start_task_wave(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_ids: tuple[str, ...],
) -> TaskGraphExecutionState:
    """Atomically authorize one canonical wave through governed starts."""

    _validate_dispatchable_execution(graph, execution)
    expected = ready_task_wave_ids(graph, execution)
    if not task_ids:
        raise TaskExecutionError("A task execution wave cannot be empty.")
    if task_ids != expected:
        raise TaskExecutionError(
            "Task execution wave must equal the current canonical bounded READY "
            f"selection; expected {expected}, found {task_ids}."
        )

    # Validate every intended member before applying the first immutable transition.
    for task_id in task_ids:
        index = _task_state_index(execution, task_id)
        current = execution.task_states[index]
        if current.status is not TaskExecutionStatus.READY:
            raise TaskExecutionError(
                f"Task {task_id} cannot start from {current.status.value}."
            )
        if current.attempt_count >= MAX_TASK_EXECUTION_ATTEMPTS:
            raise TaskExecutionError(
                f"Task {task_id} cannot exceed {MAX_TASK_EXECUTION_ATTEMPTS} "
                "execution attempts."
            )

    started = execution
    for task_id in task_ids:
        started = start_task(graph, started, task_id)
    return started


def start_serialized_task_wave(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Authorize one canonical READY retry as a serialized fallback wave."""

    _validate_dispatchable_execution(graph, execution)
    ready = ready_task_ids(graph, execution)
    if task_id not in ready:
        raise TaskExecutionError(
            f"Serialized retry task {task_id} is not currently READY."
        )
    return start_task(graph, execution, task_id)


def start_task(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Apply the controlled READY to RUNNING transition."""

    _validate_dispatchable_execution(graph, execution)
    index = _task_state_index(execution, task_id)
    current = execution.task_states[index]
    if current.status is not TaskExecutionStatus.READY:
        raise TaskExecutionError(
            f"Task {task_id} cannot start from {current.status.value}."
        )
    if current.attempt_count >= MAX_TASK_EXECUTION_ATTEMPTS:
        raise TaskExecutionError(
            f"Task {task_id} cannot exceed {MAX_TASK_EXECUTION_ATTEMPTS} "
            "execution attempts."
        )
    updated = TaskExecutionState(
        task_id=task_id,
        status=TaskExecutionStatus.RUNNING,
        attempt_count=current.attempt_count + 1,
    )
    return _replace_task_state(
        execution,
        index,
        updated,
        graph_status=TaskGraphExecutionStatus.RUNNING,
    )


def mark_task_succeeded(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Mark a running task successful and unlock fully satisfied dependents."""

    index, current = _settleable_task(
        graph, execution, task_id, transition="succeed"
    )

    task_states = list(execution.task_states)
    task_states[index] = TaskExecutionState(
        task_id=task_id,
        status=TaskExecutionStatus.SUCCEEDED,
        attempt_count=current.attempt_count,
    )
    if execution.status is not TaskGraphExecutionStatus.FAILED:
        statuses = {state.task_id: state.status for state in task_states}
        for task_index, task in enumerate(graph.tasks):
            state = task_states[task_index]
            if state.status is not TaskExecutionStatus.BLOCKED:
                continue
            if all(
                statuses[dependency] is TaskExecutionStatus.SUCCEEDED
                for dependency in task.depends_on
            ):
                task_states[task_index] = TaskExecutionState(
                    task_id=state.task_id,
                    status=TaskExecutionStatus.READY,
                    attempt_count=state.attempt_count,
                )

    if execution.status is TaskGraphExecutionStatus.FAILED:
        graph_status = TaskGraphExecutionStatus.FAILED
    elif all(
        state.status is TaskExecutionStatus.SUCCEEDED for state in task_states
    ):
        graph_status = TaskGraphExecutionStatus.SUCCEEDED
    else:
        graph_status = TaskGraphExecutionStatus.RUNNING
    return TaskGraphExecutionState(
        graph_id=execution.graph_id,
        status=graph_status,
        task_states=tuple(task_states),
    )


def mark_task_failed(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Mark a running task failed without unlocking dependent work."""

    index, current = _settleable_task(
        graph, execution, task_id, transition="fail"
    )
    failed = TaskExecutionState(
        task_id=task_id,
        status=TaskExecutionStatus.FAILED,
        attempt_count=current.attempt_count,
    )
    return _replace_task_state(
        execution,
        index,
        failed,
        graph_status=TaskGraphExecutionStatus.FAILED,
    )


def abort_running_task(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Abort an authorized peer because run-level workspace proof was lost."""

    index, current = _settleable_task(
        graph, execution, task_id, transition="abort"
    )
    aborted = TaskExecutionState(
        task_id=task_id,
        status=TaskExecutionStatus.ABORTED,
        attempt_count=current.attempt_count,
    )
    return _replace_task_state(
        execution,
        index,
        aborted,
        graph_status=TaskGraphExecutionStatus.FAILED,
    )


def fail_task_graph_integrity(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
) -> TaskGraphExecutionState:
    """Freeze future dispatch when live workspace authority is unprovable."""

    _validate_execution_matches_graph(graph, execution)
    if execution.status not in {
        TaskGraphExecutionStatus.PENDING,
        TaskGraphExecutionStatus.RUNNING,
    }:
        raise TaskExecutionError(
            "Workspace-integrity failure requires a dispatchable graph execution."
        )
    return TaskGraphExecutionState(
        graph_id=execution.graph_id,
        status=TaskGraphExecutionStatus.FAILED,
        task_states=execution.task_states,
    )


def prepare_task_retry(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> TaskGraphExecutionState:
    """Return one running task to READY without beginning another attempt."""

    _validate_execution_matches_graph(graph, execution)
    if execution.status is not TaskGraphExecutionStatus.RUNNING:
        raise TaskExecutionError(
            "Only a RUNNING TaskGraph execution can prepare a task retry; "
            f"found {execution.status.value}."
        )
    index = _task_state_index(execution, task_id)
    current = execution.task_states[index]
    if current.status is not TaskExecutionStatus.RUNNING:
        raise TaskExecutionError(
            f"Task {task_id} cannot prepare retry from {current.status.value}."
        )
    if current.attempt_count >= MAX_TASK_EXECUTION_ATTEMPTS:
        raise TaskExecutionError(
            f"Task {task_id} has exhausted its {MAX_TASK_EXECUTION_ATTEMPTS} "
            "execution attempts."
        )
    ready = TaskExecutionState(
        task_id=task_id,
        status=TaskExecutionStatus.READY,
        attempt_count=current.attempt_count,
    )
    return _replace_task_state(
        execution,
        index,
        ready,
        graph_status=TaskGraphExecutionStatus.RUNNING,
    )


def decide_task_execution_recovery(
    *,
    task_id: str,
    attempt_number: int,
    request_id: str | None,
    attempt_id: str | None,
    failure_kind: TaskExecutionRecoveryFailureKind,
    retryable: bool,
    feedback: str,
) -> TaskExecutionRecoveryDecision:
    """Choose RETRY or FAIL_TASK from intrinsic eligibility and fixed budget."""

    if attempt_number < 1:
        raise TaskExecutionError("Recovery requires a positive attempt number.")
    if not feedback.strip():
        raise TaskExecutionError("Recovery feedback must be non-empty.")

    if not retryable:
        action = TaskExecutionRecoveryAction.FAIL_TASK
        reason = f"{failure_kind.value} failure is non-retryable."
    elif attempt_number >= MAX_TASK_EXECUTION_ATTEMPTS:
        action = TaskExecutionRecoveryAction.FAIL_TASK
        reason = (
            f"Retryable {failure_kind.value} failure exhausted the maximum "
            f"task execution attempts ({MAX_TASK_EXECUTION_ATTEMPTS})."
        )
    else:
        action = TaskExecutionRecoveryAction.RETRY
        reason = (
            f"Retryable {failure_kind.value} failure may use attempt "
            f"{attempt_number + 1} of {MAX_TASK_EXECUTION_ATTEMPTS}."
        )
    return TaskExecutionRecoveryDecision(
        task_id=task_id,
        attempt_number=attempt_number,
        request_id=request_id,
        attempt_id=attempt_id,
        failure_kind=failure_kind,
        retryable=retryable,
        max_attempts=MAX_TASK_EXECUTION_ATTEMPTS,
        action=action,
        reason=reason,
        feedback=feedback,
    )


def safe_stop_task_graph_execution(
    graph: TaskGraph, execution: TaskGraphExecutionState
) -> TaskGraphExecutionState:
    """Convert a failed execution attempt into an explicit safe-stop outcome."""

    _validate_execution_matches_graph(graph, execution)
    if execution.status is not TaskGraphExecutionStatus.FAILED:
        raise TaskExecutionError(
            "Only a FAILED TaskGraph execution can transition to SAFE_STOPPED."
        )
    running_task_ids = tuple(
        state.task_id
        for state in execution.task_states
        if state.status is TaskExecutionStatus.RUNNING
    )
    if running_task_ids:
        raise TaskExecutionError(
            "Cannot safe-stop TaskGraph execution while tasks are still running: "
            f"{', '.join(running_task_ids)}."
        )
    return TaskGraphExecutionState(
        graph_id=execution.graph_id,
        status=TaskGraphExecutionStatus.SAFE_STOPPED,
        task_states=execution.task_states,
    )


def _validate_dispatchable_execution(
    graph: TaskGraph, execution: TaskGraphExecutionState
) -> None:
    _validate_execution_matches_graph(graph, execution)
    if execution.status not in {
        TaskGraphExecutionStatus.PENDING,
        TaskGraphExecutionStatus.RUNNING,
    }:
        raise TaskExecutionError(
            "TaskGraph execution does not permit new task dispatch: "
            f"{execution.status.value}."
        )


def _settleable_task(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
    *,
    transition: str,
) -> tuple[int, TaskExecutionState]:
    _validate_execution_matches_graph(graph, execution)
    index = _task_state_index(execution, task_id)
    current = execution.task_states[index]
    if current.status is not TaskExecutionStatus.RUNNING:
        raise TaskExecutionError(
            f"Task {task_id} cannot {transition} from {current.status.value}."
        )
    if execution.status not in {
        TaskGraphExecutionStatus.RUNNING,
        TaskGraphExecutionStatus.FAILED,
    }:
        raise TaskExecutionError(
            "TaskGraph execution does not permit running task settlement: "
            f"{execution.status.value}."
        )
    return index, current


def _validate_execution_matches_graph(
    graph: TaskGraph, execution: TaskGraphExecutionState
) -> None:
    if execution.graph_id != graph.graph_id:
        raise TaskExecutionError(
            f"Execution graph {execution.graph_id} does not match {graph.graph_id}."
        )
    expected_task_ids = tuple(task.task_id for task in graph.tasks)
    actual_task_ids = tuple(state.task_id for state in execution.task_states)
    if actual_task_ids != expected_task_ids:
        raise TaskExecutionError(
            "Execution task identities/order do not match the canonical TaskGraph."
        )


def _task_state_index(execution: TaskGraphExecutionState, task_id: str) -> int:
    for index, state in enumerate(execution.task_states):
        if state.task_id == task_id:
            return index
    raise TaskExecutionError(f"Unknown task ID: {task_id}.")


def _replace_task_state(
    execution: TaskGraphExecutionState,
    index: int,
    updated: TaskExecutionState,
    *,
    graph_status: TaskGraphExecutionStatus,
) -> TaskGraphExecutionState:
    task_states = list(execution.task_states)
    task_states[index] = updated
    return TaskGraphExecutionState(
        graph_id=execution.graph_id,
        status=graph_status,
        task_states=tuple(task_states),
    )
