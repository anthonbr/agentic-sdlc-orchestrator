"""Thin Streamlit presentation adapter for one governed workflow session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import streamlit as st

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunLifecycleError,
    GovernedRunSnapshot,
    HumanGovernanceGate,
)
from agentic_sdlc.project_export import ProjectNameError
from agentic_sdlc.requirement_submission import RequirementSubmissionError
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


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENT_REVIEW_STAGE = "requirement_analysis_review"
TASK_GRAPH_REVIEW_STAGE = "task_graph_review"

_UI_PHASE_KEY = "agentic_sdlc_ui_phase"
_RUN_ID_KEY = "agentic_sdlc_current_run_id"
_OPERATION_ID_KEY = "agentic_sdlc_operation_id"


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


def render_app(runtime: StreamlitRunRuntime) -> None:
    """Render one UI pass from the runtime's immutable polling projection."""

    st.title("Agentic SDLC Orchestrator")
    st.write(
        "Supply a natural-language software requirement. The governed workflow "
        "will analyze it before asking for your approval."
    )

    view = runtime.poll()
    if view.error_message:
        st.error(view.error_message)

    if view.in_flight:
        _render_polling_fragment(
            runtime,
            view,
            str(st.session_state.get(_UI_PHASE_KEY, "executing")),
        )
        return

    snapshot = view.snapshot
    if snapshot is None:
        st.session_state[_UI_PHASE_KEY] = "requirement_entry"
        _render_requirement_entry(runtime)
        return

    st.session_state[_RUN_ID_KEY] = snapshot.run_id
    _render_snapshot(
        runtime,
        snapshot,
        execution_progress=view.execution_progress,
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
    if ui_phase == "task_graph_execution":
        _render_engineering_execution(view.execution_progress)
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
    with st.container(border=True):
        st.subheader("Describe the software you want to build")
        with st.form("requirement_entry_form"):
            requirement_text = st.text_area(
                "Software requirement",
                height=240,
                placeholder=(
                    "Example: Build a task manager that can add, list, prioritize, "
                    "and complete tasks."
                ),
                help="Enter the complete natural-language requirement inline.",
            )
            project_name = st.text_input(
                "Project name (optional)",
                placeholder="task-manager",
                help=(
                    "Leave blank to use the existing deterministic project-name "
                    "behavior."
                ),
            )
            submitted = st.form_submit_button(
                "Analyze Requirement",
                type="primary",
                use_container_width=True,
            )

    if not submitted:
        return

    try:
        request = governed_run_request_from_inline_requirement(
            requirement_text,
            project_name,
        )
    except RequirementSubmissionError as error:
        st.error(str(error))
        return
    except ProjectNameError as error:
        st.error(f"Invalid project name: {error}")
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

    st.session_state[_OPERATION_ID_KEY] = operation_id
    st.session_state[_UI_PHASE_KEY] = "requirement_analysis"
    st.rerun()


def _render_snapshot(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
    *,
    execution_progress: StreamlitExecutionProgressView | None,
) -> None:
    for warning in snapshot.warnings:
        st.warning(warning)

    gate = snapshot.human_gate
    if (
        snapshot.application_status is GovernedRunApplicationStatus.AWAITING_HUMAN
        and gate is not None
    ):
        if gate.stage == REQUIREMENT_REVIEW_STAGE:
            st.session_state[_UI_PHASE_KEY] = "requirement_analysis_review"
            _render_requirement_analysis_review(runtime, snapshot, gate)
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
        _render_run_context(snapshot, None)
        if execution_progress is not None:
            _render_engineering_execution(execution_progress)
        return

    if snapshot.application_status is GovernedRunApplicationStatus.SUCCEEDED:
        st.session_state[_UI_PHASE_KEY] = "succeeded"
        st.success("The governed workflow completed successfully.")
        _render_run_context(snapshot, None)
        if execution_progress is not None:
            _render_engineering_execution(execution_progress)
        return

    st.session_state[_UI_PHASE_KEY] = "failed"
    st.error("The governed workflow did not reach a reviewable or successful state.")
    if snapshot.application_error:
        st.write(snapshot.application_error)
    for error in _string_sequence(snapshot.workflow_state.get("errors", ())):
        st.write(f"- {error}")
    _render_run_context(snapshot, None)
    if execution_progress is not None:
        _render_engineering_execution(execution_progress)


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

    _render_analysis_history(snapshot)
    _render_requirement_decision_form(runtime, snapshot, gate)


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
    with st.form(f"requirement_decision_{gate.gate_token}"):
        decision = st.radio(
            "Decision",
            allowed_decisions,
            format_func=lambda value: labels[value],
            key=f"requirement_decision_choice_{gate.gate_token}",
        )
        feedback = st.text_area(
            "Review feedback",
            key=f"requirement_decision_feedback_{gate.gate_token}",
            help=(
                "Required for Request changes. Meaningful whitespace and line breaks "
                "are passed to the governed workflow unchanged."
            ),
        )
        submitted = st.form_submit_button(
            "Submit Decision",
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
