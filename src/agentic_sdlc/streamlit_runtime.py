"""Session-owned background coordination for the Streamlit presentation adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from agentic_sdlc.application import (
    EligibleBrownfieldProject,
    GovernedRunError,
    GovernedRunLifecycleError,
    GovernedRunMode,
    GovernedRunRequest,
    GovernedRunService,
    GovernedRunSnapshot,
)
from agentic_sdlc.clarification_draft import (
    ClarificationDrafter,
    ClarificationDraftError,
    ClarificationDraftRequest,
    ClarificationDraftResult,
    clarification_draft_context_identity,
)
from agentic_sdlc.project_export import normalize_project_name
from agentic_sdlc.requirement_submission import (
    deterministic_project_name,
    resolve_inline_requirement,
)
from agentic_sdlc.state import ApprovalResponse, workflow_input_from_submission
from agentic_sdlc.streamlit_execution_progress import (
    StreamlitExecutionProgressCollector,
    StreamlitExecutionProgressView,
)
from agentic_sdlc.task_execution_progress import TaskExecutionProgressReporter


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
    operation_elapsed_seconds: float | None = None
    execution_progress: StreamlitExecutionProgressView | None = None


@dataclass(frozen=True, slots=True)
class ClarificationDraftRuntimeView:
    """Immutable state for one presentation-only clarification generation."""

    generation_id: str | None
    context_identity: str | None
    in_flight: bool
    result: ClarificationDraftResult | None
    error_message: str | None


class GovernedRunLifecycle(Protocol):
    """Public GovernedRunService surface used by the session runtime."""

    def list_eligible_brownfield_projects(
        self,
    ) -> tuple[EligibleBrownfieldProject, ...]: ...

    def start_run(
        self,
        request: GovernedRunRequest,
        *,
        progress_reporter: TaskExecutionProgressReporter | None = None,
    ) -> GovernedRunSnapshot: ...

    def inspect_run(self, run_id: str) -> GovernedRunSnapshot: ...

    def resume_run(
        self,
        run_id: str,
        decision: ApprovalResponse,
        *,
        gate_token: str,
    ) -> GovernedRunSnapshot: ...


class ClarificationDraftEventRecorder(Protocol):
    """Presentation-neutral observation seam with no governed-run authority."""

    def record_clarification_draft_requested(
        self,
        request: ClarificationDraftRequest,
        *,
        generation_id: str,
        context_identity: str,
        model_name: str,
    ) -> bool: ...

    def record_clarification_draft_generated(
        self,
        request: ClarificationDraftRequest,
        result: ClarificationDraftResult,
        *,
        generation_id: str,
        context_identity: str,
        model_name: str,
    ) -> bool: ...

class BackgroundExecutor(Protocol):
    """Small executor seam for deterministic runtime tests."""

    def submit(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]: ...


class ClarificationDraftBackgroundRuntime:
    """Run optional clarification assistance off-thread without workflow authority."""

    def __init__(
        self,
        *,
        executor: BackgroundExecutor | None = None,
        event_recorder: ClarificationDraftEventRecorder | None = None,
    ) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agentic-sdlc-clarification",
        )
        self._owns_executor = executor is None
        self._event_recorder = event_recorder
        self._lock = Lock()
        self._future: Future[Any] | None = None
        self._generation_id: str | None = None
        self._context_identity: str | None = None
        self._known_generation_ids: set[str] = set()
        self._result: ClarificationDraftResult | None = None
        self._error_message: str | None = None
        self._request: ClarificationDraftRequest | None = None
        self._model_name: str | None = None

    def schedule(
        self,
        generation_id: str,
        context_identity: str,
        request: ClarificationDraftRequest,
        drafter: ClarificationDrafter,
    ) -> bool:
        """Schedule one explicit draft request without invoking governed lifecycle."""

        self._require_identity(generation_id, "generation")
        self._require_identity(context_identity, "context")
        if clarification_draft_context_identity(request) != context_identity:
            raise ValueError(
                "Clarification draft context identity does not match the request."
            )
        with self._lock:
            self._settle_finished_locked()
            if generation_id in self._known_generation_ids:
                return False
            if self._future is not None:
                return False
            if self._event_recorder is not None:
                try:
                    current = self._event_recorder.record_clarification_draft_requested(
                        request,
                        generation_id=generation_id,
                        context_identity=context_identity,
                        model_name=drafter.model_name,
                    )
                except Exception:
                    # Observation cannot acquire veto authority over optional
                    # clarification assistance. The production recorder retains
                    # storage failures as run warnings.
                    current = True
                if not current:
                    return False
            self._known_generation_ids.add(generation_id)
            self._generation_id = generation_id
            self._context_identity = context_identity
            self._result = None
            self._error_message = None
            self._request = request.model_copy(deep=True)
            self._model_name = drafter.model_name
            self._future = self._executor.submit(
                drafter.draft,
                request.model_copy(deep=True),
            )
            return True

    def poll(self, expected_context_identity: str) -> ClarificationDraftRuntimeView:
        """Return assistance state only when it belongs to the current gate context."""

        self._require_identity(expected_context_identity, "expected context")
        with self._lock:
            self._settle_finished_locked()
            if self._context_identity != expected_context_identity:
                in_flight = self._future is not None
                if not in_flight:
                    self._clear_completed_locked()
                return ClarificationDraftRuntimeView(
                    generation_id=self._generation_id,
                    context_identity=self._context_identity,
                    in_flight=in_flight,
                    result=None,
                    error_message=None,
                )
            return ClarificationDraftRuntimeView(
                generation_id=self._generation_id,
                context_identity=self._context_identity,
                in_flight=self._future is not None,
                result=self._result,
                error_message=self._error_message,
            )

    def close(self, *, wait: bool = False) -> None:
        """Release the session-owned assistance executor."""

        if self._owns_executor and isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=wait, cancel_futures=False)

    def _settle_finished_locked(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        try:
            result = future.result()
            if not isinstance(result, ClarificationDraftResult):
                raise ClarificationDraftError(
                    "The clarification drafter returned an invalid result."
                )
        except ClarificationDraftError as error:
            self._result = None
            self._error_message = str(error)
        except Exception as error:
            self._result = None
            self._error_message = (
                "Clarification drafting failed "
                f"({type(error).__name__}). Review the local server logs and retry."
            )
        else:
            retain_result = True
            if (
                self._event_recorder is not None
                and self._request is not None
                and self._generation_id is not None
                and self._context_identity is not None
                and self._model_name is not None
            ):
                try:
                    retain_result = (
                        self._event_recorder.record_clarification_draft_generated(
                            self._request,
                            result,
                            generation_id=self._generation_id,
                            context_identity=self._context_identity,
                            model_name=self._model_name,
                        )
                    )
                except Exception:
                    # As above, audit observation cannot invalidate a valid draft.
                    retain_result = True
            self._result = result if retain_result else None
            self._error_message = None
        finally:
            self._future = None
            self._request = None
            self._model_name = None

    def _clear_completed_locked(self) -> None:
        self._generation_id = None
        self._context_identity = None
        self._result = None
        self._error_message = None
        self._request = None
        self._model_name = None

    @staticmethod
    def _require_identity(value: str, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"Clarification draft {label} ID must be non-empty text.")


def governed_run_request_from_inline_requirement(
    requirement_text: str,
    project_name: str,
    *,
    run_mode: GovernedRunMode = GovernedRunMode.GREENFIELD,
    baseline_project_name: str | None = None,
) -> GovernedRunRequest:
    """Resolve one GUI submission through the authoritative input boundary."""

    submission = resolve_inline_requirement(requirement_text)
    mode = GovernedRunMode(run_mode)
    requested_project_name = project_name if project_name != "" else None
    if mode is GovernedRunMode.BROWNFIELD and requested_project_name is None:
        raise ValueError("Brownfield runs require a new output project name.")
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
        run_mode=mode,
        baseline_project_name=baseline_project_name,
    )


class StreamlitRunRuntime:
    """Own one service and serialize its blocking lifecycle calls off-thread."""

    def __init__(
        self,
        service: GovernedRunLifecycle,
        *,
        executor: BackgroundExecutor | None = None,
        clarification_executor: BackgroundExecutor | None = None,
        clarification_event_recorder: ClarificationDraftEventRecorder | None = None,
        progress_collector: StreamlitExecutionProgressCollector | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._service = service
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agentic-sdlc-streamlit",
        )
        self._owns_executor = executor is None
        self._clarification_drafts = ClarificationDraftBackgroundRuntime(
            executor=clarification_executor,
            event_recorder=clarification_event_recorder,
        )
        self._progress_collector = (
            progress_collector or StreamlitExecutionProgressCollector()
        )
        self._clock = clock
        self._lock = Lock()
        self._future: Future[GovernedRunSnapshot] | None = None
        self._operation_started_at: float | None = None
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

        service = GovernedRunService(repository_root=repository_root)
        return cls(service, clarification_event_recorder=service)

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
            self._progress_collector.reset()
            self._future = self._executor.submit(
                self._service.start_run,
                request,
                progress_reporter=self._progress_collector,
            )
            self._operation_started_at = self._clock()
            return True

    def list_eligible_brownfield_projects(
        self,
    ) -> tuple[EligibleBrownfieldProject, ...]:
        """Delegate verified baseline discovery to the shared application service."""

        return self._service.list_eligible_brownfield_projects()

    def schedule_clarification_draft(
        self,
        generation_id: str,
        context_identity: str,
        request: ClarificationDraftRequest,
        drafter: ClarificationDrafter,
    ) -> bool:
        """Schedule optional assistance on its non-governed background lane."""

        return self._clarification_drafts.schedule(
            generation_id,
            context_identity,
            request,
            drafter,
        )

    def poll_clarification_draft(
        self,
        context_identity: str,
    ) -> ClarificationDraftRuntimeView:
        """Poll assistance bound to the exact current human-gate context."""

        return self._clarification_drafts.poll(context_identity)

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

            response_copy = deepcopy(response)
            if gate.stage == "task_graph_review" and (
                response_copy.get("decision") == "APPROVE"
            ):
                graph = gate.payload.get("candidate_task_graph")
                semantics = gate.payload.get("graph_semantics")
                if not isinstance(graph, Mapping) or not isinstance(
                    semantics,
                    Mapping,
                ):
                    raise GovernedRunLifecycleError(
                        "The authoritative TaskGraph gate has no renderable execution "
                        "telemetry context."
                    )
                if not self._progress_collector.begin_execution(
                    run_id=run_id,
                    operation_id=operation_id,
                    candidate_task_graph=graph,
                    graph_semantics=semantics,
                ):
                    raise GovernedRunLifecycleError(
                        "Execution telemetry is not bound to the displayed governed "
                        "run."
                    )
            self._known_operation_ids.add(operation_id)
            self._scheduled_gate_tokens.add(gate_token)
            self._operation_id = operation_id
            self._operation_kind = StreamlitOperationKind.RESUME
            self._operation_gate_token = gate_token
            self._error_message = None
            self._future = self._executor.submit(
                self._service.resume_run,
                run_id,
                response_copy,
                gate_token=gate_token,
            )
            self._operation_started_at = self._clock()
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
            operation_elapsed_seconds = None
            if self._future is not None and self._operation_started_at is not None:
                operation_elapsed_seconds = max(
                    0.0,
                    self._clock() - self._operation_started_at,
                )
            return StreamlitRuntimeView(
                snapshot=self._snapshot,
                operation_id=self._operation_id,
                operation_kind=self._operation_kind,
                in_flight=self._future is not None,
                error_message=self._error_message,
                operation_elapsed_seconds=operation_elapsed_seconds,
                execution_progress=self._progress_collector.snapshot(
                    run_id=(
                        self._snapshot.run_id
                        if self._snapshot is not None
                        else None
                    )
                ),
            )

    def close(self, *, wait: bool = False) -> None:
        """Release the owned executor when the Streamlit session is discarded."""

        self._clarification_drafts.close(wait=wait)
        if self._owns_executor:
            executor = self._executor
            if isinstance(executor, ThreadPoolExecutor):
                executor.shutdown(wait=wait, cancel_futures=False)

    def _settle_finished_locked(self) -> None:
        future = self._future
        if future is None or not future.done():
            return

        operation_kind = self._operation_kind
        operation_id = self._operation_id
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
            if (
                operation_kind is StreamlitOperationKind.START
                and self._snapshot is not None
            ):
                self._progress_collector.attach_run(self._snapshot.run_id)
            elif (
                operation_kind is StreamlitOperationKind.RESUME
                and operation_id is not None
                and self._snapshot is not None
            ):
                self._progress_collector.finish_execution(
                    run_id=self._snapshot.run_id,
                    operation_id=operation_id,
                )
            self._future = None
            self._operation_started_at = None
            self._operation_gate_token = None

    @staticmethod
    def _require_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Streamlit operation ID must be non-empty text.")
