"""Shared application coordination for one process-local governed run."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol, cast
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph

from agentic_sdlc.brownfield_baseline import (
    BrownfieldBaselineProvenance,
    PublishedProjectBaselineError,
    PublishedProjectCatalog,
    build_brownfield_baseline_provenance,
)
from agentic_sdlc.brownfield_context import (
    BrownfieldCodebaseContext,
    BrownfieldCodebaseContextError,
    build_brownfield_codebase_context,
)
from agentic_sdlc.project_export import (
    ProjectNameError,
    ProjectExportContractError,
    ProjectExporter,
    ProjectExportResult,
    normalize_project_name,
    project_export_request_from_state,
)
from agentic_sdlc.run_artifacts import (
    LiveRunArtifactBundle,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.state import ApprovalDecision, ApprovalResponse, WorkflowState
from agentic_sdlc.task_execution_progress import (
    NullTaskExecutionProgressReporter,
    TaskExecutionProgressReporter,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow
from agentic_sdlc.workspace_integration import (
    GovernedWorkspaceRuntime,
    WorkspaceIntegrationError,
)
from agentic_sdlc.workspace_runtime import (
    WorkspaceRuntimeError,
    discard_isolated_workspace,
)
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedingError,
    seed_isolated_workspace_from_approved_files,
)


class GovernedRunApplicationStatus(StrEnum):
    """Presentation-neutral lifecycle status derived from application state."""

    EXECUTING = "EXECUTING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    SAFE_STOPPED = "SAFE_STOPPED"
    FAILED = "FAILED"


class GovernedRunError(RuntimeError):
    """Base error for process-local run coordination."""


class UnknownGovernedRunError(GovernedRunError):
    """Raised when a caller references a run not owned by this service."""


class GovernedRunLifecycleError(GovernedRunError):
    """Raised when an application operation is invalid for the run lifecycle."""


class GovernedRunMode(StrEnum):
    """Application-owned source mode for one shared governed run."""

    GREENFIELD = "GREENFIELD"
    BROWNFIELD = "BROWNFIELD"


@dataclass(frozen=True, slots=True)
class GovernedRunRequest:
    """Immutable application request to start one governed workflow run."""

    command: Literal["demo", "run"]
    workflow_input: WorkflowState
    requested_project_name: str | None = None
    run_mode: GovernedRunMode = GovernedRunMode.GREENFIELD
    baseline_project_name: str | None = None

    def __post_init__(self) -> None:
        if self.command not in {"demo", "run"}:
            raise ValueError("Governed run command must be 'demo' or 'run'.")
        try:
            mode = GovernedRunMode(self.run_mode)
        except ValueError as error:
            raise ValueError(
                "Governed run mode must be GREENFIELD or BROWNFIELD."
            ) from error
        object.__setattr__(self, "run_mode", mode)
        if mode is GovernedRunMode.GREENFIELD:
            if self.baseline_project_name is not None:
                raise ValueError(
                    "Greenfield runs must not specify a published baseline."
                )
            return
        if self.baseline_project_name is None:
            raise ValueError("Brownfield runs require a published baseline name.")
        if self.requested_project_name is None:
            raise ValueError("Brownfield runs require an explicit output project name.")
        try:
            baseline_name = normalize_project_name(self.baseline_project_name)
            output_name = normalize_project_name(self.requested_project_name)
        except ProjectNameError as error:
            raise ValueError(
                "Brownfield project names must be safe logical names."
            ) from error
        if baseline_name == output_name:
            raise ValueError(
                "Brownfield output project name must differ from its baseline."
            )


@dataclass(frozen=True, slots=True)
class HumanGovernanceGate:
    """Read-only view of one authoritative LangGraph interrupt payload."""

    gate_token: str
    stage: str
    checkpoint: str
    allowed_decisions: tuple[ApprovalDecision, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GovernedRunSnapshot:
    """Read-only application projection for presentation adapters."""

    run_id: str
    application_status: GovernedRunApplicationStatus
    workflow_status: str
    human_gate: HumanGovernanceGate | None
    workflow_state: Mapping[str, Any]
    artifact_bundle: LiveRunArtifactBundle
    workflow_diagram_generated: bool
    manifest_path: Path | None
    export_result: ProjectExportResult | None
    application_error: str | None
    warnings: tuple[str, ...]

    @property
    def is_terminal(self) -> bool:
        """Return whether application coordination has reached a terminal result."""

        return self.application_status in {
            GovernedRunApplicationStatus.SUCCEEDED,
            GovernedRunApplicationStatus.SAFE_STOPPED,
            GovernedRunApplicationStatus.FAILED,
        }


class WorkflowFactory(Protocol):
    """Minimal workflow-construction seam retained for deterministic tests."""

    def __call__(
        self,
        *,
        workspace_runtime: GovernedWorkspaceRuntime,
        task_execution_progress_reporter: TaskExecutionProgressReporter,
    ) -> CompiledStateGraph: ...


RunIdFactory = Callable[[str], str]
WorkspaceRuntimeFactory = Callable[[], GovernedWorkspaceRuntime]


class WorkflowDiagramWriter(Protocol):
    """Run-owned workflow diagram generation boundary."""

    def __call__(
        self,
        output_path: Path,
        *,
        workflow: CompiledStateGraph,
    ) -> None: ...


@dataclass(slots=True)
class _GovernedRunContext:
    run_id: str
    workflow: CompiledStateGraph
    workspace_runtime: GovernedWorkspaceRuntime
    artifact_bundle: LiveRunArtifactBundle
    progress_reporter: TaskExecutionProgressReporter
    requested_project_name: str | None
    workflow_state: WorkflowState
    executing: bool = True
    terminal_finalized: bool = False
    workflow_diagram_generated: bool = False
    manifest_path: Path | None = None
    export_result: ProjectExportResult | None = None
    application_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    gate_sequence: int = 0
    gate_token: str | None = None
    lock: Lock = field(default_factory=Lock)


class GovernedRunService:
    """Coordinate run lifetime without acquiring governance authority.

    The existing workflow, nodes, domain policies, workspace controls, exit gate,
    and exporter remain authoritative. This service owns only process-local object
    lifetime and the application sequencing around those existing boundaries.
    """

    def __init__(
        self,
        *,
        repository_root: Path,
        workflow_factory: WorkflowFactory | None = None,
        workspace_runtime_factory: WorkspaceRuntimeFactory | None = None,
        run_id_factory: RunIdFactory | None = None,
        workflow_diagram_writer: WorkflowDiagramWriter | None = None,
        project_exporter: ProjectExporter | None = None,
        published_project_catalog: PublishedProjectCatalog | None = None,
    ) -> None:
        self._repository_root = Path(repository_root).resolve()
        self._workflow_factory = workflow_factory or build_workflow
        self._workspace_runtime_factory = (
            workspace_runtime_factory or GovernedWorkspaceRuntime
        )
        self._run_id_factory = run_id_factory or _default_run_id
        self._workflow_diagram_writer = (
            workflow_diagram_writer or write_workflow_diagram
        )
        self._project_exporter = project_exporter or ProjectExporter()
        self._published_project_catalog = (
            published_project_catalog
            or PublishedProjectCatalog(self._repository_root)
        )
        self._runs: dict[str, _GovernedRunContext] = {}
        self._runs_lock = Lock()

    def start_run(
        self,
        request: GovernedRunRequest,
        *,
        progress_reporter: TaskExecutionProgressReporter | None = None,
    ) -> GovernedRunSnapshot:
        """Create and advance one run to its first gate or terminal result."""

        run_id = self._run_id_factory(request.command)
        artifact_bundle = LiveRunArtifactBundle.under_repository(
            self._repository_root,
            run_id,
        )
        workspace_runtime = self._workspace_runtime_factory()
        active_reporter = progress_reporter or NullTaskExecutionProgressReporter()
        workflow = self._workflow_factory(
            workspace_runtime=workspace_runtime,
            task_execution_progress_reporter=active_reporter,
        )
        initial_state = cast(
            WorkflowState,
            {**deepcopy(request.workflow_input), "run_id": run_id},
        )
        if request.run_mode is GovernedRunMode.BROWNFIELD:
            provenance, codebase_context = self._prepare_brownfield_baseline(
                request,
                run_id=run_id,
                workspace_runtime=workspace_runtime,
            )
            initial_state["brownfield_baseline"] = cast(
                Any,
                provenance.model_dump(mode="json"),
            )
            initial_state["brownfield_codebase_context"] = cast(
                Any,
                codebase_context.model_dump(mode="json"),
            )
        context = _GovernedRunContext(
            run_id=run_id,
            workflow=workflow,
            workspace_runtime=workspace_runtime,
            artifact_bundle=artifact_bundle,
            progress_reporter=active_reporter,
            requested_project_name=request.requested_project_name,
            workflow_state=initial_state,
        )
        with self._runs_lock:
            if run_id in self._runs:
                raise GovernedRunLifecycleError(
                    f"Governed run ID already exists: {run_id}"
                )
            self._runs[run_id] = context

        self._write_workflow_diagram(context)
        try:
            state = run_workflow(
                deepcopy(initial_state),
                thread_id=run_id,
                artifact_dir=artifact_bundle.artifact_dir,
                workflow=workflow,
            )
        except Exception:
            with context.lock:
                context.executing = False
            raise
        return self._complete_advance(context, state)

    def _prepare_brownfield_baseline(
        self,
        request: GovernedRunRequest,
        *,
        run_id: str,
        workspace_runtime: GovernedWorkspaceRuntime,
    ) -> tuple[BrownfieldBaselineProvenance, BrownfieldCodebaseContext]:
        """Select and seed one published baseline before workflow authority starts."""

        baseline_name = request.baseline_project_name
        output_name = request.requested_project_name
        if baseline_name is None or output_name is None:
            raise GovernedRunLifecycleError(
                "Brownfield baseline and output identities are incomplete."
            )
        try:
            baseline = self._published_project_catalog.select(baseline_name)
            self._published_project_catalog.require_available_output(
                output_name,
                baseline_project_name=baseline.project_name,
            )
            workspace = workspace_runtime.establish_workspace_for_run(run_id)
            self._published_project_catalog.require_current_identity(baseline)
            seed_result, seeded_snapshot = (
                seed_isolated_workspace_from_approved_files(
                    workspace,
                    source_root=baseline.project_root,
                    source_root_label=f"projects/{baseline.project_name}",
                    relative_paths=tuple(
                        item.path for item in baseline.engineering_files
                    ),
                )
            )
            self._published_project_catalog.require_current_identity(baseline)
            provenance = build_brownfield_baseline_provenance(
                baseline,
                seed_result,
                seeded_snapshot,
            )
            context = build_brownfield_codebase_context(workspace, provenance)
            return provenance, context
        except (
            BrownfieldCodebaseContextError,
            PublishedProjectBaselineError,
            WorkspaceIntegrationError,
            WorkspaceRuntimeError,
            WorkspaceSeedingError,
            ValueError,
        ) as error:
            try:
                workspace = workspace_runtime.workspace_for_run(run_id)
            except WorkspaceIntegrationError:
                workspace = None
            if workspace is not None:
                try:
                    discard_isolated_workspace(workspace)
                except WorkspaceRuntimeError as cleanup_error:
                    raise GovernedRunLifecycleError(
                        "Brownfield baseline preparation and isolated-workspace "
                        "cleanup both failed."
                    ) from cleanup_error
            raise GovernedRunLifecycleError(
                f"Brownfield baseline preparation failed: {error}"
            ) from error

    def resume_run(
        self,
        run_id: str,
        decision: ApprovalResponse,
        *,
        gate_token: str,
    ) -> GovernedRunSnapshot:
        """Resume exactly one currently interrupted run with human authority."""

        context = self._context_for(run_id)
        with context.lock:
            if context.executing:
                raise GovernedRunLifecycleError(
                    f"Governed run is already executing: {run_id}"
                )
            if context.terminal_finalized:
                raise GovernedRunLifecycleError(
                    f"Governed run is already terminal: {run_id}"
                )
            human_gate = _human_gate_from_state(
                context.workflow_state,
                gate_token=context.gate_token,
            )
            if human_gate is None:
                raise GovernedRunLifecycleError(
                    f"Governed run is not awaiting human governance: {run_id}"
                )
            if gate_token != context.gate_token:
                raise GovernedRunLifecycleError(
                    "Governed run response does not match the current human gate: "
                    f"{run_id}"
                )
            if decision.get("decision") not in human_gate.allowed_decisions:
                raise GovernedRunLifecycleError(
                    "Governed run decision is not allowed by the current human gate: "
                    f"{run_id}"
                )
            feedback = decision.get("feedback")
            if not isinstance(feedback, str):
                raise GovernedRunLifecycleError(
                    f"Governed run feedback must be a string: {run_id}"
                )
            if (
                decision.get("decision") == "REQUEST_CHANGES"
                and not feedback.strip()
            ):
                raise GovernedRunLifecycleError(
                    "REQUEST_CHANGES requires non-empty human feedback: "
                    f"{run_id}"
                )
            context.executing = True

        try:
            state = resume_workflow(
                run_id,
                deepcopy(decision),
                artifact_dir=context.artifact_bundle.artifact_dir,
                workflow=context.workflow,
            )
        except Exception:
            with context.lock:
                context.executing = False
            raise
        return self._complete_advance(context, state)

    def inspect_run(self, run_id: str) -> GovernedRunSnapshot:
        """Return a defensive read-only view of one service-owned run."""

        context = self._context_for(run_id)
        with context.lock:
            return _snapshot_from_context(context)

    def _context_for(self, run_id: str) -> _GovernedRunContext:
        with self._runs_lock:
            context = self._runs.get(run_id)
        if context is None:
            raise UnknownGovernedRunError(f"Unknown governed run ID: {run_id}")
        return context

    def _write_workflow_diagram(self, context: _GovernedRunContext) -> None:
        try:
            self._workflow_diagram_writer(
                context.artifact_bundle.workflow_diagram_path,
                workflow=context.workflow,
            )
        except Exception as error:
            detail = str(error).splitlines()[0] or type(error).__name__
            context.warnings.append(f"workflow diagram was not generated: {detail}")
        else:
            context.workflow_diagram_generated = True

    def _complete_advance(
        self,
        context: _GovernedRunContext,
        state: WorkflowState,
    ) -> GovernedRunSnapshot:
        with context.lock:
            context.workflow_state = state
            if state.get("__interrupt__"):
                context.gate_sequence += 1
                context.gate_token = (
                    f"{context.run_id}:human-gate:{context.gate_sequence}"
                )
            else:
                context.gate_token = None
        try:
            with context.lock:
                self._finalize_terminal_run(context)
        finally:
            with context.lock:
                context.executing = False
        with context.lock:
            return _snapshot_from_context(context)

    def _finalize_terminal_run(self, context: _GovernedRunContext) -> None:
        state = context.workflow_state
        if state.get("__interrupt__") or context.terminal_finalized:
            return

        workflow_status = state.get("workflow_status", "unknown")
        if workflow_status in {"success", "safe_stopped"}:
            try:
                context.manifest_path = write_sdlc_artifact_manifest(
                    state,
                    context.artifact_bundle,
                )
            except (OSError, ValueError) as error:
                context.application_error = (
                    f"SDLC artifact manifest failed: {error}"
                )

        if workflow_status == "success" and context.application_error is None:
            try:
                workspace = context.workspace_runtime.workspace_for_run(context.run_id)
                export_request = project_export_request_from_state(
                    state,
                    workspace=workspace,
                    artifact_bundle=context.artifact_bundle,
                    export_root=self._repository_root / "projects",
                    requested_project_name=context.requested_project_name,
                )
            except (ProjectExportContractError, WorkspaceIntegrationError) as error:
                context.application_error = f"Project export failed: {error}"
            else:
                context.export_result = self._project_exporter.export(export_request)
                if not context.export_result.succeeded:
                    context.application_error = (
                        "Project export failed: "
                        f"{context.export_result.failure_reason}"
                    )

        context.terminal_finalized = True


def write_workflow_diagram(
    output_path: Path,
    *,
    workflow: CompiledStateGraph,
) -> None:
    """Render one exact compiled workflow graph into its run-owned bundle."""

    png_bytes = workflow.get_graph().draw_mermaid_png()
    if not png_bytes:
        raise ValueError("The workflow diagram renderer returned no PNG data.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png_bytes)


def _default_run_id(command: str) -> str:
    return f"{command}-{uuid4().hex}"


def _snapshot_from_context(context: _GovernedRunContext) -> GovernedRunSnapshot:
    state = context.workflow_state
    gate = (
        None
        if context.executing
        else _human_gate_from_state(state, gate_token=context.gate_token)
    )
    workflow_status = str(state.get("workflow_status", "pending"))
    if context.executing:
        application_status = GovernedRunApplicationStatus.EXECUTING
    elif gate is not None:
        application_status = GovernedRunApplicationStatus.AWAITING_HUMAN
    elif workflow_status == "safe_stopped" and context.application_error is None:
        application_status = GovernedRunApplicationStatus.SAFE_STOPPED
    elif (
        workflow_status == "success"
        and context.application_error is None
        and context.export_result is not None
        and context.export_result.succeeded
    ):
        application_status = GovernedRunApplicationStatus.SUCCEEDED
    else:
        application_status = GovernedRunApplicationStatus.FAILED

    return GovernedRunSnapshot(
        run_id=context.run_id,
        application_status=application_status,
        workflow_status=workflow_status,
        human_gate=gate,
        workflow_state=cast(Mapping[str, Any], _read_only_copy(state)),
        artifact_bundle=context.artifact_bundle,
        workflow_diagram_generated=context.workflow_diagram_generated,
        manifest_path=context.manifest_path,
        export_result=context.export_result,
        application_error=context.application_error,
        warnings=tuple(context.warnings),
    )


def _human_gate_from_state(
    state: WorkflowState,
    *,
    gate_token: str | None,
) -> HumanGovernanceGate | None:
    interrupt_events = state.get("__interrupt__")
    if not interrupt_events:
        return None
    if gate_token is None:
        raise GovernedRunLifecycleError(
            "Interrupted governed run has no application gate token."
        )
    payload = interrupt_events[0].value
    allowed = tuple(cast(ApprovalDecision, value) for value in payload["allowed_decisions"])
    return HumanGovernanceGate(
        gate_token=gate_token,
        stage=str(payload["stage"]),
        checkpoint=str(payload["checkpoint"]),
        allowed_decisions=allowed,
        payload=cast(Mapping[str, Any], _read_only_copy(payload)),
    )


def _read_only_copy(value: Any) -> Any:
    """Recursively freeze built-in containers after severing source references."""

    if isinstance(value, dict):
        return MappingProxyType(
            {key: _read_only_copy(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_read_only_copy(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_read_only_copy(item) for item in value)
    if isinstance(value, set):
        return frozenset(_read_only_copy(item) for item in value)
    return deepcopy(value)
