"""Thin Streamlit presentation adapter for one governed workflow session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from agentic_sdlc.application import (
    EligibleBrownfieldProject,
    GovernedRunApplicationStatus,
    GovernedRunLifecycleError,
    GovernedRunMode,
    GovernedRunSnapshot,
    HumanGovernanceGate,
)
from agentic_sdlc.clarification_draft import (
    ClarificationDrafter,
    ClarificationDraftRequest,
    OpenAIClarificationDrafter,
    clarification_draft_context_identity,
)
from agentic_sdlc.project_export import ProjectNameError
from agentic_sdlc.requirement_analysis import (
    RequirementAnalysis,
    RequirementPlanningReadiness,
)
from agentic_sdlc.requirement_submission import RequirementSubmissionError
from agentic_sdlc.sdlc_artifact_index import (
    SDLCArtifactIndexError,
    load_sdlc_artifact_index,
)
from agentic_sdlc.state import ApprovalDecision, ApprovalResponse
from agentic_sdlc.streamlit_execution_progress import (
    StreamlitExecutionProgressView,
    StreamlitTaskExecutionProgress,
)
from agentic_sdlc.streamlit_runtime import (
    StreamlitOperationKind,
    StreamlitRunRuntime,
    StreamlitRuntimeView,
    governed_run_request_from_inline_requirement,
)
from agentic_sdlc.task_graph_presentation import (
    TaskGraphPresentationError,
    task_graph_mermaid,
)
from agentic_sdlc.traceability import (
    RequirementTraceabilityProjection,
    TraceabilityProjectionError,
    TraceabilityRow,
    TraceabilityStatus,
    build_requirement_traceability,
    traceability_row_evaluator_reason,
    traceability_status_explanation,
    traceability_status_heading,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENT_REVIEW_STAGE = "requirement_analysis_review"
TASK_GRAPH_REVIEW_STAGE = "task_graph_review"

_UI_PHASE_KEY = "agentic_sdlc_ui_phase"
_RUN_ID_KEY = "agentic_sdlc_current_run_id"
_OPERATION_ID_KEY = "agentic_sdlc_operation_id"
_CLARIFICATION_DRAFT_CONTEXT_KEY = "agentic_sdlc_clarification_draft_context"
_CLARIFICATION_DRAFT_TEXT_KEY = "agentic_sdlc_clarification_draft_text"
_CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY = (
    "agentic_sdlc_clarification_draft_applied_generation"
)
_ACTIVE_RUN_MODE_KEY = "agentic_sdlc_active_run_mode"
_ACTIVE_BASELINE_PROJECT_KEY = "agentic_sdlc_active_baseline_project"
_ACTIVE_OUTPUT_PROJECT_KEY = "agentic_sdlc_active_output_project"
_ENTRY_MODE_KEY = "agentic_sdlc_entry_mode"
_GREENFIELD_MODE_LABEL = "Build a new project"
_BROWNFIELD_MODE_LABEL = "Change an existing project"
_RUN_PRESENTATION_WIDGET_PREFIXES = (
    "clarification_draft_",
    "requirement_decision_",
    "task_graph_decision_",
)
_BROWNFIELD_IMPACT_CATEGORIES = (
    ("impacted_modules", "Modules / files"),
    ("impacted_services", "Services / components"),
    ("impacted_apis", "APIs / interfaces"),
    ("impacted_state", "State / data structures"),
    ("impacted_flows", "Data / control flows"),
    ("impacted_tests", "Tests"),
    ("impacted_documentation", "Documentation"),
    ("architectural_implications", "Architectural implications"),
    ("preserved_behaviors", "Preserved / backward-compatible behavior"),
)


@st.cache_resource(
    scope="session",
    show_spinner=False,
    on_release=lambda runtime: runtime.close(),
)
def _session_runtime() -> StreamlitRunRuntime:
    """Retain service, workflow capabilities, executor, and futures per session."""

    return StreamlitRunRuntime.for_repository(REPOSITORY_ROOT)


def main() -> None:
    """Configure and render the local Streamlit application."""

    st.set_page_config(
        page_title="Agentic SDLC Orchestrator",
        page_icon="⚙️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    render_app(_session_runtime())


def render_app(
    runtime: StreamlitRunRuntime,
    *,
    clarification_drafter: ClarificationDrafter | None = None,
) -> None:
    """Render one UI pass from the runtime's immutable polling projection."""

    st.title("Agentic SDLC Orchestrator")
    st.write(
        "Build a new project or propose a governed change to an eligible published "
        "project. The workflow analyzes the requirement before asking for your "
        "approval."
    )

    view = runtime.poll()
    if view.error_message:
        st.error(view.error_message)
        if (
            st.session_state.get(_ACTIVE_RUN_MODE_KEY)
            == GovernedRunMode.BROWNFIELD.value
            and view.operation_kind is StreamlitOperationKind.START
            and view.snapshot is None
        ):
            st.caption(
                "Brownfield setup failed closed. No partial codebase context was "
                "accepted and no project was published."
            )

    if view.snapshot is not None and not _bind_active_run(view.snapshot):
        return

    if view.in_flight:
        _render_polling_fragment(
            runtime,
            view,
            str(st.session_state.get(_UI_PHASE_KEY, "executing")),
        )
        return

    snapshot = view.snapshot
    if snapshot is None:
        _clear_run_presentation_state()
        st.session_state[_UI_PHASE_KEY] = "requirement_entry"
        _render_requirement_entry(runtime)
        return

    _render_snapshot(
        runtime,
        snapshot,
        execution_progress=_run_bound_execution_progress(
            view,
            expected_run_id=snapshot.run_id,
        ),
        clarification_drafter=clarification_drafter,
    )


@st.fragment(run_every=0.5)
def _render_polling_fragment(
    runtime: StreamlitRunRuntime,
    initial_view: StreamlitRuntimeView,
    ui_phase: str,
) -> None:
    """Poll background work without allowing worker threads to call Streamlit."""

    view = runtime.poll()
    if not view.in_flight:
        st.rerun()

    operation_kind = view.operation_kind or initial_view.operation_kind
    active_snapshot = view.snapshot or initial_view.snapshot
    if active_snapshot is not None:
        _render_brownfield_baseline_summary(active_snapshot)
    else:
        _render_active_brownfield_intent()
    if ui_phase == "task_graph_execution":
        active_run_id = st.session_state.get(_RUN_ID_KEY)
        _render_engineering_execution(
            _run_bound_execution_progress(
                view,
                expected_run_id=(
                    active_run_id if isinstance(active_run_id, str) else None
                ),
            )
        )
        st.caption(
            "Structured progress is observed from the governed execution engine; "
            "this dashboard does not schedule tasks or retries."
        )
        return
    _render_governed_operation(
        operation_kind=operation_kind,
        ui_phase=ui_phase,
        elapsed_seconds=view.operation_elapsed_seconds,
    )


@st.fragment(run_every=0.5)
def _render_clarification_polling_fragment(
    runtime: StreamlitRunRuntime,
    context_identity: str,
) -> None:
    """Poll optional draft work while the authoritative human gate stays visible."""

    draft_view = runtime.poll_clarification_draft(context_identity)
    if not draft_view.in_flight:
        st.rerun()
    st.info("Drafting clarification response...")
    st.caption(
        "This is optional presentation assistance; the governed workflow remains "
        "at the current human gate."
    )


def _render_governed_operation(
    *,
    operation_kind: StreamlitOperationKind | None,
    ui_phase: str,
    elapsed_seconds: float | None,
) -> None:
    """Render truthful presentation-only context for non-execution work."""

    actor: str | None
    if operation_kind is StreamlitOperationKind.START:
        title = "Analyzing Requirement"
        actor = "Requirement Analysis Agent"
        description = (
            "Normalizing the submitted requirement and producing the first "
            "governed Requirement Analysis."
        )
    elif ui_phase == "requirement_analysis_revision":
        title = "Re-analyzing Requirement"
        actor = "Requirement Analysis Agent"
        description = (
            "Applying the human clarification feedback and producing a revised "
            "authoritative Requirement Analysis."
        )
    elif ui_phase == "task_planning":
        title = "Planning Engineering Work"
        actor = "Task Planning Agent"
        description = (
            "Generating and deterministically validating the canonical TaskGraph."
        )
    elif ui_phase == "task_graph_revision":
        title = "Revising TaskGraph"
        actor = "Task Planning Agent"
        description = (
            "Applying the human TaskGraph review feedback and producing a revised "
            "canonical TaskGraph."
        )
    else:
        title = "Advancing Governed Workflow"
        actor = None
        description = (
            "The governed lifecycle is running in the session-owned background "
            "worker."
        )

    st.header(title)
    with st.container(border=True):
        if actor is not None:
            st.subheader(actor)
        st.write(description)
        st.metric("Elapsed", _format_elapsed(elapsed_seconds))
    st.caption(
        "The governed lifecycle is running in one session-owned background worker. "
        "Submission controls remain disabled until it reaches the next gate."
    )


def _render_requirement_entry(runtime: StreamlitRunRuntime) -> None:
    mode_label = st.radio(
        "What do you want to do?",
        (_GREENFIELD_MODE_LABEL, _BROWNFIELD_MODE_LABEL),
        key=_ENTRY_MODE_KEY,
        horizontal=True,
    )
    run_mode = (
        GovernedRunMode.BROWNFIELD
        if mode_label == _BROWNFIELD_MODE_LABEL
        else GovernedRunMode.GREENFIELD
    )
    eligible_projects: tuple[EligibleBrownfieldProject, ...] = ()
    baseline_listing_error: str | None = None
    if run_mode is GovernedRunMode.BROWNFIELD:
        try:
            eligible_projects = runtime.list_eligible_brownfield_projects()
        except GovernedRunLifecycleError as error:
            baseline_listing_error = str(error)

    with st.container(border=True):
        if run_mode is GovernedRunMode.BROWNFIELD:
            st.subheader("Describe the change to an existing project")
            st.caption(
                "Brownfield runs analyze a previously published project, preserve "
                "the original baseline, and publish successful changes as a new "
                "project."
            )
        else:
            st.subheader("Describe the software you want to build")
        with st.form("requirement_entry_form"):
            selected_baseline: str | None = None
            if run_mode is GovernedRunMode.BROWNFIELD:
                if baseline_listing_error is not None:
                    st.error(baseline_listing_error)
                if not eligible_projects:
                    st.info(
                        "No eligible published projects are currently available "
                        "for brownfield changes."
                    )
                selected_baseline = st.selectbox(
                    "Existing project",
                    tuple(item.project_name for item in eligible_projects),
                    index=0 if eligible_projects else None,
                    disabled=not eligible_projects,
                    help=(
                        "Only successfully governed publications verified by the "
                        "application are eligible."
                    ),
                )
                selected_metadata = next(
                    (
                        item
                        for item in eligible_projects
                        if item.project_name == selected_baseline
                    ),
                    None,
                )
                if selected_metadata is not None:
                    st.caption(
                        f"Originating run: {selected_metadata.originating_run_id} · "
                        "Authoritative engineering files: "
                        f"{selected_metadata.engineering_file_count}"
                    )
            requirement_text = st.text_area(
                (
                    "Describe the change you want to make"
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "Software requirement"
                ),
                height=240,
                placeholder=(
                    "Example: Add optional expiration times while preserving "
                    "existing behavior for records without expiration."
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "Example: Build a task manager that can add, list, "
                    "prioritize, and complete tasks."
                ),
                help="Enter the complete natural-language requirement inline.",
            )
            project_name = st.text_input(
                (
                    "New project name"
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "Project name (optional)"
                ),
                placeholder=(
                    "enhanced-project"
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "task-manager"
                ),
                help=(
                    "Required. The original project remains unchanged; a successful "
                    "run publishes this distinct new project."
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "Leave blank to use the existing deterministic project-name "
                    "behavior."
                ),
            )
            submitted = st.form_submit_button(
                (
                    "Analyze Change"
                    if run_mode is GovernedRunMode.BROWNFIELD
                    else "Analyze Requirement"
                ),
                type="primary",
                use_container_width=True,
                disabled=(
                    run_mode is GovernedRunMode.BROWNFIELD
                    and not eligible_projects
                ),
            )

    if not submitted:
        return

    try:
        request = governed_run_request_from_inline_requirement(
            requirement_text,
            project_name,
            run_mode=run_mode,
            baseline_project_name=selected_baseline,
        )
    except RequirementSubmissionError as error:
        st.error(str(error))
        return
    except ProjectNameError as error:
        st.error(f"Invalid project name: {error}")
        return
    except ValueError as error:
        st.error(str(error))
        return

    operation_id = uuid4().hex
    try:
        scheduled = runtime.schedule_start(operation_id, request)
    except GovernedRunLifecycleError as error:
        st.error(str(error))
        return
    if not scheduled:
        st.info("Requirement analysis is already scheduled for this session.")
        return

    _clear_run_presentation_state()
    st.session_state[_ACTIVE_RUN_MODE_KEY] = request.run_mode.value
    if request.baseline_project_name is not None:
        st.session_state[_ACTIVE_BASELINE_PROJECT_KEY] = (
            request.baseline_project_name
        )
    output_project = request.workflow_input.get("project_name")
    if isinstance(output_project, str):
        st.session_state[_ACTIVE_OUTPUT_PROJECT_KEY] = output_project
    st.session_state[_OPERATION_ID_KEY] = operation_id
    st.session_state[_UI_PHASE_KEY] = "requirement_analysis"
    st.rerun()


def _render_snapshot(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    *,
    execution_progress: StreamlitExecutionProgressView | None,
    clarification_drafter: ClarificationDrafter | None,
) -> None:
    for warning in snapshot.warnings:
        st.warning(warning)
    _render_brownfield_baseline_summary(snapshot)

    gate = snapshot.human_gate
    if (
        snapshot.application_status is GovernedRunApplicationStatus.AWAITING_HUMAN
        and gate is not None
    ):
        if gate.stage == REQUIREMENT_REVIEW_STAGE:
            st.session_state[_UI_PHASE_KEY] = "requirement_analysis_review"
            _render_requirement_analysis_review(
                runtime,
                snapshot,
                gate,
                clarification_drafter=clarification_drafter,
            )
            return
        if gate.stage == TASK_GRAPH_REVIEW_STAGE:
            st.session_state[_UI_PHASE_KEY] = "task_graph_review"
            _render_task_graph_review(runtime, snapshot, gate)
            return
        st.session_state[_UI_PHASE_KEY] = "unsupported_gate"
        st.warning(
            f"The governed workflow is waiting at the read-only stage "
            f"'{gate.stage}'. This GUI slice does not provide controls for that gate."
        )
        _render_run_context(snapshot, gate)
        return

    if snapshot.application_status is GovernedRunApplicationStatus.SAFE_STOPPED:
        st.session_state[_UI_PHASE_KEY] = "safe_stopped"
        st.warning("The governed workflow stopped safely.")
        safe_stop_reason = cast(
            str,
            snapshot.workflow_state.get(
                "safe_stop_reason",
                "No reason recorded.",
            ),
        )
        st.write(safe_stop_reason)
        _render_terminal_traceability(snapshot)
        _render_run_context(snapshot, None)
        if execution_progress is not None:
            _render_engineering_execution(execution_progress)
        _render_terminal_artifact_index(snapshot)
        return

    if snapshot.application_status is GovernedRunApplicationStatus.SUCCEEDED:
        st.session_state[_UI_PHASE_KEY] = "succeeded"
        st.success("The governed workflow completed successfully.")
        _render_published_project(snapshot)
        _render_terminal_traceability(snapshot)
        _render_run_context(snapshot, None)
        if execution_progress is not None:
            _render_engineering_execution(execution_progress)
        _render_terminal_artifact_index(snapshot)
        return

    st.session_state[_UI_PHASE_KEY] = "failed"
    st.error("The governed workflow did not reach a reviewable or successful state.")
    if snapshot.application_error:
        st.write(snapshot.application_error)
    for error in _string_sequence(snapshot.workflow_state.get("errors", ())):
        st.write(f"- {error}")
    _render_terminal_traceability(snapshot)
    _render_run_context(snapshot, None)
    if execution_progress is not None:
        _render_engineering_execution(execution_progress)


def _bind_active_run(snapshot: GovernedRunSnapshot) -> bool:
    """Accept snapshots only for the explicitly active presentation run."""

    active_run_id = st.session_state.get(_RUN_ID_KEY)
    if active_run_id is None:
        st.session_state[_RUN_ID_KEY] = snapshot.run_id
        return True
    if active_run_id == snapshot.run_id:
        return True
    _clear_run_presentation_state(preserve_run_id=True)
    st.error(
        "Run presentation identity mismatch. Stale run state was not rendered; "
        f"the active run remains {active_run_id}."
    )
    return False


def _run_bound_execution_progress(
    view: StreamlitRuntimeView,
    *,
    expected_run_id: str | None,
) -> StreamlitExecutionProgressView | None:
    """Return telemetry only when run, snapshot, and operation identities agree."""

    progress = view.execution_progress
    snapshot = view.snapshot
    active_run_id = st.session_state.get(_RUN_ID_KEY)
    operation_id = st.session_state.get(_OPERATION_ID_KEY)
    if (
        progress is None
        or expected_run_id is None
        or active_run_id != expected_run_id
        or snapshot is None
        or snapshot.run_id != expected_run_id
        or progress.run_id != expected_run_id
        or not isinstance(operation_id, str)
        or view.operation_id != operation_id
        or progress.operation_id != operation_id
    ):
        return None
    return progress


def _clear_run_presentation_state(*, preserve_run_id: bool = False) -> None:
    """Clear only ephemeral state belonging to a prior governed run."""

    keys = (
        _UI_PHASE_KEY,
        _OPERATION_ID_KEY,
        _CLARIFICATION_DRAFT_CONTEXT_KEY,
        _CLARIFICATION_DRAFT_TEXT_KEY,
        _CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY,
        _ACTIVE_RUN_MODE_KEY,
        _ACTIVE_BASELINE_PROJECT_KEY,
        _ACTIVE_OUTPUT_PROJECT_KEY,
    )
    for key in keys:
        st.session_state.pop(key, None)
    if not preserve_run_id:
        st.session_state.pop(_RUN_ID_KEY, None)
    for key in tuple(st.session_state):
        if isinstance(key, str) and any(
            key.startswith(prefix) for prefix in _RUN_PRESENTATION_WIDGET_PREFIXES
        ):
            st.session_state.pop(key, None)


def _render_active_brownfield_intent() -> None:
    """Show only the presentation-safe intent while the first gate is pending."""

    if (
        st.session_state.get(_ACTIVE_RUN_MODE_KEY)
        != GovernedRunMode.BROWNFIELD.value
    ):
        return
    baseline_name = st.session_state.get(_ACTIVE_BASELINE_PROJECT_KEY)
    output_name = st.session_state.get(_ACTIVE_OUTPUT_PROJECT_KEY)
    if isinstance(baseline_name, str) and isinstance(output_name, str):
        st.info(
            f"Brownfield run · Baseline: {baseline_name} · New project: "
            f"{output_name}"
        )


def _render_brownfield_baseline_summary(snapshot: GovernedRunSnapshot) -> None:
    """Render immutable baseline lineage already established by the application."""

    baseline = _mapping(snapshot.workflow_state.get("brownfield_baseline"))
    if not baseline:
        return
    context = _mapping(snapshot.workflow_state.get("brownfield_codebase_context"))
    baseline_name = str(baseline.get("selected_project_name", "unknown"))
    output_name = _snapshot_output_project_name(snapshot)
    engineering_files = _mapping_sequence(baseline.get("engineering_files", ()))
    st.info(
        f"Brownfield run · Baseline: {baseline_name} · New project: "
        f"{output_name} · Authoritative engineering files: "
        f"{len(engineering_files)}"
    )
    with st.expander("Baseline provenance"):
        st.text(f"Originating governed run: {baseline.get('originating_run_id', 'unknown')}")
        st.text(f"Source snapshot: {baseline.get('source_snapshot_id', 'unknown')}")
        st.text(f"Baseline ID: {baseline.get('baseline_id', 'unknown')}")
        st.text(f"Codebase context ID: {context.get('context_id', 'unknown')}")
        st.caption(
            "The application verified and seeded this published engineering "
            "projection into the isolated governed workspace."
        )


def _snapshot_output_project_name(snapshot: GovernedRunSnapshot) -> str:
    export_result = snapshot.export_result
    if export_result is not None and export_result.succeeded:
        return export_result.project_name
    project_name = snapshot.workflow_state.get("project_name")
    return project_name if isinstance(project_name, str) else "unknown"


def _render_published_project(snapshot: GovernedRunSnapshot) -> None:
    """Render only the verified destination returned by application coordination."""

    export_result = snapshot.export_result
    if (
        export_result is None
        or not export_result.succeeded
        or export_result.destination_directory is None
    ):
        st.error(
            "The workflow reports success, but no verified published-project "
            "destination is available."
        )
        return
    destination = export_result.destination_directory
    try:
        display_destination = destination.relative_to(REPOSITORY_ROOT)
    except ValueError:
        display_destination = destination
    st.success(f"Published project: {display_destination}")
    st.caption(f"Authoritative run ID: {snapshot.run_id}")
    baseline = _mapping(snapshot.workflow_state.get("brownfield_baseline"))
    baseline_name = baseline.get("selected_project_name")
    if isinstance(baseline_name, str) and baseline_name:
        st.info(
            f"Baseline project {baseline_name} was preserved. The governed result "
            f"was published as the new project {export_result.project_name}."
        )


def _render_terminal_artifact_index(snapshot: GovernedRunSnapshot) -> None:
    """Render read-only native downloads from one finalized run manifest."""

    manifest_path = snapshot.manifest_path
    if manifest_path is None:
        return
    try:
        rows = load_sdlc_artifact_index(
            bundle=snapshot.artifact_bundle,
            manifest_path=manifest_path,
            workflow_status=snapshot.workflow_status,
        )
    except SDLCArtifactIndexError as error:
        st.warning(
            "SDLC Evidence & Artifacts are unavailable because the finalized "
            f"manifest could not be validated: {error}"
        )
        return

    st.header("SDLC Evidence & Artifacts")
    st.caption(
        "Read-only access to retained evidence in lifecycle order, derived from "
        "the finalized manifest."
    )
    stage_column, artifact_column, description_column, action_column = st.columns(
        (1.3, 1.8, 3.7, 1.0)
    )
    stage_column.markdown("**SDLC Stage**")
    artifact_column.markdown("**Artifact**")
    description_column.markdown("**What it shows**")
    action_column.markdown("**Action**")
    for row in rows:
        stage_column, artifact_column, description_column, action_column = (
            st.columns((1.3, 1.8, 3.7, 1.0))
        )
        stage_column.write(row.stage)
        artifact_column.text(row.artifact)
        description_column.write(row.description)
        action_column.download_button(
            "Download",
            data=row.contents,
            file_name=row.artifact,
            mime=row.mime_type,
            key=f"sdlc-artifact-download-{snapshot.run_id}-{row.artifact}",
            on_click="ignore",
        )


def _render_terminal_traceability(snapshot: GovernedRunSnapshot) -> None:
    """Build and render a read-only projection only when spec authority exists."""

    if snapshot.workflow_state.get("approved_requirement_spec") is None:
        return
    try:
        projection = build_requirement_traceability(
            snapshot.workflow_state,
            export_result=snapshot.export_result,
        )
    except TraceabilityProjectionError as error:
        st.warning(f"Requirement traceability is unavailable: {error}")
        return
    _render_requirement_traceability(projection)


def _render_requirement_traceability(
    projection: RequirementTraceabilityProjection,
) -> None:
    """Render the presentation-neutral projection without acquiring authority."""

    st.header("Requirement-to-Code Traceability")
    st.caption(
        "Deterministic read-only projection over existing governed state and "
        "evidence. Statuses are derived; retained evidence remains authoritative."
    )
    st.subheader("Traceability status")
    for status in TraceabilityStatus:
        st.markdown(
            f"**{status.value}** — {traceability_status_explanation(status)}"
        )
    _render_brownfield_traceability(projection)
    st.dataframe(
        [_traceability_table_row(row) for row in projection.rows],
        hide_index=True,
        width="stretch",
    )
    for row in projection.rows:
        _render_traceability_row_detail(row, projection)


def _render_brownfield_traceability(
    projection: RequirementTraceabilityProjection,
) -> None:
    lineage = projection.brownfield_lineage
    if lineage is None:
        return
    with st.expander("Brownfield baseline → governed outcome lineage", expanded=True):
        if not lineage.verified:
            st.warning(
                "Brownfield lineage could not be established from one exact "
                "correlated authority chain."
            )
            for gap in lineage.gaps:
                st.text(f"{gap.code.value}: {gap.detail}")
            return
        for index, step in enumerate(lineage.steps, start=1):
            st.text(
                f"{index}. [{step.basis.value}] {step.stage}: {step.identity}"
            )
            st.caption(step.detail)
        st.caption(
            "The approved impact analysis is traceable to the overall plan, but "
            "individual impact findings are not yet traceable to specific tasks."
        )
        st.caption("Missing links are shown explicitly rather than inferred.")


def _traceability_table_row(row: TraceabilityRow) -> dict[str, str]:
    authority = row.authority_links[0]
    tasks = _summarize_values(
        tuple(link.task_id for link in row.task_links),
        empty="No explicit task",
    )
    implementation = (
        _summarize_values(
            tuple(
                f"{link.target_path} · {link.operation.value}"
                for link in row.implementation_links
            ),
            separator="; ",
            empty="",
        )
        or "No materialized implementation"
    )
    validation = (
        _summarize_values(
            tuple(
                f"{link.profile.value} · PASS" for link in row.validation_links
            ),
            separator="; ",
            empty="",
        )
        or "No qualifying governed validation"
    )
    evidence_parts = []
    if row.artifact_links:
        evidence_parts.append(f"{len(row.artifact_links)} artifact")
    mutation_count = sum(
        link.evidence_kind == "WORKSPACE_MUTATION" for link in row.evidence_links
    )
    if mutation_count:
        evidence_parts.append(f"{mutation_count} mutation")
    if row.validation_links:
        evidence_parts.append(f"{len(row.validation_links)} validation")
    return {
        "Requirement / AC": f"{row.item_id} — {_compact_text(row.text)}",
        "Spec": f"{authority.spec_id} V{authority.spec_version:03d}",
        "Task": tasks,
        "Implementation": implementation,
        "Validation": validation,
        "Evidence": " · ".join(evidence_parts) or "No final evidence link",
        "Status": row.status.value,
    }


def _render_traceability_row_detail(
    row: TraceabilityRow,
    projection: RequirementTraceabilityProjection,
) -> None:
    with st.expander(f"{row.item_id} — {row.text} · {row.status.value}"):
        authority = row.authority_links[0]
        st.subheader("Traceability status")
        status_heading = traceability_status_heading(row.status)
        if row.status is TraceabilityStatus.VERIFIED:
            st.success(status_heading)
        elif row.status is TraceabilityStatus.UNVERIFIED:
            st.warning(status_heading)
        else:
            st.info(status_heading)
        st.write(traceability_row_evaluator_reason(row))

        st.subheader("Authoritative requirement")
        st.text(
            f"{authority.spec_id} V{authority.spec_version:03d} · analysis revision "
            f"{authority.source_analysis_revision}"
        )

        st.subheader("TaskGraph")
        if row.task_links:
            for link in row.task_links:
                st.text(f"[{link.basis.value}] {link.task_id} — {link.title}")
        else:
            st.text("No approved TaskGraph task explicitly references this item.")

        st.subheader("Generated artifact")
        if row.artifact_links:
            for link in row.artifact_links:
                st.text(
                    f"{link.artifact_type} · logical name: {link.logical_name}"
                )
        else:
            st.text("No canonical final-attempt artifact is linked.")

        st.subheader("Files changed")
        if row.implementation_links:
            for link in row.implementation_links:
                st.text(
                    f"{link.target_path} — {link.operation.value}"
                )
        else:
            st.text("No final-authority materialized implementation target is linked.")

        st.subheader("Validation performed")
        if row.validation_links:
            for link in row.validation_links:
                st.text(
                    f"{link.profile.value} — PASS through {link.task_id}"
                )
        else:
            st.text("No qualifying governed execution evidence linked.")

        final = projection.final_authority
        st.subheader("Run completion evidence")
        st.text(
            f"Workflow {final.workflow_status} · exit gate "
            f"{'passed' if final.exit_gate_passed else 'not passed'}"
        )
        st.text(
            "Readiness: "
            f"{final.readiness_validation_id or 'not established'} · "
            f"{'PASS' if final.readiness_passed else 'not passed'}"
        )
        if final.publication_succeeded:
            st.text(f"Published project: {final.publication_project_name}")
        else:
            st.text("No verified publication relationship is included.")

        st.subheader("Missing links")
        if row.gaps:
            for gap in row.gaps:
                st.text(f"  {gap.code.value}: {gap.detail}")
        else:
            st.text("None.")

        st.subheader("Technical evidence")
        st.text(
            f"Authority [{authority.basis.value}] · item lineage "
            f"{authority.item_lineage_id}"
        )
        for link in row.task_links:
            st.text(f"Task [{link.basis.value}] · {link.task_id}")
        for link in row.artifact_links:
            st.text(
                f"Artifact [{link.basis.value}] · {link.artifact_id} · "
                f"request {link.request_id} · attempt {link.attempt_id}"
            )
        for link in row.implementation_links:
            st.text(
                f"Materialization [{link.basis.value}] · "
                f"{link.materialization_validation_id} · artifact "
                f"{link.artifact_id} · target {link.target_path} · change set "
                f"{link.change_set_id} · mutation {link.mutation_id}"
            )
            st.caption(
                "Preimage: "
                f"{link.expected_preimage_hash or 'none'} · postimage: "
                f"{link.observed_postimage_hash}"
            )
        for link in row.validation_links:
            st.text(
                f"Validation [{link.basis.value}] · "
                f"{link.validation_requirement_id} · evidence {link.evidence_id} · "
                f"policy {link.policy_id} {link.policy_version}"
            )
            if link.provisioning_evidence_ids:
                st.caption(
                    "Provisioning evidence: "
                    + ", ".join(link.provisioning_evidence_ids)
                )
        for link in row.evidence_links:
            st.text(
                f"Evidence [{link.basis.value}] · {link.evidence_kind} · "
                f"{link.evidence_id}"
            )
        st.text(
            "Final workspace snapshot: "
            f"{final.final_workspace_snapshot_id or 'not established'}"
        )
        st.caption(f"Projection reason: {row.status_reason}")


def _compact_text(value: str, *, limit: int = 72) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _summarize_values(
    values: Sequence[str],
    *,
    separator: str = ", ",
    empty: str,
    limit: int = 3,
) -> str:
    if not values:
        return empty
    visible = tuple(values[:limit])
    remainder = len(values) - len(visible)
    summary = separator.join(visible)
    return f"{summary}{separator}+{remainder} more" if remainder else summary


def _render_engineering_execution(
    progress: StreamlitExecutionProgressView | None,
) -> None:
    """Render supplementary structured telemetry without inferring authority."""

    st.header("Engineering Execution")
    if progress is None:
        st.info(
            "TaskGraph approved. Waiting for structured Task Agent execution "
            "telemetry..."
        )
        return

    metric_columns = st.columns(5)
    metric_columns[0].metric("Progress status", progress.telemetry_status)
    metric_columns[1].metric(
        "Completed tasks",
        f"{progress.completed_task_count} / {progress.total_task_count}",
    )
    metric_columns[2].metric(
        "Current wave",
        (
            f"{progress.current_wave_number} · {progress.current_wave_mode}"
            if progress.current_wave_number is not None
            else "Not started"
        ),
    )
    metric_columns[3].metric(
        "Observed elapsed",
        _format_elapsed(progress.elapsed_seconds),
    )
    metric_columns[4].metric(
        "Retries / failed",
        f"{progress.retry_count} / {progress.failed_task_count}",
    )
    st.caption(
        f"Run ID: {progress.run_id} · Execution operation: "
        f"{progress.operation_id}"
    )
    if progress.current_layer_numbers:
        st.caption(
            "Current canonical layer context: "
            + ", ".join(
                str(layer_number)
                for layer_number in progress.current_layer_numbers
            )
            + ". Scheduler waves remain distinct from canonical layers because "
            "parallelism limits and retries may create additional waves."
        )

    task_by_id = {task.task_id: task for task in progress.tasks}
    st.subheader("Canonical execution layers")
    rendered_task_ids: set[str] = set()
    for layer_number, task_ids in enumerate(progress.execution_layers, start=1):
        parallel_suffix = " — parallel" if len(task_ids) > 1 else ""
        with st.container(border=True):
            st.subheader(f"Layer {layer_number}{parallel_suffix}")
            for task_id in task_ids:
                task = task_by_id.get(task_id)
                if task is None:
                    st.warning(
                        "Canonical execution semantics reference a task without "
                        f"progress metadata: {task_id}"
                    )
                    continue
                rendered_task_ids.add(task_id)
                _render_execution_task(task)

    for task in progress.tasks:
        if task.task_id in rendered_task_ids:
            continue
        if task.unknown_task:
            st.warning(
                "Structured execution telemetry referenced an unknown canonical "
                f"task ID: {task.task_id}"
            )
        _render_execution_task(task)

    with st.expander(
        f"Execution events ({len(progress.recent_events)} recent)",
    ):
        if not progress.recent_events:
            st.caption("No structured execution event has arrived yet.")
        for event in progress.recent_events:
            wave = (
                f" · wave {event.wave_number}"
                if event.wave_number is not None
                else ""
            )
            tasks = (
                f" · {', '.join(event.task_ids)}" if event.task_ids else ""
            )
            st.text(
                f"{_format_elapsed(event.elapsed_seconds)} · "
                f"{event.event_type}{wave}{tasks} · {event.detail}"
            )
        if progress.dropped_event_count:
            st.caption(
                f"{progress.dropped_event_count} older event(s) were discarded by "
                "the bounded in-memory timeline."
            )


def _render_execution_task(task: StreamlitTaskExecutionProgress) -> None:
    icons = {
        "AWAITING_EVENT": "⏳",
        "PREPARING": "⏳",
        "RUNNING": "🔄",
        "VALIDATING": "🔄",
        "RETRY_SCHEDULED": "🔁",
        "SUCCEEDED": "✅",
        "FAILED": "❌",
        "SAFE_STOPPED": "❌",
    }
    identity = (
        f"{task.task_id} — {task.title}" if task.title else task.task_id
    )
    st.markdown(
        f"{icons.get(task.status, 'ℹ️')} **{identity}** · `{task.status}`"
    )
    details = []
    if task.wave_number is not None:
        details.append(f"wave {task.wave_number}")
    if task.attempt_number:
        details.append(f"attempt {task.attempt_number}")
    if task.retry_count:
        details.append(f"retries {task.retry_count}")
    if task.attempt_elapsed_seconds is not None:
        details.append(
            "attempt elapsed " + _format_elapsed(task.attempt_elapsed_seconds)
        )
    if task.latest_detail:
        details.append(task.latest_detail)
    if details:
        st.caption(" · ".join(details))


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "Not available"
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _render_requirement_analysis_review(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
    *,
    clarification_drafter: ClarificationDrafter | None,
) -> None:
    payload = gate.payload
    analysis = _mapping(payload.get("requirement_analysis"))
    readiness = _mapping(payload.get("planning_readiness"))
    revision = payload.get("revision_number", 0)

    st.warning(
        "Agent/LLM proposed this analysis. Human approval is required before "
        "the workflow can proceed."
    )
    st.header("Requirement Analysis")
    _render_run_context(snapshot, gate)

    readiness_status = str(readiness.get("status", "UNKNOWN"))
    metric_columns = st.columns(3)
    metric_columns[0].metric("Revision", revision)
    metric_columns[1].metric(
        "Planning readiness",
        readiness_status,
    )
    confidence = analysis.get("confidence")
    confidence_label = (
        f"{float(confidence):.0%}"
        if isinstance(confidence, (int, float))
        else "Unknown"
    )
    metric_columns[2].metric(
        "Confidence",
        confidence_label,
    )

    if readiness_status == "READY":
        st.success("This analysis is ready for planning if a human approves it.")
    elif readiness_status == "BLOCKED":
        st.warning(
            "This analysis needs clarification. Approval is not permitted by the "
            "authoritative gate."
        )

    st.subheader("Normalized problem statement")
    st.write(str(analysis.get("normalized_problem_statement", "")))
    st.caption(f"Requirement type: {analysis.get('requirement_type', 'unknown')}")

    left_column, right_column = st.columns(2)
    with left_column:
        _render_analysis_collection(
            "Functional requirements",
            analysis.get("functional_requirements", ()),
            expanded=True,
        )
        _render_analysis_collection(
            "Nonfunctional requirements",
            analysis.get("nonfunctional_requirements", ()),
        )
        _render_analysis_collection(
            "Constraints",
            analysis.get("constraints", ()),
        )
        _render_analysis_collection(
            "Acceptance criteria",
            analysis.get("acceptance_criteria", ()),
            expanded=True,
        )
    with right_column:
        _render_analysis_collection(
            "Ambiguities",
            analysis.get("ambiguities", ()),
            expanded=readiness_status == "BLOCKED",
        )
        _render_analysis_collection(
            "Assumptions",
            analysis.get("assumptions", ()),
        )
        _render_analysis_collection(
            "Risks",
            analysis.get("risks", ()),
        )
        _render_analysis_collection(
            "Blocking clarifications",
            readiness.get("blocking_ambiguities", ()),
            expanded=readiness_status == "BLOCKED",
        )

    _render_brownfield_impact(
        analysis.get("brownfield_impact"),
        title="Brownfield Impact",
        show_provenance=True,
    )
    _render_analysis_history(snapshot)
    if readiness_status == "BLOCKED":
        try:
            draft_request = _clarification_draft_request(
                snapshot,
                gate,
                analysis=analysis,
                readiness=readiness,
                revision=revision,
            )
        except (ValidationError, ValueError) as error:
            _clear_clarification_draft_state()
            st.error(
                "Clarification assistance is unavailable because the current "
                f"governed context is incomplete ({type(error).__name__})."
            )
        else:
            _render_clarification_draft_helper(
                runtime,
                gate,
                draft_request,
                clarification_drafter=clarification_drafter,
            )
    else:
        _clear_clarification_draft_state()
    _render_requirement_decision_form(runtime, snapshot, gate)


def _clarification_draft_request(
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
    *,
    analysis: Mapping[str, Any],
    readiness: Mapping[str, Any],
    revision: object,
) -> ClarificationDraftRequest:
    """Validate the narrow current context supplied to presentation assistance."""

    submission = _mapping(snapshot.workflow_state.get("requirement_submission"))
    original_requirement = submission.get("original_text")
    if not isinstance(original_requirement, str) or not original_requirement.strip():
        raise ValueError("Original requirement evidence is unavailable.")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError("Requirement Analysis revision is invalid.")
    return ClarificationDraftRequest(
        run_id=snapshot.run_id,
        gate_token=gate.gate_token,
        analysis_revision=revision,
        original_requirement=original_requirement,
        requirement_analysis=RequirementAnalysis.model_validate(
            dict(analysis),
            strict=False,
        ),
        planning_readiness=RequirementPlanningReadiness.model_validate(
            dict(readiness),
            strict=False,
        ),
    )


def _render_clarification_draft_helper(
    runtime: StreamlitRunRuntime,
    gate: HumanGovernanceGate,
    request: ClarificationDraftRequest,
    *,
    clarification_drafter: ClarificationDrafter | None,
) -> None:
    """Render ephemeral drafting controls without invoking governed resume."""

    context_identity = clarification_draft_context_identity(request)
    if st.session_state.get(_CLARIFICATION_DRAFT_CONTEXT_KEY) != context_identity:
        _clear_clarification_draft_state()
        st.session_state[_CLARIFICATION_DRAFT_CONTEXT_KEY] = context_identity

    draft_view = runtime.poll_clarification_draft(context_identity)
    if (
        draft_view.result is not None
        and draft_view.generation_id is not None
        and st.session_state.get(_CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY)
        != draft_view.generation_id
    ):
        st.session_state[_CLARIFICATION_DRAFT_TEXT_KEY] = (
            draft_view.result.suggested_clarification
        )
        st.session_state[_CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY] = (
            draft_view.generation_id
        )

    st.subheader("Clarification assistance")
    st.caption(
        "This optional AI draft is editable presentation state. It does not resolve "
        "ambiguities or submit a human decision."
    )
    existing_draft = st.session_state.get(_CLARIFICATION_DRAFT_TEXT_KEY)
    has_draft = isinstance(existing_draft, str) and bool(existing_draft.strip())
    generation_label = (
        "Regenerate draft" if has_draft else "Draft clarification response"
    )
    if draft_view.error_message:
        st.error(
            "Clarification draft was not generated: "
            f"{draft_view.error_message}"
        )
    generation_clicked = st.button(
        generation_label,
        key=f"clarification_draft_generate_{gate.gate_token}",
        disabled=draft_view.in_flight,
    )
    if generation_clicked:
        active_drafter = clarification_drafter or OpenAIClarificationDrafter()
        runtime.schedule_clarification_draft(
            uuid4().hex,
            context_identity,
            request,
            active_drafter,
        )
        draft_view = runtime.poll_clarification_draft(context_identity)

    if draft_view.in_flight:
        _render_clarification_polling_fragment(runtime, context_identity)

    current_draft = st.session_state.get(_CLARIFICATION_DRAFT_TEXT_KEY)
    if not isinstance(current_draft, str) or not current_draft.strip():
        return
    edited_draft = st.text_area(
        "Suggested clarification",
        key=_CLARIFICATION_DRAFT_TEXT_KEY,
        height=180,
        help=(
            "Edit this non-authoritative suggestion before copying it into human "
            "review feedback."
        ),
    )
    feedback_key = _requirement_feedback_key(gate)
    existing_feedback = st.session_state.get(feedback_key)
    feedback_exists = isinstance(existing_feedback, str) and existing_feedback != ""
    adoption_label = (
        "Replace feedback with this draft" if feedback_exists else "Use this draft"
    )
    if feedback_exists:
        st.caption(
            "Existing human feedback will be preserved unless you explicitly "
            "replace it."
        )
    if st.button(
        adoption_label,
        key=f"clarification_draft_adopt_{gate.gate_token}",
        disabled=not edited_draft.strip(),
    ):
        st.session_state[feedback_key] = edited_draft


def _clear_clarification_draft_state() -> None:
    """Discard stale non-authoritative text before another gate renders it."""

    st.session_state.pop(_CLARIFICATION_DRAFT_CONTEXT_KEY, None)
    st.session_state.pop(_CLARIFICATION_DRAFT_TEXT_KEY, None)
    st.session_state.pop(_CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY, None)


def _requirement_feedback_key(gate: HumanGovernanceGate) -> str:
    return f"requirement_decision_feedback_{gate.gate_token}"


def _render_analysis_collection(
    label: str,
    values: object,
    *,
    expanded: bool = False,
) -> None:
    items = _string_sequence(values)
    with st.expander(f"{label} ({len(items)})", expanded=expanded):
        if not items:
            st.caption("None identified.")
            return
        for item in items:
            st.markdown(f"- {item}")


def _render_brownfield_impact(
    value: object,
    *,
    title: str | None,
    show_provenance: bool,
) -> None:
    """Render the workflow-proposed structured impact without recomputing it."""

    impact = _mapping(value)
    if not impact:
        return
    if title is not None:
        st.subheader(title)
    populated = False
    for field_name, label in _BROWNFIELD_IMPACT_CATEGORIES:
        findings = _mapping_sequence(impact.get(field_name, ()))
        if not findings:
            continue
        populated = True
        st.markdown(f"**{label}**")
        for finding in findings:
            target = str(finding.get("target", "Unknown target"))
            reason = str(finding.get("reason", "No reason supplied."))
            st.markdown(f"- **{target}** — {reason}")
    if not populated:
        st.caption("No populated brownfield impact categories were identified.")
    if show_provenance:
        with st.expander("Brownfield impact provenance"):
            st.text(f"Baseline ID: {impact.get('baseline_id', 'unknown')}")
            st.text(
                "Codebase context ID: "
                f"{impact.get('codebase_context_id', 'unknown')}"
            )


def _render_analysis_history(snapshot: GovernedRunSnapshot) -> None:
    analysis_history = _mapping_sequence(
        snapshot.workflow_state.get("requirement_analysis_history", ())
    )
    review_history = _mapping_sequence(
        snapshot.workflow_state.get("requirement_review_history", ())
    )
    if len(analysis_history) <= 1 and not review_history:
        return

    with st.expander(f"Revision history ({len(analysis_history)} analyses)"):
        for record in analysis_history:
            st.write(
                f"Revision {record.get('revision_number', '?')} · "
                f"attempt {record.get('attempt_number', '?')}"
            )
            reviewer_feedback = record.get("reviewer_feedback")
            if isinstance(reviewer_feedback, str) and reviewer_feedback:
                st.caption(f"Human feedback: {reviewer_feedback}")
        for event in review_history:
            st.write(
                f"Decision {event.get('sequence', '?')}: "
                f"{event.get('decision', 'unknown')} on revision "
                f"{event.get('revision_number', '?')}"
            )


def _render_requirement_decision_form(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
) -> None:
    allowed_decisions = gate.allowed_decisions
    if not allowed_decisions:
        st.error("The authoritative human gate exposes no available decisions.")
        return

    labels = {
        "APPROVE": "Approve and continue",
        "REQUEST_CHANGES": "Request changes",
        "REJECT": "Reject and safely stop",
    }
    st.subheader("Human decision")
    decision = st.radio(
        "Decision",
        allowed_decisions,
        format_func=lambda value: labels[value],
        key=f"requirement_decision_choice_{gate.gate_token}",
    )
    feedback = st.text_area(
        "Review feedback",
        key=_requirement_feedback_key(gate),
        help=(
            "Required for Request changes. Meaningful whitespace and line breaks "
            "are passed to the governed workflow unchanged."
        ),
    )
    submitted = st.button(
        "Submit Decision",
        key=f"requirement_decision_submit_{gate.gate_token}",
        type="primary",
        use_container_width=True,
    )

    if not submitted:
        return
    if decision == "REQUEST_CHANGES" and not feedback.strip():
        st.error("Please provide feedback before requesting changes.")
        return

    response: ApprovalResponse = {
        "decision": cast(ApprovalDecision, decision),
        "feedback": feedback,
    }
    operation_id = uuid4().hex
    try:
        scheduled = runtime.schedule_resume(
            operation_id,
            snapshot.run_id,
            response,
            gate_token=gate.gate_token,
        )
    except GovernedRunLifecycleError as error:
        st.error(str(error))
        return
    if not scheduled:
        st.info("A decision for this human gate is already being processed.")
        return

    st.session_state[_OPERATION_ID_KEY] = operation_id
    st.session_state[_UI_PHASE_KEY] = (
        "requirement_analysis_revision"
        if decision == "REQUEST_CHANGES"
        else "task_planning"
        if decision == "APPROVE"
        else "executing"
    )
    st.rerun()


def _render_task_graph_review(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
) -> None:
    payload = gate.payload
    spec = _mapping(payload.get("approved_requirement_spec"))
    delivery_policy = _mapping(payload.get("project_delivery_policy"))
    graph = _mapping(payload.get("candidate_task_graph"))
    semantics = _mapping(payload.get("graph_semantics"))
    tasks = _mapping_sequence(graph.get("tasks", ()))
    execution_layers = _nested_string_sequences(
        semantics.get("execution_layers", ())
    )
    synchronization_points = _string_sequence(
        semantics.get("synchronization_points", ())
    )

    st.success("Requirement Analysis approved.")
    st.warning(
        "Agent/LLM proposed this canonical TaskGraph. Human approval is required "
        "before governed engineering execution can begin."
    )
    st.header("TaskGraph Review")
    _render_run_context(snapshot, gate)
    baseline = _mapping(snapshot.workflow_state.get("brownfield_baseline"))
    baseline_name = baseline.get("selected_project_name")
    if isinstance(baseline_name, str) and baseline_name:
        st.info(
            f"Planning incremental changes to baseline {baseline_name} for "
            f"publication as {_snapshot_output_project_name(snapshot)}."
        )
        approved_impact = spec.get("brownfield_impact")
        if _mapping(approved_impact):
            with st.expander("Approved Brownfield Impact"):
                _render_brownfield_impact(
                    approved_impact,
                    title=None,
                    show_provenance=False,
                )

    summary_columns = st.columns(4)
    summary_columns[0].metric(
        "TaskGraph revision",
        payload.get("revision_number", 0),
    )
    summary_columns[1].metric("Canonical tasks", len(tasks))
    summary_columns[2].metric("Execution layers", len(execution_layers))
    summary_columns[3].metric(
        "Synchronization points",
        len(synchronization_points),
    )
    st.caption(
        f"Graph ID: {graph.get('graph_id', 'unknown')} · "
        f"Graph version: {graph.get('version', 'unknown')} · "
        f"Delivery mode: {delivery_policy.get('mode', 'unknown')}"
    )
    st.caption(
        f"Approved requirement spec: {spec.get('spec_id', 'unknown')} · "
        f"Spec version: {spec.get('version', 'unknown')} · "
        "Source Requirement Analysis revision: "
        f"{spec.get('source_analysis_revision', 'unknown')}"
    )

    st.subheader("Canonical dependency DAG")
    try:
        mermaid = task_graph_mermaid(graph, semantics)
    except TaskGraphPresentationError as error:
        st.error(f"The canonical TaskGraph could not be visualized: {error}")
    else:
        st.mermaid_chart(mermaid)

    _render_task_graph_semantics(semantics)
    _render_task_execution_layers(tasks, execution_layers, spec)
    _render_task_graph_history(snapshot)
    _render_task_graph_decision_form(runtime, snapshot, gate)


def _render_task_graph_semantics(semantics: Mapping[str, Any]) -> None:
    st.subheader("Authoritative execution semantics")
    st.text(
        "Topological order: "
        + _joined_or_none(_string_sequence(semantics.get("topological_order", ())))
    )
    st.text(
        "ENTRY-ready tasks: "
        + _joined_or_none(
            _string_sequence(semantics.get("entry_ready_tasks", ()))
        )
    )
    st.text(
        "Synchronization points: "
        + _joined_or_none(
            _string_sequence(semantics.get("synchronization_points", ()))
        )
    )
    st.text(
        "EXIT predecessors: "
        + _joined_or_none(
            _string_sequence(semantics.get("exit_predecessor_tasks", ()))
        )
    )


def _render_task_execution_layers(
    tasks: Sequence[Mapping[str, Any]],
    execution_layers: Sequence[Sequence[str]],
    spec: Mapping[str, Any],
) -> None:
    task_by_id = {
        str(task.get("task_id")): task
        for task in tasks
        if isinstance(task.get("task_id"), str)
    }
    reference_lookup = _approved_spec_reference_lookup(spec)
    st.subheader("Execution-layer review")
    for layer_number, task_ids in enumerate(execution_layers, start=1):
        parallel_suffix = " — parallel" if len(task_ids) > 1 else ""
        with st.container(border=True):
            st.subheader(f"Layer {layer_number}{parallel_suffix}")
            if len(task_ids) > 1:
                st.info("Parallel tasks: " + ", ".join(task_ids))
            for task_id in task_ids:
                task = task_by_id.get(task_id)
                if task is None:
                    st.warning(
                        "Authoritative execution semantics reference a missing "
                        f"canonical task: {task_id}"
                    )
                    continue
                _render_task_details(task, reference_lookup)


def _render_task_details(
    task: Mapping[str, Any],
    reference_lookup: Mapping[str, str],
) -> None:
    task_id = str(task.get("task_id", "unknown"))
    title = str(task.get("title", "Untitled task"))
    with st.expander(f"{task_id} — {title}"):
        st.markdown("**Canonical identity**")
        st.text(f"Task ID: {task_id}")
        st.text(f"Lineage ID: {task.get('lineage_id', 'unknown')}")
        st.text(f"Planner source key: {task.get('source_key', 'unknown')}")

        st.markdown("**Task Agent responsibility**")
        st.text(f"Title: {title}")
        st.text(str(task.get("description", "")))
        st.text(f"Task type: {task.get('task_type', 'unknown')}")
        st.text(
            "Materialization policy: "
            f"{task.get('materialization_policy', 'unknown')}"
        )
        st.text(
            "Depends on: "
            + _joined_or_entry(_string_sequence(task.get("depends_on", ())))
        )
        st.text(
            "Expected outputs: "
            + _joined_or_none(_string_sequence(task.get("expected_outputs", ())))
        )
        st.text(
            "Deliverable roles: "
            + _joined_or_none(_string_sequence(task.get("deliverable_roles", ())))
        )
        st.text(
            "Required validations: "
            + _joined_or_none(
                _validation_profiles(task.get("required_validations", ()))
            )
        )

        st.markdown("**Approved-spec traceability**")
        _render_reference_group(
            "Requirements",
            _string_sequence(task.get("requirement_refs", ())),
            reference_lookup,
        )
        _render_reference_group(
            "Acceptance criteria",
            _string_sequence(task.get("acceptance_criteria_refs", ())),
            reference_lookup,
        )
        _render_reference_group(
            "Risks",
            _string_sequence(task.get("risk_refs", ())),
            reference_lookup,
        )
        _render_reference_group(
            "Ambiguities",
            _string_sequence(task.get("ambiguity_refs", ())),
            reference_lookup,
        )


def _render_reference_group(
    label: str,
    reference_ids: Sequence[str],
    reference_lookup: Mapping[str, str],
) -> None:
    st.text(f"{label}:")
    if not reference_ids:
        st.text("  None")
        return
    for reference_id in reference_ids:
        reference_text = reference_lookup.get(reference_id)
        if reference_text is None:
            st.warning(
                f"{reference_id} — approved requirement-spec text was not found."
            )
        else:
            st.text(f"  {reference_id} — {reference_text}")


def _approved_spec_reference_lookup(spec: Mapping[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for field_name in (
        "functional_requirements",
        "nonfunctional_requirements",
        "constraints",
        "acceptance_criteria",
        "risks",
        "ambiguities",
    ):
        for item in _mapping_sequence(spec.get(field_name, ())):
            item_id = item.get("item_id")
            text = item.get("text")
            if isinstance(item_id, str) and isinstance(text, str):
                lookup[item_id] = text
    return lookup


def _render_task_graph_history(snapshot: GovernedRunSnapshot) -> None:
    graph_history = _mapping_sequence(
        snapshot.workflow_state.get("task_graph_history", ())
    )
    review_history = _mapping_sequence(
        snapshot.workflow_state.get("task_graph_review_history", ())
    )
    with st.expander(
        f"TaskGraph governance history ({len(graph_history)} generated graphs)"
    ):
        for record in graph_history:
            graph = _mapping(record.get("task_graph"))
            st.text(
                f"Graph generation {record.get('sequence', '?')}: revision "
                f"{record.get('revision_number', '?')}, attempt "
                f"{record.get('attempt_number', '?')}"
            )
            st.text(
                f"Graph ID: {graph.get('graph_id', 'unknown')} · "
                f"version {graph.get('version', 'unknown')}"
            )
            st.text(
                f"Prompt: {record.get('prompt_version', 'unknown')} · "
                f"Model: {record.get('model_name', 'unknown')}"
            )
            reviewer_feedback = record.get("reviewer_feedback")
            if isinstance(reviewer_feedback, str) and reviewer_feedback:
                st.text(f"Human feedback used: {reviewer_feedback}")
        for event in review_history:
            st.text(
                f"Human decision {event.get('sequence', '?')}: "
                f"{event.get('decision', 'unknown')} on revision "
                f"{event.get('revision_number', '?')}"
            )
            feedback = event.get("feedback")
            if isinstance(feedback, str) and feedback:
                st.text(f"Decision feedback: {feedback}")


def _render_task_graph_decision_form(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
) -> None:
    allowed_decisions = gate.allowed_decisions
    if not allowed_decisions:
        st.error("The authoritative TaskGraph gate exposes no available decisions.")
        return

    labels = {
        "APPROVE": "Approve TaskGraph and execute",
        "REQUEST_CHANGES": "Request TaskGraph changes",
        "REJECT": "Reject TaskGraph and safely stop",
    }
    st.subheader("TaskGraph human decision")
    with st.form(f"task_graph_decision_{gate.gate_token}"):
        decision = st.radio(
            "TaskGraph decision",
            allowed_decisions,
            format_func=lambda value: labels[value],
            key=f"task_graph_decision_choice_{gate.gate_token}",
        )
        feedback = st.text_area(
            "TaskGraph review feedback",
            key=f"task_graph_decision_feedback_{gate.gate_token}",
            help=(
                "Required for Request TaskGraph changes. Meaningful whitespace and "
                "line breaks are passed to the governed workflow unchanged."
            ),
        )
        submitted = st.form_submit_button(
            "Submit TaskGraph Decision",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return
    if decision == "REQUEST_CHANGES" and not feedback.strip():
        st.error("Please provide feedback before requesting TaskGraph changes.")
        return

    response: ApprovalResponse = {
        "decision": cast(ApprovalDecision, decision),
        "feedback": feedback,
    }
    operation_id = uuid4().hex
    try:
        scheduled = runtime.schedule_resume(
            operation_id,
            snapshot.run_id,
            response,
            gate_token=gate.gate_token,
        )
    except GovernedRunLifecycleError as error:
        st.error(str(error))
        return
    if not scheduled:
        st.info("A decision for this TaskGraph gate is already being processed.")
        return

    st.session_state[_OPERATION_ID_KEY] = operation_id
    st.session_state[_UI_PHASE_KEY] = (
        "task_graph_execution"
        if decision == "APPROVE"
        else "task_graph_revision"
        if decision == "REQUEST_CHANGES"
        else "executing"
    )
    st.rerun()


def _render_run_context(
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate | None,
) -> None:
    stage = gate.stage if gate is not None else "terminal"
    checkpoint = gate.checkpoint if gate is not None else "none"
    st.caption(
        f"Run ID: {snapshot.run_id} · Stage: {stage} · "
        f"Checkpoint: {checkpoint} · Workflow status: {snapshot.workflow_status}"
    )
    if gate is not None:
        st.caption(
            f"Current gate token: {gate.gate_token} · Allowed decisions: "
            f"{', '.join(gate.allowed_decisions)}"
        )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item) for item in value)
    return ()


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _validation_profiles(value: object) -> tuple[str, ...]:
    return tuple(
        str(item["profile"])
        for item in _mapping_sequence(value)
        if isinstance(item.get("profile"), str) and item["profile"]
    )


def _nested_string_sequences(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(_string_sequence(item) for item in value)


def _joined_or_none(values: Sequence[str]) -> str:
    return ", ".join(values) or "None"


def _joined_or_entry(values: Sequence[str]) -> str:
    return ", ".join(values) or "ENTRY"


if __name__ == "__main__":
    main()
