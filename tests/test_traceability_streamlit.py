"""Light Streamlit coverage for terminal traceability presentation."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunMode,
    GovernedRunRequest,
    GovernedRunSnapshot,
)
from agentic_sdlc.llm import FakeTaskPlanningClient
from agentic_sdlc.project_export import (
    ProjectExportResult,
    ProjectExportStatus,
    ProjectExportValidation,
)
from agentic_sdlc.run_artifacts import LiveRunArtifactBundle
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_graph import ProposedTaskGraph, TaskMaterializationPolicy
from tests.test_application import _service
from tests.test_brownfield_baseline import _publish_project
from tests.test_brownfield_reasoning import _ContextAwareAnalyst
from tests.test_governed_validation_workflow import (
    ScriptedValidationExecutor,
    _run as _run_compile,
)
from tests.test_streamlit_app import FakeUIRuntime, _render_for_test, _values
from tests.test_task_execution_workflow import (
    DeterministicExecutor,
    MaterializingExecutor,
    _run_approved,
    _task,
)
from tests.test_workflow import _proposal as _runnable_proposal


def _verified_export(state: WorkflowState, destination: Path) -> ProjectExportResult:
    final_snapshot_id = state[
        "project_readiness_validation"
    ].final_workspace_snapshot_id
    assert final_snapshot_id is not None
    return ProjectExportResult(
        status=ProjectExportStatus.SUCCEEDED,
        requested_project_name=destination.name,
        project_name=destination.name,
        export_root=destination.parent,
        destination_directory=destination,
        exported_file_count=1,
        packaged_artifact_file_count=1,
        validation=ProjectExportValidation(
            authoritative_snapshot_id=final_snapshot_id,
            pre_export_snapshot_id=final_snapshot_id,
            staged_snapshot_id=final_snapshot_id,
            post_export_snapshot_id=final_snapshot_id,
            source_matches_authority=True,
            export_matches_authority=True,
            artifact_bundle_sha256="a" * 64,
            staged_artifact_bundle_sha256="a" * 64,
            post_export_artifact_bundle_sha256="a" * 64,
            evidence_source_valid=True,
            staged_evidence_matches=True,
            post_export_evidence_matches=True,
        ),
    )


def _terminal_snapshot(tmp_path: Path, state: WorkflowState) -> GovernedRunSnapshot:
    run_id = state["run_id"]
    destination = tmp_path / "projects" / "traceability-project"
    return GovernedRunSnapshot(
        run_id=run_id,
        application_status=GovernedRunApplicationStatus.SUCCEEDED,
        workflow_status="success",
        human_gate=None,
        workflow_state=state,
        artifact_bundle=LiveRunArtifactBundle.under_repository(tmp_path, run_id),
        workflow_diagram_generated=True,
        manifest_path=tmp_path / "manifest.json",
        export_result=_verified_export(state, destination),
        application_error=None,
        warnings=(),
    )


def _app_for(runtime: FakeUIRuntime) -> AppTest:
    return AppTest.from_function(
        _render_for_test,
        args=(runtime, None),
    ).run(timeout=10)


def test_terminal_summary_renders_compact_traceability_table_and_details(
    tmp_path: Path,
) -> None:
    state = _run_compile(ScriptedValidationExecutor())
    snapshot = _terminal_snapshot(tmp_path, state)

    app = _app_for(FakeUIRuntime(snapshot))

    assert app.exception == []
    assert "Requirement-to-Code Traceability" in _values(app.header)
    assert len(app.dataframe) == 1
    table = app.dataframe[0].value
    assert list(table["Requirement / AC"].str.split(" — ").str[0]) == [
        "FR-001",
        "AC-001",
    ]
    assert list(table["Status"]) == ["VERIFIED", "VERIFIED"]
    assert all(
        "src/candidate.py · CREATE" in value
        for value in table["Implementation"]
    )
    assert all("PYTHON_COMPILE · PASS" in value for value in table["Validation"])
    assert any(
        expander.label.startswith("FR-001 —") for expander in app.expander
    )
    subheaders = _values(app.subheader)
    assert "Traceability status" in subheaders
    assert "Generated artifact" in subheaders
    assert "Files changed" in subheaders
    assert "Validation performed" in subheaders
    assert "Run completion evidence" in subheaders
    assert "Technical evidence" in subheaders
    markdown = _values(app.markdown)
    assert any(
        "Implemented and explicitly linked to successful governed validation"
        in value
        for value in markdown
    )
    assert any(
        "Implemented, but successful validation cannot be explicitly traced"
        in value
        for value in markdown
    )
    assert any(
        "No authoritative implementation outcome is traceable" in value
        for value in markdown
    )
    assert any(
        "VERIFIED — Implementation and validation are both traceable" in value
        for value in _values(app.success)
    )
    assert any("semantic-TASK-001" in value for value in _values(app.text))
    assert any("src/candidate.py — CREATE" in value for value in _values(app.text))
    assert not any(
        "Missing links remain visible and are not inferred from names, prose, or "
        "semantic similarity." in value
        for value in _values(app.caption)
    )
    assert not any("traceability" in button.label.casefold() for button in app.button)


def test_terminal_details_make_missing_validation_visible(tmp_path: Path) -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "unverified",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )
    state = _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "src/unverified.py"}),
    )
    snapshot = _terminal_snapshot(tmp_path, state)

    app = _app_for(FakeUIRuntime(snapshot))

    table = app.dataframe[0].value
    assert set(table["Status"]) == {"UNVERIFIED"}
    assert set(table["Validation"]) == {"No qualifying governed validation"}
    assert any(
        "No qualifying governed execution evidence linked" in value
        for value in _values(app.text)
    )
    assert any("GOVERNED_VALIDATION" in value for value in _values(app.text))
    assert any(
        "UNVERIFIED — Implemented, validation not proven" in value
        for value in _values(app.warning)
    )
    assert any(
        "successful governed validation is not explicitly linked to this item"
        in value
        for value in _values(app.markdown)
    )


def test_terminal_details_explain_not_implemented_without_hiding_gaps(
    tmp_path: Path,
) -> None:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "non_materializing",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.ALLOWED,
            )
        ]
    )
    state = _run_approved(proposal, DeterministicExecutor())

    app = _app_for(FakeUIRuntime(_terminal_snapshot(tmp_path, state)))

    assert set(app.dataframe[0].value["Status"]) == {"NOT_IMPLEMENTED"}
    assert any(
        "NOT_IMPLEMENTED — No implementation outcome is traceable" in value
        for value in _values(app.info)
    )
    assert any("IMPLEMENTATION_LINEAGE" in value for value in _values(app.text))


@pytest.fixture
def brownfield_terminal(tmp_path: Path) -> GovernedRunSnapshot:
    _publish_project(tmp_path)
    service, _, _ = _service(
        tmp_path,
        analyst=_ContextAwareAnalyst(blocked_revisions=(False,)),
        planner=FakeTaskPlanningClient([_runnable_proposal()]),
        run_suffix="traceability-streamlit-brownfield",
    )
    requirement_review = service.start_run(
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            requested_project_name="enhanced-project",
            run_mode=GovernedRunMode.BROWNFIELD,
            baseline_project_name="published-project",
        )
    )
    assert requirement_review.human_gate is not None
    graph_review = service.resume_run(
        requirement_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_review.human_gate.gate_token,
    )
    assert graph_review.human_gate is not None
    return service.resume_run(
        graph_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_review.human_gate.gate_token,
    )


def test_brownfield_terminal_summary_renders_verified_run_level_lineage(
    brownfield_terminal: GovernedRunSnapshot,
) -> None:
    before = repr(brownfield_terminal.workflow_state)

    app = _app_for(FakeUIRuntime(brownfield_terminal))

    assert any(
        expander.label == "Brownfield baseline → governed outcome lineage"
        for expander in app.expander
    )
    texts = _values(app.text)
    assert any(
        "Selected baseline publication: published-project" in value
        for value in texts
    )
    assert any("New published project: enhanced-project" in value for value in texts)
    assert any("Approved impact analysis" in value for value in texts)
    assert any(
        "The approved impact analysis is traceable to the overall plan, but "
        "individual impact findings are not yet traceable to specific tasks."
        in value
        for value in _values(app.caption)
    )
    assert any(
        "Missing links are shown explicitly rather than inferred" in value
        for value in _values(app.caption)
    )
    assert not any("V0.16" in value for value in _values(app.caption))
    assert repr(brownfield_terminal.workflow_state) == before
