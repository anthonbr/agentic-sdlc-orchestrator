"""Behavior tests for the V0.1 orchestration workflow."""

from __future__ import annotations

import json
from pathlib import Path

import agentic_sdlc.__main__ as cli
from pytest import CaptureFixture, MonkeyPatch

from agentic_sdlc.artifacts import ARTIFACT_FILENAMES
from agentic_sdlc.nodes import exit_gate, synchronize
from agentic_sdlc.state import ArchitectureArtifact, WorkflowState, demo_input
from agentic_sdlc.workflow import run_workflow


def test_successful_workflow_produces_all_validated_outputs() -> None:
    result = run_workflow(demo_input())

    assert result["entry_gate_passed"] is True
    assert [requirement["id"] for requirement in result["normalized_requirements"]] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
        "REQ-004",
    ]
    assert len(result["work_items"]) == 4
    assert [
        (item["source_requirement_id"], item["id"])
        for item in result["work_items"]
    ] == [
        ("REQ-001", "WI-001"),
        ("REQ-002", "WI-002"),
        ("REQ-003", "WI-003"),
        ("REQ-004", "WI-004"),
    ]
    assert result["implementation_plan"]
    assert result["implementation_plan"][1]["work_item_ids"] == ["WI-002"]
    assert result["implementation_plan"][5]["work_item_ids"] == [
        "WI-001",
        "WI-002",
        "WI-003",
        "WI-004",
    ]
    assert result["architecture"]["components"]
    assert result["test_plan"]["cases"]
    assert result["synchronization_complete"] is True
    assert result["exit_gate_passed"] is True
    assert result["workflow_status"] == "success"


def test_entry_gate_failure_stops_downstream_work(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "failed-run"
    invalid_input: WorkflowState = {
        "project_name": "",
        "requirements": ["Accept a long URL."],
    }

    result = run_workflow(invalid_input, artifact_dir=artifact_dir)

    assert result["entry_gate_passed"] is False
    assert result["workflow_status"] == "entry_gate_failed"
    assert "work_items" not in result
    assert "implementation_plan" not in result
    assert "architecture" not in result
    assert "test_plan" not in result
    assert not any("decompose_requirements" in event for event in result["trace"])
    assert not artifact_dir.exists()


def test_synchronization_requires_both_branch_outputs() -> None:
    architecture: ArchitectureArtifact = {
        "summary": "Present",
        "components": ["API layer"],
        "design_notes": [],
    }
    incomplete_state: WorkflowState = {"architecture": architecture, "errors": []}

    result = synchronize(incomplete_state)

    assert result["synchronization_complete"] is False
    assert result["workflow_status"] == "synchronization_failed"
    assert "test plan" in result["errors"][0]


def test_exit_gate_rejects_incomplete_state() -> None:
    incomplete_state: WorkflowState = {
        "entry_gate_passed": True,
        "normalized_requirements": [{"id": "REQ-001", "text": "A requirement"}],
        "errors": [],
    }

    result = exit_gate(incomplete_state)

    assert result["exit_gate_passed"] is False
    assert result["workflow_status"] == "exit_gate_failed"
    assert "implementation plan" in result["errors"][0]
    assert "successful synchronization" in result["errors"][0]


def test_successful_run_writes_reviewable_artifact_set(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "demo-run"

    result = run_workflow(demo_input(), artifact_dir=artifact_dir)

    assert result["workflow_status"] == "success"
    assert {path.name for path in artifact_dir.iterdir()} == set(ARTIFACT_FILENAMES)

    requirements = json.loads(
        (artifact_dir / "requirements.json").read_text(encoding="utf-8")
    )
    assert requirements["project_name"] == "URL Shortener"
    assert requirements["submitted_requirements"][0] == "Accept a long URL."
    assert requirements["normalized_requirements"][0] == {
        "id": "REQ-001",
        "text": "Accept a long URL.",
    }

    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "Workflow result: success" in summary
    assert "Entry gate: passed" in summary
    assert "Exit gate: passed" in summary


def test_workflow_diagram_writer_uses_compiled_graph(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\nexample"

    class StubGraph:
        def draw_mermaid_png(self) -> bytes:
            return png_bytes

    class StubWorkflow:
        def get_graph(self) -> StubGraph:
            return StubGraph()

    monkeypatch.setattr(cli, "WORKFLOW", StubWorkflow())
    diagram_path = tmp_path / "artifacts" / "workflow_diagram.png"

    cli.write_workflow_diagram(diagram_path)

    assert diagram_path.read_bytes() == png_bytes


def test_diagram_failure_does_not_fail_demo(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail_to_render(_output_path: Path) -> None:
        raise RuntimeError("renderer unavailable")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "write_workflow_diagram", fail_to_render)

    assert cli.main(["demo"]) == 0
    assert "Warning: workflow diagram was not generated" in capsys.readouterr().err
    assert (tmp_path / "artifacts" / "demo-run" / "summary.md").exists()
