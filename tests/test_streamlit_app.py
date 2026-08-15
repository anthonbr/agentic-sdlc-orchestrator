"""Streamlit AppTest coverage for the first governed GUI vertical slice."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from agentic_sdlc.application import (
    EligibleBrownfieldProject,
    GovernedRunApplicationStatus,
    GovernedRunMode,
    GovernedRunRequest,
    GovernedRunSnapshot,
)
from agentic_sdlc.clarification_draft import (
    ClarificationDrafter,
    ClarificationDraftError,
    ClarificationDraftRequest,
    ClarificationDraftResult,
    FakeClarificationDrafter,
)
from agentic_sdlc.project_export import (
    ProjectExportResult,
    ProjectExportStatus,
    ProjectExportValidation,
)
from agentic_sdlc.state import ApprovalResponse
from agentic_sdlc.streamlit_execution_progress import (
    StreamlitExecutionProgressCollector,
    StreamlitExecutionProgressView,
)
from agentic_sdlc.streamlit_runtime import (
    ClarificationDraftBackgroundRuntime,
    ClarificationDraftRuntimeView,
    StreamlitOperationKind,
    StreamlitRuntimeView,
)
from agentic_sdlc.task_execution_progress import (
    GovernedTaskExecutionStarted,
    TaskExecutionAttemptSettled,
    TaskExecutionAttemptStarted,
    TaskExecutionHeartbeat,
    TaskExecutionProgressAttempt,
    TaskExecutionSettledOutcome,
    TaskExecutionWaveMode,
    TaskExecutionWaveStarted,
)
from tests.test_streamlit_runtime import QueuedExecutor, _snapshot, _task_graph_snapshot


def _render_for_test(runtime: object, drafter: object | None = None) -> None:
    from agentic_sdlc.streamlit_app import render_app

    render_app(runtime, clarification_drafter=drafter)  # type: ignore[arg-type]


class FakeUIRuntime:
    """Mutable test double held outside Streamlit session state."""

    def __init__(
        self,
        snapshot: GovernedRunSnapshot | None = None,
        *,
        eligible_projects: tuple[EligibleBrownfieldProject, ...] = (),
    ) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=snapshot,
            operation_id=None,
            operation_kind=None,
            in_flight=False,
            error_message=None,
        )
        self.start_calls: list[tuple[str, GovernedRunRequest]] = []
        self.resume_calls: list[tuple[str, str, ApprovalResponse, str]] = []
        self.eligible_projects = eligible_projects
        self.list_eligible_calls = 0
        self.clarification_executor = QueuedExecutor()
        self.clarification_runtime = ClarificationDraftBackgroundRuntime(
            executor=self.clarification_executor
        )

    def list_eligible_brownfield_projects(
        self,
    ) -> tuple[EligibleBrownfieldProject, ...]:
        self.list_eligible_calls += 1
        return self.eligible_projects

    def poll(self) -> StreamlitRuntimeView:
        return self.view

    def schedule_clarification_draft(
        self,
        generation_id: str,
        context_identity: str,
        request: ClarificationDraftRequest,
        drafter: ClarificationDrafter,
    ) -> bool:
        return self.clarification_runtime.schedule(
            generation_id,
            context_identity,
            request,
            drafter,
        )

    def poll_clarification_draft(
        self,
        context_identity: str,
    ) -> ClarificationDraftRuntimeView:
        return self.clarification_runtime.poll(context_identity)

    def settle_clarification_draft(self) -> None:
        self.clarification_executor.run_next()

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
            operation_elapsed_seconds=0.0,
            execution_progress=self.view.execution_progress,
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
            operation_elapsed_seconds=0.0,
            execution_progress=self.view.execution_progress,
        )
        return True

    def complete(self, snapshot: GovernedRunSnapshot) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=snapshot,
            operation_id=self.view.operation_id,
            operation_kind=self.view.operation_kind,
            in_flight=False,
            error_message=None,
            operation_elapsed_seconds=None,
            execution_progress=self.view.execution_progress,
        )

    def set_execution_progress(
        self,
        progress: StreamlitExecutionProgressView,
    ) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=self.view.snapshot,
            operation_id=self.view.operation_id,
            operation_kind=self.view.operation_kind,
            in_flight=self.view.in_flight,
            error_message=self.view.error_message,
            operation_elapsed_seconds=self.view.operation_elapsed_seconds,
            execution_progress=progress,
        )

    def set_operation_elapsed(self, seconds: float) -> None:
        self.view = StreamlitRuntimeView(
            snapshot=self.view.snapshot,
            operation_id=self.view.operation_id,
            operation_kind=self.view.operation_kind,
            in_flight=self.view.in_flight,
            error_message=self.view.error_message,
            operation_elapsed_seconds=seconds,
            execution_progress=self.view.execution_progress,
        )


def _app_for(
    runtime: FakeUIRuntime,
    drafter: FakeClarificationDrafter | None = None,
) -> AppTest:
    return AppTest.from_function(_render_for_test, args=(runtime, drafter)).run()


def _values(elements: object) -> list[str]:
    return [str(element.value) for element in elements]  # type: ignore[attr-defined]


def _button(app: AppTest, label: str) -> Any:
    return next(button for button in app.button if button.label == label)


def _text_area(app: AppTest, label: str) -> Any:
    return next(area for area in app.text_area if area.label == label)


def _execution_collector(
    snapshot: GovernedRunSnapshot,
    *,
    operation_id: str = "task-graph-operation",
) -> StreamlitExecutionProgressCollector:
    gate = snapshot.human_gate
    assert gate is not None
    collector = StreamlitExecutionProgressCollector()
    collector.attach_run(snapshot.run_id)
    assert collector.begin_execution(
        run_id=snapshot.run_id,
        operation_id=operation_id,
        candidate_task_graph=gate.payload["candidate_task_graph"],
        graph_semantics=gate.payload["graph_semantics"],
    )
    return collector


def _successful_export(destination: Path) -> ProjectExportResult:
    return ProjectExportResult(
        status=ProjectExportStatus.SUCCEEDED,
        requested_project_name="misleading-input-name",
        project_name=destination.name,
        export_root=destination.parent,
        destination_directory=destination,
        exported_file_count=3,
        packaged_artifact_file_count=2,
        validation=ProjectExportValidation(
            authoritative_snapshot_id="WORKSPACE-SNAPSHOT-STREAMLIT"
        ),
    )


def _eligible_project(
    project_name: str = "published-project",
) -> EligibleBrownfieldProject:
    return EligibleBrownfieldProject(
        project_name=project_name,
        originating_run_id="published-run",
        workflow_project_name="Published Project",
        source_snapshot_id="WORKSPACE-SNAPSHOT-SOURCE",
        engineering_file_count=4,
        publication_bundle_sha256="a" * 64,
    )


def _as_brownfield_snapshot(
    snapshot: GovernedRunSnapshot,
    *,
    baseline_name: str = "published-project",
    output_name: str = "enhanced-project",
) -> GovernedRunSnapshot:
    impact = {
        "baseline_id": "BROWNFIELD-BASELINE-TEST",
        "codebase_context_id": "BROWNFIELD-CONTEXT-TEST",
        "impacted_modules": [
            {
                "target": "src/service.py",
                "reason": "The existing service must implement the requested change.",
            }
        ],
        "impacted_services": [],
        "impacted_apis": [],
        "impacted_state": [],
        "impacted_flows": [],
        "impacted_tests": [
            {
                "target": "tests/test_service.py",
                "reason": "Regression coverage must preserve existing behavior.",
            }
        ],
        "impacted_documentation": [],
        "architectural_implications": [],
        "preserved_behaviors": [
            {
                "target": "existing behavior",
                "reason": "Unaffected behavior must remain backward compatible.",
            }
        ],
    }
    baseline = {
        "baseline_id": "BROWNFIELD-BASELINE-TEST",
        "selected_project_name": baseline_name,
        "originating_run_id": "published-run",
        "source_snapshot_id": "WORKSPACE-SNAPSHOT-SOURCE",
        "engineering_files": [
            {"path": "README.md", "content_hash": "a" * 64},
            {"path": "src/service.py", "content_hash": "b" * 64},
            {"path": "tests/test_service.py", "content_hash": "c" * 64},
        ],
    }
    context = {"context_id": "BROWNFIELD-CONTEXT-TEST"}
    workflow_state = dict(snapshot.workflow_state)
    workflow_state.update(
        {
            "project_name": output_name,
            "brownfield_baseline": baseline,
            "brownfield_codebase_context": context,
        }
    )
    gate = snapshot.human_gate
    if gate is not None:
        payload = dict(gate.payload)
        analysis = payload.get("requirement_analysis")
        if isinstance(analysis, dict):
            analysis = dict(analysis)
            analysis["requirement_type"] = "brownfield"
            analysis["brownfield_impact"] = impact
            payload["requirement_analysis"] = analysis
            workflow_state["requirement_analysis"] = analysis
        spec = payload.get("approved_requirement_spec")
        if isinstance(spec, dict):
            spec = dict(spec)
            spec["requirement_type"] = "brownfield"
            spec["brownfield_impact"] = impact
            payload["approved_requirement_spec"] = spec
            workflow_state["approved_requirement_spec"] = spec
        gate = replace(gate, payload=payload)
    return replace(snapshot, human_gate=gate, workflow_state=workflow_state)


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
    assert app.radio[0].label == "What do you want to do?"
    assert app.radio[0].value == "Build a new project"
    assert _values(app.text_area) == [""]
    assert app.text_area[0].label == "Software requirement"
    assert app.text_input[0].label == "Project name (optional)"
    assert [button.label for button in app.button] == ["Analyze Requirement"]
    assert app.file_uploader == []


def test_brownfield_entry_uses_verified_logical_baseline_and_distinct_output() -> None:
    runtime = FakeUIRuntime(eligible_projects=(_eligible_project(),))
    app = _app_for(runtime)

    app.radio[0].set_value("Change an existing project").run()

    assert runtime.list_eligible_calls >= 1
    assert app.selectbox[0].label == "Existing project"
    assert app.selectbox[0].options == ["published-project"]
    assert _text_area(app, "Describe the change you want to make")
    assert app.text_input[0].label == "New project name"
    assert _button(app, "Analyze Change").disabled is False
    assert any(
        "preserve the original baseline" in value for value in _values(app.caption)
    )

    _text_area(app, "Describe the change you want to make").input(
        "Add expiration while preserving existing behavior."
    )
    app.text_input[0].input("enhanced-project")
    _button(app, "Analyze Change").click().run()

    assert len(runtime.start_calls) == 1
    request = runtime.start_calls[0][1]
    assert request.run_mode is GovernedRunMode.BROWNFIELD
    assert request.baseline_project_name == "published-project"
    assert request.requested_project_name == "enhanced-project"
    assert request.workflow_input["project_name"] == "enhanced-project"
    assert any(
        "Baseline: published-project" in value for value in _values(app.info)
    )


def test_brownfield_entry_without_eligible_projects_fails_closed_cleanly() -> None:
    runtime = FakeUIRuntime()
    app = _app_for(runtime)

    app.radio[0].set_value("Change an existing project").run()

    assert runtime.list_eligible_calls >= 1
    assert any(
        "No eligible published projects" in value for value in _values(app.info)
    )
    assert _button(app, "Analyze Change").disabled is True
    assert runtime.start_calls == []


def test_brownfield_entry_requires_explicit_new_project_name() -> None:
    runtime = FakeUIRuntime(eligible_projects=(_eligible_project(),))
    app = _app_for(runtime)
    app.radio[0].set_value("Change an existing project").run()

    _text_area(app, "Describe the change you want to make").input(
        "Change the existing service."
    )
    _button(app, "Analyze Change").click().run()

    assert runtime.start_calls == []
    assert any("new output project name" in value for value in _values(app.error))


def test_brownfield_start_error_is_presented_as_fail_closed() -> None:
    runtime = FakeUIRuntime()
    app = _app_for(runtime)
    app.session_state["agentic_sdlc_active_run_mode"] = "BROWNFIELD"
    runtime.view = replace(
        runtime.view,
        operation_kind=StreamlitOperationKind.START,
        error_message=(
            "Brownfield baseline preparation failed: reasoning limit exceeded."
        ),
    )

    app.run()

    assert any("reasoning limit exceeded" in value for value in _values(app.error))
    assert any("failed closed" in value for value in _values(app.caption))
    assert runtime.start_calls == []


def test_brownfield_resume_error_does_not_claim_setup_failed(
    tmp_path: Path,
) -> None:
    snapshot = _as_brownfield_snapshot(_snapshot(tmp_path))
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.session_state["agentic_sdlc_active_run_mode"] = "BROWNFIELD"
    runtime.view = replace(
        runtime.view,
        operation_id="resume-error",
        operation_kind=StreamlitOperationKind.RESUME,
        error_message="Later governed resume operation failed.",
    )

    app.run()

    assert any(
        "Later governed resume operation failed" in value
        for value in _values(app.error)
    )
    captions = _values(app.caption)
    assert all("Brownfield setup failed closed" not in value for value in captions)
    assert all(
        "No partial codebase context was accepted" not in value
        for value in captions
    )
    assert any(
        "Baseline: published-project" in value for value in _values(app.info)
    )


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
    assert "Analyzing Requirement" in _values(app.header)
    assert "Requirement Analysis Agent" in _values(app.subheader)
    assert any(
        "producing the first governed Requirement Analysis" in value
        for value in _values(app.markdown)
    )
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "00:00"
    )

    runtime.set_operation_elapsed(17.9)
    app.run()
    assert len(runtime.start_calls) == 1
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "00:17"
    )


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
    assert "Brownfield Impact" not in _values(app.subheader)


def test_brownfield_requirement_review_renders_baseline_and_structured_impact(
    tmp_path: Path,
) -> None:
    snapshot = _as_brownfield_snapshot(_snapshot(tmp_path))
    app = _app_for(FakeUIRuntime(snapshot))

    assert app.exception == []
    assert any(
        "Baseline: published-project" in value for value in _values(app.info)
    )
    assert "Brownfield Impact" in _values(app.subheader)
    markdown = _values(app.markdown)
    assert any("src/service.py" in value for value in markdown)
    assert any("existing service must implement" in value for value in markdown)
    assert any("tests/test_service.py" in value for value in markdown)
    assert any("backward compatible" in value for value in markdown)
    assert "Baseline provenance" in [item.label for item in app.expander]
    assert "Brownfield impact provenance" in [
        item.label for item in app.expander
    ]


def test_blocked_brownfield_review_keeps_clarification_governance(
    tmp_path: Path,
) -> None:
    snapshot = _as_brownfield_snapshot(_snapshot(tmp_path, blocked=True))
    runtime = FakeUIRuntime(snapshot)
    drafter = FakeClarificationDrafter(
        [ClarificationDraftResult(suggested_clarification="Preserve compatibility.")]
    )
    app = _app_for(runtime, drafter)

    assert "Brownfield Impact" in _values(app.subheader)
    _button(app, "Draft clarification response").click().run()

    assert drafter.calls == []
    assert len(runtime.clarification_executor.jobs) == 1
    assert "Requirement Analysis" in _values(app.header)
    assert "Brownfield Impact" in _values(app.subheader)
    assert any(
        "Baseline: published-project" in value for value in _values(app.info)
    )
    assert any(
        "Drafting clarification response" in value for value in _values(app.info)
    )
    assert all(area.label != "Software requirement" for area in app.text_area)
    app.run()
    assert drafter.calls == []
    assert len(runtime.clarification_executor.jobs) == 1
    assert _button(app, "Draft clarification response").disabled is True

    runtime.settle_clarification_draft()
    app.run()

    assert len(drafter.calls) == 1
    assert runtime.resume_calls == []
    assert _text_area(app, "Suggested clarification").value == (
        "Preserve compatibility."
    )


def test_blocked_analysis_draft_survives_rerun_without_governed_transition(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, blocked=True)
    runtime = FakeUIRuntime(snapshot)
    drafter = FakeClarificationDrafter(
        [
            ClarificationDraftResult(
                suggested_clarification=(
                    "Retain completed jobs for 30 days.\n"
                    "Authentication is not required for this prototype."
                )
            )
        ]
    )
    app = _app_for(runtime, drafter)

    _button(app, "Draft clarification response").click().run()
    assert drafter.calls == []
    runtime.settle_clarification_draft()
    app.run()

    assert len(drafter.calls) == 1
    request = drafter.calls[0]
    assert request.run_id == snapshot.run_id
    assert request.gate_token == snapshot.human_gate.gate_token
    assert request.analysis_revision == 0
    assert request.original_requirement == "  Build a scheduler for recurring jobs.  "
    assert request.planning_readiness.blocking_ambiguities == (
        "How long should scheduled jobs be retained?",
        "Is authentication required for this prototype?",
    )
    assert runtime.resume_calls == []
    assert _text_area(app, "Suggested clarification").value.startswith(
        "Retain completed jobs"
    )

    app.run()

    assert len(drafter.calls) == 1
    assert runtime.resume_calls == []
    assert _button(app, "Regenerate draft") is not None


def test_edited_draft_adoption_only_populates_feedback_until_explicit_submit(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path, blocked=True)
    runtime = FakeUIRuntime(snapshot)
    drafter = FakeClarificationDrafter(
        [ClarificationDraftResult(suggested_clarification="Initial suggestion.")]
    )
    app = _app_for(runtime, drafter)
    _button(app, "Draft clarification response").click().run()
    runtime.settle_clarification_draft()
    app.run()

    edited = (
        "Retain completed jobs for 14 days.\n"
        "Authentication is not required for the prototype."
    )
    _text_area(app, "Suggested clarification").input(edited).run()
    assert runtime.resume_calls == []

    _button(app, "Use this draft").click().run()

    assert _text_area(app, "Review feedback").value == edited
    assert runtime.resume_calls == []

    app.radio[0].set_value("Request changes")
    _button(app, "Submit Decision").click().run()

    assert len(runtime.resume_calls) == 1
    assert runtime.resume_calls[0][2] == {
        "decision": "REQUEST_CHANGES",
        "feedback": edited,
    }


def test_existing_human_feedback_requires_explicit_draft_replacement(
    tmp_path: Path,
) -> None:
    runtime = FakeUIRuntime(_snapshot(tmp_path, blocked=True))
    drafter = FakeClarificationDrafter(
        [ClarificationDraftResult(suggested_clarification="Generated answer.")]
    )
    app = _app_for(runtime, drafter)
    _text_area(app, "Review feedback").input("Human-authored feedback.").run()

    _button(app, "Draft clarification response").click().run()
    runtime.settle_clarification_draft()
    app.run()

    assert _text_area(app, "Review feedback").value == "Human-authored feedback."
    assert _button(app, "Replace feedback with this draft") is not None
    assert runtime.resume_calls == []

    _button(app, "Replace feedback with this draft").click().run()

    assert _text_area(app, "Review feedback").value == "Generated answer."
    assert runtime.resume_calls == []


def test_new_requirement_gate_invalidates_stale_clarification_draft(
    tmp_path: Path,
) -> None:
    original = _snapshot(tmp_path, blocked=True)
    revised = _snapshot(
        tmp_path,
        gate_token="run-streamlit:human-gate:2",
        revision=1,
        blocked=True,
    )
    runtime = FakeUIRuntime(original)
    drafter = FakeClarificationDrafter(
        [
            ClarificationDraftResult(suggested_clarification="Old revision answer."),
            ClarificationDraftResult(suggested_clarification="New revision answer."),
        ]
    )
    app = _app_for(runtime, drafter)
    _button(app, "Draft clarification response").click().run()
    assert drafter.calls == []

    runtime.complete(revised)
    app.run()

    assert "Requirement Analysis" in _values(app.header)
    assert _button(app, "Draft clarification response").disabled is True
    runtime.settle_clarification_draft()
    app.run()

    assert all(area.label != "Suggested clarification" for area in app.text_area)
    assert all(button.label != "Use this draft" for button in app.button)
    assert _button(app, "Draft clarification response") is not None
    assert len(drafter.calls) == 1
    assert runtime.resume_calls == []

    _button(app, "Draft clarification response").click().run()
    runtime.settle_clarification_draft()
    app.run()

    assert _text_area(app, "Suggested clarification").value == "New revision answer."
    assert len(drafter.calls) == 2
    assert runtime.resume_calls == []


def test_draft_provider_failure_preserves_feedback_and_never_resumes(
    tmp_path: Path,
) -> None:
    runtime = FakeUIRuntime(_snapshot(tmp_path, blocked=True))
    drafter = FakeClarificationDrafter(
        [ClarificationDraftError("provider unavailable")]
    )
    app = _app_for(runtime, drafter)
    _text_area(app, "Review feedback").input("Keep this human feedback.").run()

    _button(app, "Draft clarification response").click().run()
    assert drafter.calls == []
    runtime.settle_clarification_draft()
    app.run()

    assert runtime.resume_calls == []
    assert _text_area(app, "Review feedback").value == "Keep this human feedback."
    assert any("provider unavailable" in value for value in _values(app.error))
    assert all(area.label != "Suggested clarification" for area in app.text_area)
    assert _button(app, "Draft clarification response").disabled is False


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
    assert "Re-analyzing Requirement" in _values(app.header)
    assert "Requirement Analysis Agent" in _values(app.subheader)
    assert any(
        "producing a revised authoritative Requirement Analysis" in value
        for value in _values(app.markdown)
    )
    runtime.set_operation_elapsed(402.75)

    app.run()
    assert len(runtime.resume_calls) == 1
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "06:42"
    )

    revised = _snapshot(
        tmp_path,
        gate_token="run-streamlit:human-gate:2",
        revision=1,
    )
    runtime.complete(revised)
    app.run()
    assert "Requirement Analysis" in _values(app.header)
    assert "Re-analyzing Requirement" not in _values(app.header)
    assert all(metric.label != "Elapsed" for metric in app.metric)


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
    assert "Planning Engineering Work" in _values(app.header)
    assert "Task Planning Agent" in _values(app.subheader)
    assert any(
        "deterministically validating the canonical TaskGraph" in value
        for value in _values(app.markdown)
    )
    runtime.set_operation_elapsed(38.0)
    app.run()
    assert len(runtime.resume_calls) == 1
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "00:38"
    )

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
    snapshot = _task_graph_snapshot(
        tmp_path,
        required_validation_task_id="TASK-003",
    )
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
    assert "Required validations: PYTHON_COMPILE" in text_values
    assert "Graph generation 1: revision 0, attempt 1" in text_values
    assert "Prompt: task-planning-v1.4 · Model: fake-task-planner" in text_values


def test_brownfield_task_graph_review_preserves_incremental_context(
    tmp_path: Path,
) -> None:
    snapshot = _as_brownfield_snapshot(_task_graph_snapshot(tmp_path))
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)

    assert any(
        "Planning incremental changes to baseline published-project" in value
        for value in _values(app.info)
    )
    assert any(
        "publication as enhanced-project" in value for value in _values(app.info)
    )
    assert "Approved Brownfield Impact" in [
        item.label for item in app.expander
    ]
    assert app.radio[0].label == "TaskGraph decision"
    assert app.button[0].label == "Submit TaskGraph Decision"

    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    assert "Engineering Execution" in _values(app.header)
    assert any(
        "Baseline: published-project" in value for value in _values(app.info)
    )


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
    assert "Revising TaskGraph" in _values(app.header)
    assert "Task Planning Agent" in _values(app.subheader)
    assert any(
        "producing a revised canonical TaskGraph" in value
        for value in _values(app.markdown)
    )
    runtime.set_operation_elapsed(27.0)
    app.run()
    assert len(runtime.resume_calls) == 1
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "00:27"
    )


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


def test_task_graph_approve_uses_current_token_and_shows_execution_dashboard(
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
    assert "Engineering Execution" in _values(app.header)
    assert "Advancing Governed Workflow" not in _values(app.header)
    assert any(
        "Waiting for structured Task Agent execution telemetry" in value
        for value in _values(app.info)
    )
    assert all(
        "Executing the governed engineering workflow" not in value
        for value in _values(app.info)
    )
    app.run()
    assert len(runtime.resume_calls) == 1


def test_unknown_non_execution_phase_uses_generic_elapsed_fallback(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)

    app.radio[0].set_value("Reject and safely stop")
    app.button[0].click().run()
    app.session_state["agentic_sdlc_ui_phase"] = "future_governed_operation"
    runtime.set_operation_elapsed(9.0)
    app.run()

    assert len(runtime.resume_calls) == 1
    assert "Advancing Governed Workflow" in _values(app.header)
    assert "Requirement Analysis Agent" not in _values(app.subheader)
    assert "Task Planning Agent" not in _values(app.subheader)
    assert {metric.label: metric.value for metric in app.metric}["Elapsed"] == (
        "00:09"
    )


def test_explicit_streamlit_session_state_keys_remain_presentation_only() -> None:
    app_path = (
        Path(__file__).parents[1]
        / "src"
        / "agentic_sdlc"
        / "streamlit_app.py"
    )
    source = app_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    session_key_constants = {
        node.targets[0].id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.endswith("_KEY")
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }

    assert session_key_constants == {
        "_UI_PHASE_KEY": "agentic_sdlc_ui_phase",
        "_RUN_ID_KEY": "agentic_sdlc_current_run_id",
        "_OPERATION_ID_KEY": "agentic_sdlc_operation_id",
        "_CLARIFICATION_DRAFT_CONTEXT_KEY": (
            "agentic_sdlc_clarification_draft_context"
        ),
        "_CLARIFICATION_DRAFT_TEXT_KEY": "agentic_sdlc_clarification_draft_text",
        "_CLARIFICATION_DRAFT_APPLIED_GENERATION_KEY": (
            "agentic_sdlc_clarification_draft_applied_generation"
        ),
        "_ACTIVE_RUN_MODE_KEY": "agentic_sdlc_active_run_mode",
        "_ACTIVE_BASELINE_PROJECT_KEY": "agentic_sdlc_active_baseline_project",
        "_ACTIVE_OUTPUT_PROJECT_KEY": "agentic_sdlc_active_output_project",
        "_ENTRY_MODE_KEY": "agentic_sdlc_entry_mode",
    }
    assert "PublishedProjectCatalog" not in source
    assert "runtime.list_eligible_brownfield_projects()" in source


def test_live_execution_dashboard_shows_parallel_running_tasks_and_wave(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    operation_id = runtime.resume_calls[0][0]
    collector = _execution_collector(snapshot, operation_id=operation_id)
    attempts = (
        TaskExecutionProgressAttempt("TASK-002", 1, "event title ignored"),
        TaskExecutionProgressAttempt("TASK-003", 1, "event title ignored"),
    )
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=2,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=attempts,
        )
    )
    for attempt in attempts:
        collector.report(
            TaskExecutionAttemptStarted(wave_number=2, attempt=attempt)
        )
    collector.report(
        TaskExecutionHeartbeat(
            wave_number=2,
            outstanding_attempts=attempts,
        )
    )
    progress = collector.snapshot(run_id=snapshot.run_id)
    assert progress is not None
    runtime.set_execution_progress(progress)

    app.run()

    assert "Engineering Execution" in _values(app.header)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Progress status"] == "IN_PROGRESS"
    assert metrics["Completed tasks"] == "0 / 4"
    assert metrics["Current wave"] == "2 · PARALLEL"
    assert "Layer 2 — parallel" in _values(app.subheader)
    markdown = _values(app.markdown)
    assert any(
        "TASK-002 — Implement shortener" in value and "RUNNING" in value
        for value in markdown
    )
    assert any(
        "TASK-003 — Build validation suite" in value and "RUNNING" in value
        for value in markdown
    )
    assert any(
        "Scheduler waves remain distinct from canonical layers" in value
        for value in _values(app.caption)
    )
    assert len(runtime.resume_calls) == 1


def test_execution_dashboard_displays_retry_failure_and_unknown_task_warning(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    operation_id = runtime.resume_calls[0][0]
    collector = _execution_collector(snapshot, operation_id=operation_id)
    retry = TaskExecutionProgressAttempt("TASK-002", 1, "ignored")
    failed = TaskExecutionProgressAttempt("TASK-003", 1, "ignored")
    unknown = TaskExecutionProgressAttempt("TASK-999", 1, "untrusted")
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=2,
            mode=TaskExecutionWaveMode.PARALLEL,
            attempts=(retry, failed),
        )
    )
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=2,
            attempt=retry,
            outcome=TaskExecutionSettledOutcome.RETRY_SCHEDULED,
            detail="scheduled retry after validation",
        )
    )
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=2,
            attempt=failed,
            outcome=TaskExecutionSettledOutcome.FAILED,
            detail="terminally failed",
        )
    )
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=3,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(unknown,),
        )
    )
    progress = collector.snapshot(run_id=snapshot.run_id)
    assert progress is not None
    runtime.set_execution_progress(progress)

    app.run()

    markdown = _values(app.markdown)
    assert any("TASK-002" in value and "RETRY_SCHEDULED" in value for value in markdown)
    assert any("TASK-003" in value and "FAILED" in value for value in markdown)
    assert any("TASK-999" in value and "PREPARING" in value for value in markdown)
    assert any(
        "unknown canonical task ID: TASK-999" in value
        for value in _values(app.warning)
    )
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Retries / failed"] == "1 / 1"


def test_terminal_success_retains_final_execution_summary(tmp_path: Path) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()
    operation_id = runtime.resume_calls[0][0]
    collector = _execution_collector(snapshot, operation_id=operation_id)
    collector.report(GovernedTaskExecutionStarted())
    for wave_number, task_ids, mode in (
        (1, ("TASK-001",), TaskExecutionWaveMode.SINGLE),
        (2, ("TASK-002", "TASK-003"), TaskExecutionWaveMode.PARALLEL),
        (3, ("TASK-004",), TaskExecutionWaveMode.SINGLE),
    ):
        attempts = tuple(
            TaskExecutionProgressAttempt(task_id, 1, "ignored")
            for task_id in task_ids
        )
        collector.report(
            TaskExecutionWaveStarted(
                wave_number=wave_number,
                mode=mode,
                attempts=attempts,
            )
        )
        for attempt in attempts:
            collector.report(
                TaskExecutionAttemptStarted(
                    wave_number=wave_number,
                    attempt=attempt,
                )
            )
            collector.report(
                TaskExecutionAttemptSettled(
                    wave_number=wave_number,
                    attempt=attempt,
                    outcome=TaskExecutionSettledOutcome.SUCCEEDED,
                    detail="succeeded",
                )
            )
    assert collector.finish_execution(
        run_id=snapshot.run_id,
        operation_id=operation_id,
    )
    progress = collector.snapshot(run_id=snapshot.run_id)
    assert progress is not None
    runtime.set_execution_progress(progress)
    terminal = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
    )
    terminal = replace(
        terminal,
        export_result=_successful_export(
            Path(__file__).parents[1] / "projects" / "terminal-summary"
        ),
    )
    runtime.complete(terminal)

    app.run()

    assert any("completed successfully" in value for value in _values(app.success))
    assert "Engineering Execution" in _values(app.header)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Progress status"] == "OBSERVATION_COMPLETE"
    assert metrics["Completed tasks"] == "4 / 4"
    assert sum("SUCCEEDED" in value for value in _values(app.markdown)) == 4


def test_new_run_clears_stale_terminal_progress_and_binds_new_execution(
    tmp_path: Path,
) -> None:
    old_graph = _as_brownfield_snapshot(
        replace(_task_graph_snapshot(tmp_path), run_id="run-old"),
        baseline_name="old-baseline",
        output_name="old-output",
    )
    runtime = FakeUIRuntime(old_graph)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()
    old_operation_id = runtime.resume_calls[0][0]
    old_collector = _execution_collector(
        old_graph,
        operation_id=old_operation_id,
    )
    old_attempts = tuple(
        TaskExecutionProgressAttempt(task_id, 1, "ignored")
        for task_id in ("TASK-001", "TASK-002", "TASK-003", "TASK-004")
    )
    old_collector.report(
        TaskExecutionWaveStarted(
            wave_number=8,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=old_attempts,
        )
    )
    for attempt in old_attempts:
        old_collector.report(
            TaskExecutionAttemptSettled(
                wave_number=8,
                attempt=attempt,
                outcome=TaskExecutionSettledOutcome.SUCCEEDED,
                detail="old run succeeded",
            )
        )
    old_progress = old_collector.snapshot(run_id="run-old")
    assert old_progress is not None
    old_progress = replace(
        old_progress,
        completed_task_count=10,
        total_task_count=10,
    )
    runtime.set_execution_progress(old_progress)
    old_terminal = _as_brownfield_snapshot(
        _snapshot(
            tmp_path,
            run_id="run-old",
            gate_token=None,
            application_status=GovernedRunApplicationStatus.SUCCEEDED,
            workflow_status="success",
        ),
        baseline_name="old-baseline",
        output_name="old-output",
    )
    runtime.complete(
        replace(
            old_terminal,
            export_result=_successful_export(
                Path(__file__).parents[1] / "projects" / "old-project"
            ),
        )
    )
    app.run()
    assert {metric.label: metric.value for metric in app.metric}[
        "Completed tasks"
    ] == "10 / 10"
    assert any("Run ID: run-old" in value for value in _values(app.caption))
    assert any("Baseline: old-baseline" in value for value in _values(app.info))

    app.session_state["agentic_sdlc_clarification_draft_context"] = "old-context"
    app.session_state["agentic_sdlc_clarification_draft_text"] = "old draft"
    app.session_state["agentic_sdlc_clarification_draft_applied_generation"] = (
        "old-generation"
    )
    app.session_state["requirement_decision_feedback_old-gate"] = "old feedback"
    app.session_state["task_graph_decision_feedback_old-gate"] = "old graph feedback"
    app.session_state["agentic_sdlc_active_run_mode"] = "BROWNFIELD"
    app.session_state["agentic_sdlc_active_baseline_project"] = "old-baseline"
    app.session_state["agentic_sdlc_active_output_project"] = "old-output"
    app.session_state["unrelated_display_preference"] = "compact"
    runtime.view = StreamlitRuntimeView(
        snapshot=None,
        operation_id=None,
        operation_kind=None,
        in_flight=False,
        error_message=None,
        execution_progress=old_progress,
    )
    app.run()

    state = app.session_state.filtered_state
    assert state["agentic_sdlc_ui_phase"] == "requirement_entry"
    assert "agentic_sdlc_current_run_id" not in state
    assert "agentic_sdlc_operation_id" not in state
    assert "agentic_sdlc_clarification_draft_context" not in state
    assert "agentic_sdlc_clarification_draft_text" not in state
    assert "agentic_sdlc_clarification_draft_applied_generation" not in state
    assert "requirement_decision_feedback_old-gate" not in state
    assert "task_graph_decision_feedback_old-gate" not in state
    assert "agentic_sdlc_active_run_mode" not in state
    assert "agentic_sdlc_active_baseline_project" not in state
    assert "agentic_sdlc_active_output_project" not in state
    assert state["unrelated_display_preference"] == "compact"

    _text_area(app, "Software requirement").input("Build a smaller project.")
    app.text_input[0].input("new-project")
    _button(app, "Analyze Requirement").click().run()
    assert all("run-old" not in value for value in _values(app.caption))
    assert all(metric.value != "10 / 10" for metric in app.metric)
    assert all("old-baseline" not in value for value in _values(app.info))

    new_graph = replace(_task_graph_snapshot(tmp_path), run_id="run-new")
    runtime.complete(new_graph)
    app.run()
    assert app.session_state.filtered_state["agentic_sdlc_current_run_id"] == (
        "run-new"
    )
    assert all("run-old" not in value for value in _values(app.caption))
    assert all("old-baseline" not in value for value in _values(app.info))

    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()
    assert "Engineering Execution" in _values(app.header)
    assert any(
        "Waiting for structured Task Agent execution telemetry" in value
        for value in _values(app.info)
    )
    assert all(metric.value != "10 / 10" for metric in app.metric)

    new_operation_id = runtime.resume_calls[-1][0]
    new_collector = _execution_collector(
        new_graph,
        operation_id=new_operation_id,
    )
    new_collector.report(
        TaskExecutionWaveStarted(
            wave_number=1,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(TaskExecutionProgressAttempt("TASK-001", 1, "ignored"),),
        )
    )
    new_progress = new_collector.snapshot(run_id="run-new")
    assert new_progress is not None
    runtime.set_execution_progress(new_progress)
    app.run()
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Completed tasks"] == "0 / 4"
    assert metrics["Current wave"] == "1 · SINGLE"
    assert any("Run ID: run-new" in value for value in _values(app.caption))
    assert all("run-old" not in value for value in _values(app.caption))

    runtime.set_execution_progress(old_progress)
    stopped = _snapshot(
        tmp_path,
        run_id="run-new",
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SAFE_STOPPED,
        workflow_status="safe_stopped",
    )
    stopped.workflow_state["safe_stop_reason"] = "New run stopped safely."
    runtime.complete(stopped)
    app.run()
    assert any("stopped safely" in value for value in _values(app.warning))
    assert all("completed successfully" not in value for value in _values(app.success))
    assert all(metric.label != "Completed tasks" for metric in app.metric)


def test_progress_requires_current_operation_identity(tmp_path: Path) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()

    stale_operation = _execution_collector(
        snapshot,
        operation_id="prior-operation",
    )
    stale_operation.report(
        TaskExecutionWaveStarted(
            wave_number=8,
            mode=TaskExecutionWaveMode.SINGLE,
            attempts=(TaskExecutionProgressAttempt("TASK-001", 1, "ignored"),),
        )
    )
    progress = stale_operation.snapshot(run_id=snapshot.run_id)
    assert progress is not None
    runtime.set_execution_progress(progress)
    app.run()

    assert any(
        "Waiting for structured Task Agent execution telemetry" in value
        for value in _values(app.info)
    )
    assert all(metric.label != "Completed tasks" for metric in app.metric)
    assert all("prior-operation" not in value for value in _values(app.caption))


def test_stale_terminal_snapshot_is_not_rendered_for_active_run(
    tmp_path: Path,
) -> None:
    active = replace(_snapshot(tmp_path), run_id="run-active")
    runtime = FakeUIRuntime(active)
    app = _app_for(runtime)
    stale = _snapshot(
        tmp_path,
        run_id="run-stale",
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
    )
    runtime.complete(
        replace(
            stale,
            export_result=_successful_export(
                Path(__file__).parents[1] / "projects" / "stale-project"
            ),
        )
    )

    app.run()

    assert any("identity mismatch" in value for value in _values(app.error))
    assert all("completed successfully" not in value for value in _values(app.success))
    assert all("Published project" not in value for value in _values(app.success))
    assert app.session_state.filtered_state["agentic_sdlc_current_run_id"] == (
        "run-active"
    )


def test_success_displays_returned_publication_destination_and_run_id(
    tmp_path: Path,
) -> None:
    destination = Path(__file__).parents[1] / "projects" / "returned-destination"
    terminal = _snapshot(
        tmp_path,
        run_id="run-published",
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
    )
    terminal.workflow_state["project_name"] = "not-the-returned-destination"
    runtime = FakeUIRuntime(
        replace(terminal, export_result=_successful_export(destination))
    )

    app = _app_for(runtime)

    assert "Published project: projects/returned-destination" in _values(app.success)
    assert any(
        "Authoritative run ID: run-published" in value
        for value in _values(app.caption)
    )
    assert all(
        "not-the-returned-destination" not in value
        for value in _values(app.success)
    )


def test_brownfield_success_displays_baseline_to_authoritative_destination_lineage(
    tmp_path: Path,
) -> None:
    destination = Path(__file__).parents[1] / "projects" / "enhanced-project"
    terminal = _as_brownfield_snapshot(
        _snapshot(
            tmp_path,
            run_id="run-brownfield-published",
            gate_token=None,
            application_status=GovernedRunApplicationStatus.SUCCEEDED,
            workflow_status="success",
        )
    )
    app = _app_for(
        FakeUIRuntime(
            replace(terminal, export_result=_successful_export(destination))
        )
    )

    assert "Published project: projects/enhanced-project" in _values(app.success)
    assert any(
        "Baseline project published-project was preserved" in value
        for value in _values(app.info)
    )
    assert any(
        "Authoritative run ID: run-brownfield-published" in value
        for value in _values(app.caption)
    )


def test_success_without_verified_publication_destination_is_reported(
    tmp_path: Path,
) -> None:
    terminal = _snapshot(
        tmp_path,
        run_id="run-missing-publication",
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
    )

    app = _app_for(FakeUIRuntime(terminal))

    assert any(
        "no verified published-project destination" in value
        for value in _values(app.error)
    )
    assert all("Published project:" not in value for value in _values(app.success))


def test_terminal_safe_stop_keeps_failed_task_telemetry_and_authoritative_reason(
    tmp_path: Path,
) -> None:
    snapshot = _task_graph_snapshot(tmp_path)
    runtime = FakeUIRuntime(snapshot)
    app = _app_for(runtime)
    app.radio[0].set_value("Approve TaskGraph and execute")
    app.button[0].click().run()
    operation_id = runtime.resume_calls[0][0]
    collector = _execution_collector(snapshot, operation_id=operation_id)
    failed = TaskExecutionProgressAttempt("TASK-002", 2, "ignored")
    collector.report(GovernedTaskExecutionStarted())
    collector.report(
        TaskExecutionWaveStarted(
            wave_number=4,
            mode=TaskExecutionWaveMode.SERIALIZED_RETRY,
            attempts=(failed,),
        )
    )
    collector.report(
        TaskExecutionAttemptStarted(wave_number=4, attempt=failed)
    )
    collector.report(
        TaskExecutionAttemptSettled(
            wave_number=4,
            attempt=failed,
            outcome=TaskExecutionSettledOutcome.FAILED,
            detail="terminally failed",
        )
    )
    assert collector.finish_execution(
        run_id=snapshot.run_id,
        operation_id=operation_id,
    )
    progress = collector.snapshot(run_id=snapshot.run_id)
    assert progress is not None
    runtime.set_execution_progress(progress)
    stopped = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=GovernedRunApplicationStatus.SAFE_STOPPED,
        workflow_status="safe_stopped",
    )
    stopped.workflow_state["safe_stop_reason"] = (  # type: ignore[index]
        "Task TASK-002 terminally failed after bounded retries."
    )
    runtime.complete(stopped)

    app.run()

    assert any("stopped safely" in value for value in _values(app.warning))
    assert any(
        "Task TASK-002 terminally failed after bounded retries" in value
        for value in _values(app.markdown)
    )
    assert any(
        "TASK-002 — Implement shortener" in value and "FAILED" in value
        for value in _values(app.markdown)
    )
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Retries / failed"] == "0 / 1"


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
