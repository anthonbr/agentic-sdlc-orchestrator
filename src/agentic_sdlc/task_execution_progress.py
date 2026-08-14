"""Ephemeral Task Agent execution progress outside governed workflow state."""

from __future__ import annotations

from concurrent.futures import Future, wait
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Collection, Protocol, TextIO, TypeAlias

from agentic_sdlc.task_graph import ValidationExecutionProfile
from agentic_sdlc.validation_execution_contracts import ValidationExecutionOutcome


DEFAULT_TASK_EXECUTION_HEARTBEAT_SECONDS = 5.0


class TaskExecutionWaveMode(StrEnum):
    """Human-facing dispatch shape without changing scheduler semantics."""

    SINGLE = "SINGLE"
    PARALLEL = "PARALLEL"
    SERIALIZED_RETRY = "SERIALIZED_RETRY"


class TaskExecutionSettledOutcome(StrEnum):
    """Application-classified outcome after deterministic wave settlement."""

    SUCCEEDED = "SUCCEEDED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    SAFE_STOPPED = "SAFE_STOPPED"


@dataclass(frozen=True)
class TaskExecutionProgressAttempt:
    """Display identity for one approved TaskGraph attempt."""

    task_id: str
    attempt_number: int
    title: str


@dataclass(frozen=True)
class GovernedTaskExecutionStarted:
    """Final TaskGraph approval has resumed governed execution."""


@dataclass(frozen=True)
class TaskExecutionWaveStarted:
    """One canonical scheduler wave is beginning request preparation."""

    wave_number: int
    mode: TaskExecutionWaveMode
    attempts: tuple[TaskExecutionProgressAttempt, ...]


@dataclass(frozen=True)
class TaskExecutionAttemptStarted:
    """One prepared request is about to be submitted to the executor pool."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt


@dataclass(frozen=True)
class TaskExecutionHeartbeat:
    """Executor futures remain incomplete after the bounded wait interval."""

    wave_number: int
    outstanding_attempts: tuple[TaskExecutionProgressAttempt, ...]


@dataclass(frozen=True)
class TaskExecutorCompleted:
    """An executor call returned; governed result validation has not settled."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt


@dataclass(frozen=True)
class TaskValidationExecutionStarted:
    """One approved fixed-profile validation is about to execute."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt
    validation_requirement_id: str
    profile: ValidationExecutionProfile


@dataclass(frozen=True)
class TaskValidationProvisioningStarted:
    """Application-owned dependency provisioning is about to execute."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt
    validation_requirement_id: str
    profile: ValidationExecutionProfile


@dataclass(frozen=True)
class TaskValidationProvisioningCompleted:
    """Dependency provisioning returned immutable bounded evidence."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt
    validation_requirement_id: str
    profile: ValidationExecutionProfile
    outcome: ValidationExecutionOutcome


@dataclass(frozen=True)
class TaskValidationExecutionCompleted:
    """Governed validation returned trusted bounded execution evidence."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt
    validation_requirement_id: str
    profile: ValidationExecutionProfile
    outcome: ValidationExecutionOutcome


@dataclass(frozen=True)
class TaskExecutionAttemptSettled:
    """Deterministic application settlement for one canonical wave member."""

    wave_number: int
    attempt: TaskExecutionProgressAttempt
    outcome: TaskExecutionSettledOutcome
    detail: str


TaskExecutionProgressEvent: TypeAlias = (
    GovernedTaskExecutionStarted
    | TaskExecutionWaveStarted
    | TaskExecutionAttemptStarted
    | TaskExecutionHeartbeat
    | TaskExecutorCompleted
    | TaskValidationExecutionStarted
    | TaskValidationProvisioningStarted
    | TaskValidationProvisioningCompleted
    | TaskValidationExecutionCompleted
    | TaskExecutionAttemptSettled
)


class TaskExecutionProgressReporter(Protocol):
    """Observe ephemeral execution activity without affecting workflow state."""

    def report(self, event: TaskExecutionProgressEvent) -> None:
        """Render or record one application-owned progress event."""


class NullTaskExecutionProgressReporter:
    """Default reporter for library callers that request no runtime output."""

    def report(self, event: TaskExecutionProgressEvent) -> None:
        del event


class TaskExecutionWaiter(Protocol):
    """Wait for executor futures with an injectable heartbeat timeout."""

    def wait(
        self,
        futures: Collection[Future[Any]],
        *,
        timeout_seconds: float,
    ) -> tuple[set[Future[Any]], set[Future[Any]]]:
        """Return completed and outstanding futures after one bounded wait."""


class ConcurrentFutureTaskExecutionWaiter:
    """Production waiter backed by ``concurrent.futures.wait``."""

    def wait(
        self,
        futures: Collection[Future[Any]],
        *,
        timeout_seconds: float,
    ) -> tuple[set[Future[Any]], set[Future[Any]]]:
        done, outstanding = wait(futures, timeout=timeout_seconds)
        return set(done), set(outstanding)


class ConsoleTaskExecutionProgressReporter:
    """Render concise, single-threaded live progress for the built-in CLI."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream

    def report(self, event: TaskExecutionProgressEvent) -> None:
        if isinstance(event, GovernedTaskExecutionStarted):
            self._write(
                "TaskGraph approved. Beginning governed Task Agent execution..."
            )
            return
        if isinstance(event, TaskExecutionWaveStarted):
            count = len(event.attempts)
            parallel = (
                " in parallel"
                if event.mode is TaskExecutionWaveMode.PARALLEL
                else ""
            )
            noun = "attempt" if count == 1 else "attempts"
            self._write(
                f"[wave {event.wave_number}] Starting {count} Task Agent "
                f"{noun}{parallel}:"
            )
            for attempt in event.attempts:
                self._write(
                    f"  {attempt.task_id} attempt {attempt.attempt_number} — "
                    f"{attempt.title}"
                )
            return
        if isinstance(event, TaskExecutionAttemptStarted):
            # The wave-start block already identifies submitted attempts without
            # duplicating one console line per member.
            return
        if isinstance(event, TaskExecutionHeartbeat):
            if len(event.outstanding_attempts) == 1:
                attempt = event.outstanding_attempts[0]
                self._write(
                    f"[wave {event.wave_number}] {attempt.task_id} attempt "
                    f"{attempt.attempt_number} still running..."
                )
                return
            identities = ", ".join(
                f"{attempt.task_id} attempt {attempt.attempt_number}"
                for attempt in event.outstanding_attempts
            )
            self._write(
                f"[wave {event.wave_number}] {len(event.outstanding_attempts)} "
                f"Task Agent attempts still running: {identities}"
            )
            return
        if isinstance(event, TaskExecutorCompleted):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} executor completed; validating result..."
            )
            return
        if isinstance(event, TaskValidationExecutionStarted):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} executing required "
                f"{event.profile.value} validation..."
            )
            return
        if isinstance(event, TaskValidationProvisioningStarted):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} provisioning dependencies for "
                f"{event.profile.value}..."
            )
            return
        if isinstance(event, TaskValidationProvisioningCompleted):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} dependency provisioning "
                f"{event.outcome.value.lower()}."
            )
            return
        if isinstance(event, TaskValidationExecutionCompleted):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} {event.profile.value} "
                f"{event.outcome.value.lower()}."
            )
            return
        if isinstance(event, TaskExecutionAttemptSettled):
            attempt = event.attempt
            self._write(
                f"[wave {event.wave_number}] {attempt.task_id} attempt "
                f"{attempt.attempt_number} {event.detail}"
            )

    def _write(self, message: str) -> None:
        if self._stream is None:
            print(message)
        else:
            print(message, file=self._stream)


def emit_task_execution_progress(
    reporter: TaskExecutionProgressReporter,
    event: TaskExecutionProgressEvent,
) -> None:
    """Keep reporter failure outside governed execution authority."""

    try:
        reporter.report(event)
    except Exception:
        # Progress is deliberately best-effort UI. It cannot settle, retry, or
        # invalidate an otherwise governed workflow operation.
        return
