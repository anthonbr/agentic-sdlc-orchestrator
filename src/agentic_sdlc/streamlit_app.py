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
from agentic_sdlc.streamlit_runtime import (
    StreamlitOperationKind,
    StreamlitRunRuntime,
    StreamlitRuntimeView,
    governed_run_request_from_inline_requirement,
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
        _render_polling_fragment(runtime, view)
        return

    snapshot = view.snapshot
    if snapshot is None:
        st.session_state[_UI_PHASE_KEY] = "requirement_entry"
        _render_requirement_entry(runtime)
        return

    st.session_state[_RUN_ID_KEY] = snapshot.run_id
    _render_snapshot(runtime, snapshot)


@st.fragment(run_every=0.5)
def _render_polling_fragment(
    runtime: StreamlitRunRuntime,
    initial_view: StreamlitRuntimeView,
) -> None:
    """Poll background work without allowing worker threads to call Streamlit."""

    view = runtime.poll()
    if not view.in_flight:
        st.rerun()

    operation_kind = view.operation_kind or initial_view.operation_kind
    if operation_kind is StreamlitOperationKind.START:
        message = "Analyzing requirement..."
    else:
        message = "Applying the human decision and advancing the governed workflow..."
    st.session_state[_UI_PHASE_KEY] = "executing"
    st.info(message)
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
    st.session_state[_UI_PHASE_KEY] = "executing"
    st.rerun()


def _render_snapshot(
    runtime: StreamlitRunRuntime,
    snapshot: GovernedRunSnapshot,
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
            _render_task_graph_arrival(snapshot, gate)
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
        return

    if snapshot.application_status is GovernedRunApplicationStatus.SUCCEEDED:
        st.session_state[_UI_PHASE_KEY] = "succeeded"
        st.success("The governed workflow completed successfully.")
        _render_run_context(snapshot, None)
        return

    st.session_state[_UI_PHASE_KEY] = "failed"
    st.error("The governed workflow did not reach a reviewable or successful state.")
    if snapshot.application_error:
        st.write(snapshot.application_error)
    for error in _string_sequence(snapshot.workflow_state.get("errors", ())):
        st.write(f"- {error}")
    _render_run_context(snapshot, None)


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
    st.session_state[_UI_PHASE_KEY] = "executing"
    st.rerun()


def _render_task_graph_arrival(
    snapshot: GovernedRunSnapshot,
    gate: HumanGovernanceGate,
) -> None:
    st.success("Requirement Analysis approved.")
    st.header("TaskGraph review reached")
    st.info(
        "Requirement Analysis approved. The workflow has reached TaskGraph review. "
        "Interactive TaskGraph review will be added in the next GUI slice."
    )
    st.caption("No TaskGraph decision has been submitted by this GUI.")
    _render_run_context(snapshot, gate)


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


if __name__ == "__main__":
    main()
