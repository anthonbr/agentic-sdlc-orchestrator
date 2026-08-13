"""Session-owned background coordination for the Streamlit presentation adapter."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from agentic_sdlc.application import (
    GovernedRunError,
    GovernedRunLifecycleError,
    GovernedRunRequest,
    GovernedRunService,
    GovernedRunSnapshot,
)
from agentic_sdlc.project_export import normalize_project_name
from agentic_sdlc.requirement_submission import (
    deterministic_project_name,
    resolve_inline_requirement,
)
from agentic_sdlc.state import ApprovalResponse, workflow_input_from_submission


class StreamlitOperationKind(StrEnum):
    """Blocking governed operation currently represented by the UI."""

    START = "START"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class StreamlitRuntimeView:
    """Immutable polling projection that is safe for Streamlit presentation."""

    snapshot: GovernedRunSnapshot | None
    operation_id: str | None
    operation_kind: StreamlitOperationKind | None
    in_flight: bool
    error_message: str | None


class GovernedRunLifecycle(Protocol):
    """Public GovernedRunService surface used by the session runtime."""

    def start_run(self, request: GovernedRunRequest) -> GovernedRunSnapshot: ...

    def inspect_run(self, run_id: str) -> GovernedRunSnapshot: ...

    def resume_run(
        self,
        run_id: str,
        decision: ApprovalResponse,
        *,
        gate_token: str,
    ) -> GovernedRunSnapshot: ...


class BackgroundExecutor(Protocol):
    """Small executor seam for deterministic runtime tests."""

    def submit(
        self,
        function: Callable[..., GovernedRunSnapshot],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[GovernedRunSnapshot]: ...


def governed_run_request_from_inline_requirement(
    requirement_text: str,
    project_name: str,
) -> GovernedRunRequest:
    """Resolve one GUI submission through the authoritative input boundary."""

    submission = resolve_inline_requirement(requirement_text)
    requested_project_name = project_name if project_name != "" else None
    normalized_project_name = (
        normalize_project_name(requested_project_name)
        if requested_project_name is not None
        else deterministic_project_name(submission)
    )
    return GovernedRunRequest(
        command="run",
        workflow_input=workflow_input_from_submission(
            submission,
            project_name=normalized_project_name,
        ),
        requested_project_name=requested_project_name,
    )


class StreamlitRunRuntime:
    """Own one service and serialize its blocking lifecycle calls off-thread."""

    def __init__(
        self,
        service: GovernedRunLifecycle,
        *,
        executor: BackgroundExecutor | None = None,
    ) -> None:
        self._service = service
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agentic-sdlc-streamlit",
        )
        self._owns_executor = executor is None
        self._lock = Lock()
        self._future: Future[GovernedRunSnapshot] | None = None
        self._operation_id: str | None = None
        self._operation_kind: StreamlitOperationKind | None = None
        self._operation_gate_token: str | None = None
        self._known_operation_ids: set[str] = set()
        self._scheduled_gate_tokens: set[str] = set()
        self._start_scheduled = False
        self._snapshot: GovernedRunSnapshot | None = None
        self._error_message: str | None = None

    @classmethod
    def for_repository(cls, repository_root: Path) -> StreamlitRunRuntime:
        """Create the production runtime with one real governed run service."""

        return cls(GovernedRunService(repository_root=repository_root))

    def schedule_start(
        self,
        operation_id: str,
        request: GovernedRunRequest,
    ) -> bool:
        """Schedule the session's only run start exactly once."""

        self._require_operation_id(operation_id)
        with self._lock:
            self._settle_finished_locked()
            if operation_id in self._known_operation_ids:
                return False
            if self._future is not None:
                return False
            if self._start_scheduled or self._snapshot is not None:
                raise GovernedRunLifecycleError(
                    "This browser session already owns a governed run."
                )
            self._known_operation_ids.add(operation_id)
            self._operation_id = operation_id
            self._operation_kind = StreamlitOperationKind.START
            self._operation_gate_token = None
            self._error_message = None
            self._start_scheduled = True
            self._future = self._executor.submit(self._service.start_run, request)
            return True

    def schedule_resume(
        self,
        operation_id: str,
        run_id: str,
        response: ApprovalResponse,
        *,
        gate_token: str,
    ) -> bool:
        """Schedule one response for the exact currently displayed human gate."""

        self._require_operation_id(operation_id)
        with self._lock:
            self._settle_finished_locked()
            if operation_id in self._known_operation_ids:
                return False
            if self._future is not None:
                return False
            gate = self._snapshot.human_gate if self._snapshot is not None else None
            if self._snapshot is None or self._snapshot.run_id != run_id:
                raise GovernedRunLifecycleError(
                    "The displayed run is not owned by this browser session."
                )
            if gate is None or gate.gate_token != gate_token:
                raise GovernedRunLifecycleError(
                    "The displayed human gate is no longer current. Refresh and review "
                    "the authoritative gate before submitting another decision."
                )
            if gate_token in self._scheduled_gate_tokens:
                return False

            self._known_operation_ids.add(operation_id)
            self._scheduled_gate_tokens.add(gate_token)
            self._operation_id = operation_id
            self._operation_kind = StreamlitOperationKind.RESUME
            self._operation_gate_token = gate_token
            self._error_message = None
            response_copy = deepcopy(response)
            self._future = self._executor.submit(
                self._service.resume_run,
                run_id,
                response_copy,
                gate_token=gate_token,
            )
            return True

    def poll(self) -> StreamlitRuntimeView:
        """Settle completed work and return an immutable UI projection."""

        with self._lock:
            self._settle_finished_locked()
            if self._future is None and self._snapshot is not None:
                try:
                    self._snapshot = self._service.inspect_run(self._snapshot.run_id)
                except GovernedRunError as error:
                    self._error_message = str(error)
            return StreamlitRuntimeView(
                snapshot=self._snapshot,
                operation_id=self._operation_id,
                operation_kind=self._operation_kind,
                in_flight=self._future is not None,
                error_message=self._error_message,
            )

    def close(self, *, wait: bool = False) -> None:
        """Release the owned executor when the Streamlit session is discarded."""

        if self._owns_executor:
            executor = self._executor
            if isinstance(executor, ThreadPoolExecutor):
                executor.shutdown(wait=wait, cancel_futures=False)

    def _settle_finished_locked(self) -> None:
        future = self._future
        if future is None or not future.done():
            return

        operation_kind = self._operation_kind
        gate_token = self._operation_gate_token
        try:
            self._snapshot = future.result()
        except GovernedRunError as error:
            self._error_message = str(error)
            if operation_kind is StreamlitOperationKind.START:
                self._start_scheduled = False
            elif gate_token is not None:
                self._scheduled_gate_tokens.discard(gate_token)
        except Exception as error:
            self._error_message = (
                "The governed workflow operation failed "
                f"({type(error).__name__}). Review the local server logs and retry."
            )
            if operation_kind is StreamlitOperationKind.START:
                self._start_scheduled = False
            elif gate_token is not None:
                self._scheduled_gate_tokens.discard(gate_token)
        else:
            self._error_message = None
        finally:
            self._future = None
            self._operation_gate_token = None

    @staticmethod
    def _require_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Streamlit operation ID must be non-empty text.")
