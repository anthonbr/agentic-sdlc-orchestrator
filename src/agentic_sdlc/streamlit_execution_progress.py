"""Thread-safe, read-only Streamlit projection of existing execution events."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any

from agentic_sdlc.task_execution_progress import (
    GovernedTaskExecutionStarted,
    TaskExecutionAttemptSettled,
    TaskExecutionAttemptStarted,
    TaskExecutionHeartbeat,
    TaskExecutionProgressAttempt,
    TaskExecutionProgressEvent,
    TaskExecutionSettledOutcome,
    TaskExecutionWaveStarted,
    TaskExecutorCompleted,
)


# Raw telemetry is useful for a short UI timeline, but it must not grow without
# bound during a long-running browser session. Per-task summaries remain retained.
DEFAULT_EXECUTION_EVENT_HISTORY_LIMIT = 200


@dataclass(frozen=True, slots=True)
class StreamlitTaskExecutionProgress:
    """Immutable presentation summary for one canonical or unknown task ID."""

    task_id: str
    title: str | None
    layer_number: int | None
    status: str
    attempt_number: int
    retry_count: int
    wave_number: int | None
    wave_mode: str | None
    attempt_elapsed_seconds: float | None
    latest_detail: str | None
    unknown_task: bool


@dataclass(frozen=True, slots=True)
class StreamlitExecutionEvent:
    """Immutable recent-event projection for the collapsed UI timeline."""

    sequence: int
    event_type: str
    elapsed_seconds: float
    wave_number: int | None
    task_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class StreamlitExecutionProgressView:
    """Immutable cross-thread execution telemetry projection for one operation."""

    run_id: str
    operation_id: str
    telemetry_status: str
    current_wave_number: int | None
    current_wave_mode: str | None
    current_layer_numbers: tuple[int, ...]
    completed_task_count: int
    total_task_count: int
    retry_count: int
    failed_task_count: int
    elapsed_seconds: float | None
    execution_layers: tuple[tuple[str, ...], ...]
    tasks: tuple[StreamlitTaskExecutionProgress, ...]
    recent_events: tuple[StreamlitExecutionEvent, ...]
    dropped_event_count: int


@dataclass(slots=True)
class _MutableTaskProgress:
    task_id: str
    title: str | None
    layer_number: int | None
    unknown_task: bool
    status: str = "AWAITING_EVENT"
    attempt_number: int = 0
    retry_count: int = 0
    wave_number: int | None = None
    wave_mode: str | None = None
    attempt_started_at: float | None = None
    settled_elapsed_seconds: float | None = None
    latest_detail: str | None = None


class StreamlitExecutionProgressCollector:
    """Collect existing structured events without acquiring execution authority."""

    def __init__(
        self,
        *,
        history_limit: int = DEFAULT_EXECUTION_EVENT_HISTORY_LIMIT,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if history_limit < 1:
            raise ValueError("Execution progress history limit must be positive.")
        self._history_limit = history_limit
        self._clock = clock
        self._lock = Lock()
        self._run_id: str | None = None
        self._clear_execution_locked()

    def reset(self) -> None:
        """Discard telemetry before the session schedules a genuinely new run."""

        with self._lock:
            self._run_id = None
            self._clear_execution_locked()

    def attach_run(self, run_id: str) -> None:
        """Bind this session-owned reporter to exactly one governed run."""

        if not run_id:
            raise ValueError("Execution progress run ID must be non-empty.")
        with self._lock:
            if self._run_id != run_id:
                self._clear_execution_locked()
                self._run_id = run_id

    def begin_execution(
        self,
        *,
        run_id: str,
        operation_id: str,
        candidate_task_graph: Mapping[str, Any],
        graph_semantics: Mapping[str, Any],
    ) -> bool:
        """Start observing one TaskGraph approval operation for the bound run."""

        if not operation_id:
            raise ValueError("Execution progress operation ID must be non-empty.")
        with self._lock:
            if self._run_id != run_id:
                return False
            if self._active and self._operation_id == operation_id:
                return False

            self._clear_execution_locked()
            self._operation_id = operation_id
            self._active = True
            self._execution_layers = _execution_layers(
                graph_semantics.get("execution_layers")
            )
            layer_by_task = {
                task_id: layer_number
                for layer_number, task_ids in enumerate(
                    self._execution_layers,
                    start=1,
                )
                for task_id in task_ids
            }
            for task in _mapping_sequence(candidate_task_graph.get("tasks")):
                task_id = task.get("task_id")
                title = task.get("title")
                if not isinstance(task_id, str) or not task_id:
                    continue
                if task_id in self._tasks:
                    continue
                self._task_order.append(task_id)
                self._tasks[task_id] = _MutableTaskProgress(
                    task_id=task_id,
                    title=title if isinstance(title, str) and title else None,
                    layer_number=layer_by_task.get(task_id),
                    unknown_task=False,
                )
            self._canonical_task_count = len(self._tasks)
            return True

    def finish_execution(self, *, run_id: str, operation_id: str) -> bool:
        """Stop the observation clock while retaining final telemetry evidence."""

        with self._lock:
            if (
                self._run_id != run_id
                or self._operation_id != operation_id
                or not self._active
            ):
                return False
            self._active = False
            self._finished_at = self._last_event_at or self._clock()
            return True

    def report(self, event: TaskExecutionProgressEvent) -> None:
        """Consume one existing structured event on the orchestration thread."""

        with self._lock:
            if not self._active or self._operation_id is None:
                return
            observed_at = self._clock()
            if self._started_at is None:
                self._started_at = observed_at
            self._last_event_at = observed_at

            wave_number: int | None = None
            task_ids: tuple[str, ...] = ()
            detail: str

            if isinstance(event, GovernedTaskExecutionStarted):
                self._started = True
                detail = "Governed Task Agent execution started."
            elif isinstance(event, TaskExecutionWaveStarted):
                self._started = True
                wave_number = event.wave_number
                task_ids = tuple(attempt.task_id for attempt in event.attempts)
                self._current_wave_number = event.wave_number
                self._current_wave_mode = event.mode.value
                self._current_wave_task_ids = task_ids
                for attempt in event.attempts:
                    task = self._task_for_attempt_locked(attempt)
                    task.status = "PREPARING"
                    task.attempt_number = attempt.attempt_number
                    task.wave_number = event.wave_number
                    task.wave_mode = event.mode.value
                    task.attempt_started_at = None
                    task.settled_elapsed_seconds = None
                    task.latest_detail = "Preparing the authorized attempt."
                detail = (
                    f"{event.mode.value} wave started with "
                    f"{len(event.attempts)} attempt(s)."
                )
            elif isinstance(event, TaskExecutionAttemptStarted):
                wave_number = event.wave_number
                task_ids = (event.attempt.task_id,)
                task = self._task_for_attempt_locked(event.attempt)
                task.status = "RUNNING"
                task.attempt_number = event.attempt.attempt_number
                task.wave_number = event.wave_number
                task.wave_mode = self._current_wave_mode
                task.attempt_started_at = observed_at
                task.settled_elapsed_seconds = None
                task.latest_detail = "Task Agent attempt is running."
                detail = f"Attempt {event.attempt.attempt_number} started."
            elif isinstance(event, TaskExecutionHeartbeat):
                wave_number = event.wave_number
                task_ids = tuple(
                    attempt.task_id for attempt in event.outstanding_attempts
                )
                for attempt in event.outstanding_attempts:
                    task = self._task_for_attempt_locked(attempt)
                    task.status = "RUNNING"
                    task.attempt_number = attempt.attempt_number
                    task.wave_number = event.wave_number
                    task.latest_detail = "Task Agent attempt is still running."
                detail = f"{len(task_ids)} attempt(s) still running."
            elif isinstance(event, TaskExecutorCompleted):
                wave_number = event.wave_number
                task_ids = (event.attempt.task_id,)
                task = self._task_for_attempt_locked(event.attempt)
                task.status = "VALIDATING"
                task.attempt_number = event.attempt.attempt_number
                task.wave_number = event.wave_number
                task.latest_detail = "Executor completed; validating result."
                detail = "Executor completed; validating result."
            elif isinstance(event, TaskExecutionAttemptSettled):
                wave_number = event.wave_number
                task_ids = (event.attempt.task_id,)
                task = self._task_for_attempt_locked(event.attempt)
                task.status = event.outcome.value
                task.attempt_number = event.attempt.attempt_number
                task.wave_number = event.wave_number
                task.latest_detail = event.detail
                if task.attempt_started_at is not None:
                    task.settled_elapsed_seconds = max(
                        0.0,
                        observed_at - task.attempt_started_at,
                    )
                if event.outcome is TaskExecutionSettledOutcome.RETRY_SCHEDULED:
                    task.retry_count += 1
                detail = f"{event.outcome.value}: {event.detail}"
            else:
                return

            self._append_event_locked(
                event_type=type(event).__name__,
                observed_at=observed_at,
                wave_number=wave_number,
                task_ids=task_ids,
                detail=detail,
            )

    def snapshot(
        self,
        *,
        run_id: str | None = None,
    ) -> StreamlitExecutionProgressView | None:
        """Return an immutable view isolated to the requested governed run."""

        with self._lock:
            if self._run_id is None or self._operation_id is None:
                return None
            if run_id is not None and run_id != self._run_id:
                return None
            observed_at = self._clock()
            task_views = tuple(
                self._task_view_locked(self._tasks[task_id], observed_at)
                for task_id in self._task_order
            )
            canonical_tasks = task_views[: self._canonical_task_count]
            current_layers = tuple(
                sorted(
                    {
                        task.layer_number
                        for task in canonical_tasks
                        if task.task_id in self._current_wave_task_ids
                        and task.layer_number is not None
                    }
                )
            )
            return StreamlitExecutionProgressView(
                run_id=self._run_id,
                operation_id=self._operation_id,
                telemetry_status=(
                    "IN_PROGRESS"
                    if self._active and self._started
                    else "AWAITING_EVENT"
                    if self._active
                    else "OBSERVATION_COMPLETE"
                ),
                current_wave_number=self._current_wave_number,
                current_wave_mode=self._current_wave_mode,
                current_layer_numbers=current_layers,
                completed_task_count=sum(
                    task.status == TaskExecutionSettledOutcome.SUCCEEDED.value
                    for task in canonical_tasks
                ),
                total_task_count=self._canonical_task_count,
                retry_count=sum(task.retry_count for task in canonical_tasks),
                failed_task_count=sum(
                    task.status
                    in {
                        TaskExecutionSettledOutcome.FAILED.value,
                        TaskExecutionSettledOutcome.SAFE_STOPPED.value,
                    }
                    for task in canonical_tasks
                ),
                elapsed_seconds=self._elapsed_locked(observed_at),
                execution_layers=self._execution_layers,
                tasks=task_views,
                recent_events=tuple(self._events),
                dropped_event_count=self._dropped_event_count,
            )

    def _task_for_attempt_locked(
        self,
        attempt: TaskExecutionProgressAttempt,
    ) -> _MutableTaskProgress:
        task = self._tasks.get(attempt.task_id)
        if task is not None:
            return task
        task = _MutableTaskProgress(
            task_id=attempt.task_id,
            title=None,
            layer_number=None,
            unknown_task=True,
        )
        self._tasks[attempt.task_id] = task
        self._task_order.append(attempt.task_id)
        return task

    def _task_view_locked(
        self,
        task: _MutableTaskProgress,
        observed_at: float,
    ) -> StreamlitTaskExecutionProgress:
        elapsed = task.settled_elapsed_seconds
        if task.attempt_started_at is not None and task.status in {
            "RUNNING",
            "VALIDATING",
        }:
            elapsed = max(0.0, observed_at - task.attempt_started_at)
        return StreamlitTaskExecutionProgress(
            task_id=task.task_id,
            title=task.title,
            layer_number=task.layer_number,
            status=task.status,
            attempt_number=task.attempt_number,
            retry_count=task.retry_count,
            wave_number=task.wave_number,
            wave_mode=task.wave_mode,
            attempt_elapsed_seconds=elapsed,
            latest_detail=task.latest_detail,
            unknown_task=task.unknown_task,
        )

    def _append_event_locked(
        self,
        *,
        event_type: str,
        observed_at: float,
        wave_number: int | None,
        task_ids: tuple[str, ...],
        detail: str,
    ) -> None:
        self._event_sequence += 1
        if len(self._events) == self._history_limit:
            self._dropped_event_count += 1
        self._events.append(
            StreamlitExecutionEvent(
                sequence=self._event_sequence,
                event_type=event_type,
                elapsed_seconds=(
                    max(0.0, observed_at - self._started_at)
                    if self._started_at is not None
                    else 0.0
                ),
                wave_number=wave_number,
                task_ids=task_ids,
                detail=detail,
            )
        )

    def _elapsed_locked(self, observed_at: float) -> float | None:
        if self._started_at is None:
            return None
        end = (
            observed_at
            if self._active
            else self._last_event_at or self._finished_at or observed_at
        )
        return max(0.0, end - self._started_at)

    def _clear_execution_locked(self) -> None:
        self._operation_id: str | None = None
        self._active = False
        self._started = False
        self._started_at: float | None = None
        self._last_event_at: float | None = None
        self._finished_at: float | None = None
        self._current_wave_number: int | None = None
        self._current_wave_mode: str | None = None
        self._current_wave_task_ids: tuple[str, ...] = ()
        self._execution_layers: tuple[tuple[str, ...], ...] = ()
        self._tasks: dict[str, _MutableTaskProgress] = {}
        self._task_order: list[str] = []
        self._canonical_task_count = 0
        self._events: deque[StreamlitExecutionEvent] = deque(
            maxlen=self._history_limit
        )
        self._event_sequence = 0
        self._dropped_event_count = 0


def _execution_layers(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        tuple(item for item in layer if isinstance(item, str) and item)
        for layer in value
        if isinstance(layer, Sequence) and not isinstance(layer, (str, bytes))
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))
