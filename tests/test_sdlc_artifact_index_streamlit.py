"""Streamlit coverage for the terminal SDLC artifact index."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunSnapshot,
)
from agentic_sdlc.project_export import (
    ProjectExportResult,
    ProjectExportStatus,
    ProjectExportValidation,
)
from agentic_sdlc.run_artifacts import (
    LiveRunArtifactBundle,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.sdlc_document_models import SDLC_PDF_FILENAMES
from tests.test_run_artifacts import _terminal_state
from tests.test_streamlit_app import FakeUIRuntime, _render_for_test, _values
from tests.test_streamlit_runtime import _snapshot


def _app_for(snapshot: GovernedRunSnapshot) -> AppTest:
    return AppTest.from_function(
        _render_for_test,
        args=(FakeUIRuntime(snapshot), None),
    ).run(timeout=10)


def _terminal_with_manifest(
    tmp_path: Path,
    *,
    application_status: GovernedRunApplicationStatus = (
        GovernedRunApplicationStatus.SUCCEEDED
    ),
    workflow_status: str = "success",
) -> GovernedRunSnapshot:
    snapshot = _snapshot(
        tmp_path,
        gate_token=None,
        application_status=application_status,
        workflow_status=workflow_status,
    )
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, snapshot.run_id)
    bundle.artifact_dir.mkdir(parents=True)
    files = {
        "summary.md": b"# Final summary\n",
        "task_graph.json": b"{}\n",
        "requirements.json": b"{}\n",
        "task_graph.md": b"# TaskGraph\n",
        "human_governance_history.md": b"# Human Governance History\n",
    }
    if workflow_status == "success":
        files.update(
            {
                filename: b"%PDF-1.4\n" + b"x" * 600
                for filename in SDLC_PDF_FILENAMES
            }
        )
    for name, contents in files.items():
        (bundle.artifact_dir / name).write_bytes(contents)
    manifest_path = write_sdlc_artifact_manifest(
        _terminal_state(
            snapshot.run_id,
            workflow_status=(
                "safe_stopped" if workflow_status == "safe_stopped" else "success"
            ),
            exit_gate_passed=workflow_status == "success",
        ),
        bundle,
    )
    destination = tmp_path / "projects" / "artifact-index-project"
    export_result = (
        ProjectExportResult(
            status=ProjectExportStatus.SUCCEEDED,
            requested_project_name=destination.name,
            project_name=destination.name,
            export_root=destination.parent,
            destination_directory=destination,
            exported_file_count=1,
            packaged_artifact_file_count=len(files) + 1,
            validation=ProjectExportValidation(
                authoritative_snapshot_id="WORKSPACE-SNAPSHOT-INDEX"
            ),
        )
        if workflow_status == "success"
        else None
    )
    return replace(
        snapshot,
        artifact_bundle=bundle,
        manifest_path=manifest_path,
        export_result=export_result,
    )


def test_terminal_run_renders_lifecycle_ordered_artifact_downloads(
    tmp_path: Path,
) -> None:
    app = _app_for(_terminal_with_manifest(tmp_path))

    assert app.exception == []
    assert "SDLC Evidence & Artifacts" in _values(app.header)
    artifact_names = [
        value
        for value in _values(app.text)
        if value
        in {
            "requirements.json",
            "task_graph.md",
            "task_graph.json",
            "human_governance_history.md",
            "summary.md",
            "manifest.json",
        }
    ]
    assert artifact_names == [
        "requirements.json",
        "task_graph.md",
        "task_graph.json",
        "human_governance_history.md",
        "summary.md",
        "manifest.json",
    ]
    assert [button.label for button in app.download_button] == ["Download"] * 10
    assert [
        value
        for value in _values(app.text)
        if value
        in {
            "Requirements Specification",
            "Functional Specification",
            "Design Specification",
            "Test Plan and Validation Report",
        }
    ] == [
        "Requirements Specification",
        "Functional Specification",
        "Design Specification",
        "Test Plan and Validation Report",
    ]
    captions = _values(app.caption)
    assert all(filename in captions for filename in SDLC_PDF_FILENAMES)
    rendered = "\n".join(_values(app.markdown))
    assert "Original and normalized requirement submission." in rendered
    assert "Human decisions, feedback, AI assistance" in rendered
    assert "Integrity-bound inventory of retained evidence." in rendered


def test_active_review_does_not_render_terminal_artifact_index(
    tmp_path: Path,
) -> None:
    active = _snapshot(tmp_path)

    app = _app_for(active)

    assert "SDLC Evidence & Artifacts" not in _values(app.header)
    assert app.download_button == []


def test_safe_stopped_run_with_finalized_manifest_renders_artifact_index(
    tmp_path: Path,
) -> None:
    stopped = _terminal_with_manifest(
        tmp_path,
        application_status=GovernedRunApplicationStatus.SAFE_STOPPED,
        workflow_status="safe_stopped",
    )

    app = _app_for(stopped)

    assert app.exception == []
    assert "SDLC Evidence & Artifacts" in _values(app.header)
    assert len(app.download_button) == 6


def test_failed_evidence_finalization_does_not_render_artifact_index(
    tmp_path: Path,
) -> None:
    terminal = _terminal_with_manifest(tmp_path)
    failed = replace(
        terminal,
        application_status=GovernedRunApplicationStatus.FAILED,
        application_error="Terminal evidence finalization failed.",
    )

    app = _app_for(failed)

    assert "SDLC Evidence & Artifacts" not in _values(app.header)
    assert app.download_button == []
