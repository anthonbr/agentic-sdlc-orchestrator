"""Streamlit AppTest coverage for the first governed GUI vertical slice."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunRequest,
    GovernedRunSnapshot,
)
from agentic_sdlc.state import ApprovalResponse
from agentic_sdlc.streamlit_runtime import (
    StreamlitOperationKind,
    StreamlitRuntimeView,
)
from tests.test_streamlit_runtime import _snapshot, _task_graph_snapshot


def _render_for_test(runtime: object) -> None:
    from agentic_sdlc.streamlit_app import render_app

    render_app(runtime)  # type: ignore[arg-type]


class FakeUIRuntime:
    """Mutable test double held outside Streamlit session state."""

    def __init__(self, snapshot: GovernedRunSnapshot | None = None) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=snapshot,
            operation_id=None,
            operation_kind=None,
            in_flight=False,
            error_message=None,
        )
        self.start_calls: list[tuple[str, GovernedRunRequest]] = []
        self.resume_calls: list[tuple[str, str, ApprovalResponse, str]] = []

    def poll(self) -> StreamlitRuntimeView:
        return self.view

    def schedule_start(
        self,
        operation_id: str,
        request: GovernedRunRequest,
    ) -> bool:
        self.start_calls.append((operation_id, request))
        self.view = StreamlitRuntimeView(
            snapshot=None,
            operation_id=operation_id,
            operation_kind=StreamlitOperationKind.START,
            in_flight=True,
            error_message=None,
        )
        return True

    def schedule_resume(
        self,
        operation_id: str,
        run_id: str,
        response: ApprovalResponse,
        *,
        gate_token: str,
    ) -> bool:
        self.resume_calls.append((operation_id, run_id, response, gate_token))
        self.view = StreamlitRuntimeView(
            snapshot=self.view.snapshot,
            operation_id=operation_id,
            operation_kind=StreamlitOperationKind.RESUME,
            in_flight=True,
            error_message=None,
        )
        return True

    def complete(self, snapshot: GovernedRunSnapshot) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=snapshot,
            operation_id=self.view.operation_id,
            operation_kind=self.view.operation_kind,
            in_flight=False,
            error_message=None,
        )


def _app_for(runtime: FakeUIRuntime) -> AppTest:
    return AppTest.from_function(_render_for_test, args=(runtime,)).run()


def _values(elements: object) -> list[str]:
    return [str(element.value) for element in elements]  # type: ignore[attr-defined]


def test_actual_streamlit_entrypoint_imports_and_renders_initial_screen() -> None:
    app_path = (
        Path(__file__).parents[1]
        / "src"
        / "agentic_sdlc"
        / "streamlit_app.py"
    )
    app = AppTest.from_file(app_path).run()

    assert app.exception == []
    assert _values(app.title) == ["Agentic SDLC Orchestrator"]
    assert _values(app.text_area) == [""]
    assert app.text_area[0].label == "Software requirement"
    assert app.text_input[0].label == "Project name (optional)"
    assert [button.label for button in app.button] == ["Analyze Requirement"]
    assert app.file_uploader == []


def test_whitespace_requirement_is_rejected_before_start_side_effects() -> None:
    runtime = FakeUIRuntime()
    app = _app_for(runtime)

    app.text_area[0].input("  \t\n ")
    app.button[0].click().run()

    assert runtime.start_calls == []
    assert any("non-whitespace" in value for value in _values(app.error))


def test_submission_uses_inline_evidence_boundary_and_schedules_start_once() -> None:
    runtime = FakeUIRuntime()
    app = _app_for(runtime)
    original = "\ufeff  Build a notes API.\r\n\r\n- Store notes.  "

    app.text_area[0].input(original)
    app.button[0].click().run()

    assert len(runtime.start_calls) == 1
    request = runtime.start_calls[0][1]
    evidence = request.workflow_input["requirement_submission"]
    assert evidence["source_kind"] == "inline"
    assert evidence["original_text"] == original
    assert evidence["normalized_text"] == "Build a notes API.\n\n- Store notes."
    assert request.workflow_input["project_name"].startswith("project-")
    assert any("Analyzing requirement" in value for value in _values(app.info))

    app.run()
    assert len(runtime.start_calls) == 1


def test_explicit_project_name_uses_authoritative_normalization_in_ui() -> None:
    runtime = FakeUIRuntime()
    app = _app_for(runtime)

    app.text_area[0].input("Build a notes API.")
    app.text_input[0].input("  My Notes API  ")
    app.button[0].click().run()

    assert len(runtime.start_calls) == 1
    request = runtime.start_calls[0][1]
    assert request.requested_project_name == "  My Notes API  "
    assert request.workflow_input["project_name"] == "my-notes-api"


def test_requirement_gate_renders_authoritative_analysis_and_allowed_decisions(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path,
        allowed_decisions=("REQUEST_CHANGES", "REJECT"),
    )
    runtime = FakeUIRuntime(snapshot)

    app = _app_for(runtime)

    assert app.exception == []
    assert "Requirement Analysis" in _values(app.header)
    assert any(
        "revision-0: Build a scheduler." in value for value in _values(app.markdown)
    )
    assert any("Agent/LLM proposed" in value for value in _values(app.warning))
    assert [metric.label for metric in app.metric] == [
        "Revision",
        "Planning readiness",
        "Confidence",
    ]
    assert app.metric[1].value == "READY"
    assert app.radio[0].options == ["Request changes", "Reject and safely stop"]
    assert all("Approve" not in option for option in app.radio[0].options)
    assert any(
        snapshot.human_gate.gate_token in value for value in _values(app.caption)
    )


def test_request_changes_requires_feedback_then_passes_exact_text_and_token(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    gate_token = snapshot.human_gate.gate_token

    app.radio[0].set_value("Request changes")
    app.text_area[0].input(" \t\n ")
    app.button[0].click().run()

    assert runtime.resume_calls == []
    assert any("provide feedback" in value for value in _values(app.error))

    meaningful_feedback = "  Keep leading context.\nPreserve this line.  "
    app.radio[0].set_value("Request changes")
    app.text_area[0].input(meaningful_feedback)
    app.button[0].click().run()

    assert len(runtime.resume_calls) == 1
    _, run_id, response, submitted_token = runtime.resume_calls[0]
    assert run_id == snapshot.run_id
    assert submitted_token == gate_token
    assert response == {
        "decision": "REQUEST_CHANGES",
        "feedback": meaningful_feedback,
    }
    assert any("Applying the human decision" in value for value in _values(app.info))

    app.run()
    assert len(runtime.resume_calls) == 1


def test_revised_analysis_uses_new_gate_token_and_revision_context(
    tmp_path: Path,
) -> None:
    original = _snapshot(tmp_path)
    revised = _snapshot(
        tmp_path,
        gate_token="run-streamlit:human-gate:2",
        revision=1,
    )
    analysis_history = revised.workflow_state[
        "requirement_analysis_history"
    ]
    analysis_history.insert(  # type: ignore[attr-defined]
        0,
        {
            "revision_number": 0,
            "attempt_number": 1,
            "reviewer_feedback": "",
        },
    )
    revised.workflow_state["requirement_review_history"] = [  # type: ignore[index]
        {
            "sequence": 1,
            "decision": "REQUEST_CHANGES",
            "revision_number": 0,
            "feedback": "Clarify the schedule policy.",
        }
    ]
    runtime = FakeUIRuntime(original)
    runtime.complete(revised)

    app = _app_for(runtime)

    assert any(
        "revision-1: Build a scheduler." in value for value in _values(app.markdown)
    )
    assert app.metric[0].value == "1"
    assert any(
        revised.human_gate.gate_token in value for value in _values(app.caption)
    )
    assert all(
        original.human_gate.gate_token not in value for value in _values(app.caption)
    )
    assert "Revision history (2 analyses)" in [
        expander.label for expander in app.expander
    ]


def test_requirement_approve_reaches_interactive_task_graph_without_auto_decision(
    tmp_path: Path,
) -> None:
    requirement_gate = _snapshot(tmp_path)
    runtime = FakeUIRuntime(requirement_gate)
    app = _app_for(runtime)

    app.radio[0].set_value("Approve and continue")
    app.button[0].click().run()

    assert len(runtime.resume_calls) == 1
    assert runtime.resume_calls[0][2] == {"decision": "APPROVE", "feedback": ""}
    assert runtime.resume_calls[0][3] == requirement_gate.human_gate.gate_token

    task_graph_gate = _task_graph_snapshot(
        tmp_path,
        gate_token="run-streamlit:human-gate:2",
    )
    runtime.complete(task_graph_gate)
    app.run()

    assert "TaskGraph Review" in _values(app.header)
    assert app.radio[0].label == "TaskGraph decision"
    assert app.button[0].label == "Submit TaskGraph Decision"
    assert all("next GUI slice" not in value for value in _values(app.info))
    assert len(runtime.resume_calls) == 1


def test_task_graph_review_renders_authoritative_visual_metadata_and_details(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)

    app = _app_for(runtime)

    assert app.exception == []
    assert [metric.label for metric in app.metric] == [
        "TaskGraph revision",
        "Canonical tasks",
        "Execution layers",
        "Synchronization points",
    ]
    assert [metric.value for metric in app.metric] == ["0", "4", "3", "1"]
    captions = _values(app.caption)
    assert any("GRAPH-DEMO-V001" in value for value in captions)
    assert any("SPEC-DEMO-V001" in value for value in captions)
    assert any("Source Requirement Analysis revision: 2" in value for value in captions)
    assert any("RUNNABLE_PROJECT" in value for value in captions)

    mermaid = next(
        value for value in _values(app.markdown) if "flowchart LR" in value
    )
    assert 'ENTRY(["ENTRY"])' in mermaid
    assert 'EXIT(["EXIT"])' in mermaid
    for task_id in ("TASK-001", "TASK-002", "TASK-003", "TASK-004"):
        assert mermaid.count(task_id) == 1

    subheaders = _values(app.subheader)
    assert "Layer 1" in subheaders
    assert "Layer 2 — parallel" in subheaders
    assert "Layer 3" in subheaders
    assert any(
        value == "Parallel tasks: TASK-002, TASK-003"
        for value in _values(app.info)
    )
    expander_labels = [expander.label for expander in app.expander]
    assert "TASK-001 — Define API contract" in expander_labels
    assert "TASK-002 — Implement shortener" in expander_labels
    assert "TASK-003 — Build validation suite" in expander_labels
    assert "TASK-004 — Publish run guide" in expander_labels
    assert "TaskGraph governance history (1 generated graphs)" in expander_labels

    text_values = _values(app.text)
    assert "Depends on: ENTRY" in text_values
    assert "Depends on: TASK-002, TASK-003" in text_values
    assert "FR-002 — Redirect a short code." in text_values
    assert "NFR-001 — Short-code lookup is reliable." in text_values
    assert "AC-002 — A known code redirects correctly." in text_values
    assert "RISK-001 — Code collisions can misdirect users." in text_values
    assert "AMB-001 — Expiration behavior is unspecified." in text_values
    assert "Expected outputs: src/url_shortener/app.py" in text_values
    assert "Deliverable roles: RUNNABLE_ENTRYPOINT" in text_values
    assert "Graph generation 1: revision 0, attempt 1" in text_values
    assert "Prompt: task-planning-v1.3 · Model: fake-task-planner" in text_values


def test_task_graph_decisions_come_only_from_authoritative_allowed_decisions(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(
        tmp_path,
        allowed_decisions=("REQUEST_CHANGES", "REJECT"),
    )

    app = _app_for(FakeUIRuntime(snapshot))

    assert app.radio[0].options == [
        "Request TaskGraph changes",
        "Reject TaskGraph and safely stop",
    ]
    assert all("Approve" not in option for option in app.radio[0].options)


def test_task_graph_request_changes_validates_and_preserves_exact_feedback(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)

    app.radio[0].set_value("Request TaskGraph changes")
    app.text_area[0].input(" \t\n ")
    app.button[0].click().run()

    assert runtime.resume_calls == []
    assert any("provide feedback" in value for value in _values(app.error))

    feedback = "  Preserve this context.\nAdd a validation task.  "
    app.radio[0].set_value("Request TaskGraph changes")
    app.text_area[0].input(feedback)
    app.button[0].click().run()

    assert len(runtime.resume_calls) == 1
    _, run_id, response, gate_token = runtime.resume_calls[0]
    assert run_id == snapshot.run_id
    assert gate_token == snapshot.human_gate.gate_token
    assert response == {"decision": "REQUEST_CHANGES", "feedback": feedback}
    assert any(
        "generating a revised TaskGraph" in value for value in _values(app.info)
    )
    app.run()
    assert len(runtime.resume_calls) == 1


def test_revised_task_graph_renders_new_authority_and_prior_review_history(
    tmp_path: Path,
) -> None:
    old_token = "run-streamlit:human-gate:2"
    new_token = "run-streamlit:human-gate:3"
    feedback = "Add explicit validation before the run guide."
    revised = _task_graph_snapshot(
        tmp_path,
        gate_token=new_token,
        revision=1,
        title_suffix=" revised",
        prior_feedback=feedback,
    )
    runtime = FakeUIRuntime(revised)

    app = _app_for(runtime)

    assert app.metric[0].value == "1"
    assert any("GRAPH-DEMO-V002" in value for value in _values(app.caption))
    assert any(new_token in value for value in _values(app.caption))
    assert all(old_token not in value for value in _values(app.caption))
    assert "TASK-002 — Implement shortener revised" in [
        expander.label for expander in app.expander
    ]
    text_values = _values(app.text)
    assert "Graph generation 2: revision 1, attempt 1" in text_values
    assert f"Human feedback used: {feedback}" in text_values
    assert "Human decision 1: REQUEST_CHANGES on revision 0" in text_values
    assert f"Decision feedback: {feedback}" in text_values

    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    assert runtime.resume_calls[0][3] == new_token
    assert runtime.resume_calls[0][2] == {"decision": "APPROVE", "feedback": ""}


def test_task_graph_approve_uses_current_token_and_shows_execution_status(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)

    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    assert len(runtime.resume_calls) == 1
    assert runtime.resume_calls[0][2] == {"decision": "APPROVE", "feedback": ""}
    assert runtime.resume_calls[0][3] == snapshot.human_gate.gate_token
    assert any(
        "TaskGraph approved. Executing the governed engineering workflow"
        in value
        for value in _values(app.info)
    )
    app.run()
    assert len(runtime.resume_calls) == 1


def test_task_graph_reject_uses_governed_resume_and_renders_safe_stop(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)

    app.radio[0].set_value("Reject TaskGraph and safely stop")
    app.button[0].click().run()

    assert runtime.resume_calls[0][2] == {"decision": "REJECT", "feedback": ""}
    assert runtime.resume_calls[0][3] == snapshot.human_gate.gate_token

    stopped = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SAFE_STOPPED,
        workflow_status="safe_stopped",
    )
    stopped.workflow_state["safe_stop_reason"] = (  # type: ignore[index]
        "Engineering task graph rejected by human."
    )
    runtime.complete(stopped)
    app.run()

    assert any("stopped safely" in value for value in _values(app.warning))
    assert any(
        "Engineering task graph rejected by human" in value
        for value in _values(app.markdown)
    )
    assert app.button == []


def test_reject_schedules_resume_and_renders_safe_stopped_result(
    tmp_path: Path,
) -> None:
    requirement_gate = _snapshot(tmp_path)
    runtime = FakeUIRuntime(requirement_gate)
    app = _app_for(runtime)

    app.radio[0].set_value("Reject and safely stop")
    app.button[0].click().run()

    assert runtime.resume_calls[0][2] == {"decision": "REJECT", "feedback": ""}
    stopped = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SAFE_STOPPED,
        workflow_status="safe_stopped",
    )
    stopped.workflow_state["safe_stop_reason"] = (  # type: ignore[index]
        "Requirement analysis rejected by human."
    )
    runtime.complete(stopped)
    app.run()

    assert any("stopped safely" in value for value in _values(app.warning))
    assert any(
        "Requirement analysis rejected by human" in value
        for value in _values(app.markdown)
    )
    assert app.button == []
