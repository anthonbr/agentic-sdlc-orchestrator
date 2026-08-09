"""Behavior tests for the V0.2 orchestration workflow."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import agentic_sdlc.__main__ as cli
from pytest import CaptureFixture, MonkeyPatch

from agentic_sdlc.artifacts import (
    ARTIFACT_FILENAMES,
    SAFE_STOP_ARTIFACT_FILENAMES,
)
from agentic_sdlc.nodes import exit_gate, synchronize
from agentic_sdlc.state import (
    MAX_PLAN_REVISIONS,
    MAX_PLAN_REVISIONS_REASON,
    PLAN_REJECTED_REASON,
    ArchitectureArtifact,
    WorkflowState,
    demo_input,
)
from agentic_sdlc.workflow import resume_workflow, run_workflow


def _start_demo(artifact_dir: Path | None = None) -> tuple[str, WorkflowState]:
    thread_id = uuid4().hex
    state = run_workflow(
        demo_input(), thread_id=thread_id, artifact_dir=artifact_dir
    )
    assert state["workflow_status"] == "awaiting_approval"
    assert state.get("__interrupt__")
    return thread_id, state


def _approve_demo(artifact_dir: Path | None = None) -> WorkflowState:
    thread_id, _ = _start_demo(artifact_dir)
    return resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
    )


def test_immediate_approval_runs_existing_parallel_workflow() -> None:
    thread_id, paused = _start_demo()

    assert paused["entry_gate_passed"] is True
    assert paused["implementation_plan"]
    assert "architecture" not in paused
    assert "test_plan" not in paused
    assert "synchronize" not in " ".join(paused["trace"])

    result = resume_workflow(
        thread_id, {"decision": "APPROVE", "feedback": ""}
    )

    assert [requirement["id"] for requirement in result["normalized_requirements"]] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
        "REQ-004",
    ]
    assert [
        (item["source_requirement_id"], item["id"])
        for item in result["work_items"]
    ] == [
        ("REQ-001", "WI-001"),
        ("REQ-002", "WI-002"),
        ("REQ-003", "WI-003"),
        ("REQ-004", "WI-004"),
    ]
    assert result["implementation_plan"][1]["work_item_ids"] == ["WI-002"]
    assert result["architecture"]["components"]
    assert result["test_plan"]["cases"]
    assert result["synchronization_complete"] is True
    assert result["exit_gate_passed"] is True
    assert result["workflow_status"] == "success"
    assert result["approval_history"] == [
        {
            "sequence": 1,
            "checkpoint": "implementation_plan",
            "decision": "APPROVE",
            "feedback": "",
            "revision_number": 0,
        }
    ]


def test_request_changes_revises_plan_then_can_be_approved(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "revised-run"
    thread_id, paused = _start_demo(artifact_dir)
    original_plan = paused["implementation_plan"]
    feedback = "Add explicit API validation tasks."

    revised = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        artifact_dir=artifact_dir,
    )

    assert revised["workflow_status"] == "awaiting_approval"
    assert revised.get("__interrupt__")
    assert revised["plan_revision_count"] == 1
    assert len(revised["implementation_plan"]) == len(original_plan) + 1
    assert feedback in revised["implementation_plan"][-1]["action"]
    assert "architecture" not in revised
    assert "test_plan" not in revised
    assert revised["approval_history"][0]["decision"] == "REQUEST_CHANGES"
    assert revised["approval_history"][0]["feedback"] == feedback
    assert revised["approval_history"][0]["revision_number"] == 0
    assert not artifact_dir.exists()

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
    )

    assert result["workflow_status"] == "success"
    assert result["plan_revision_count"] == 1
    assert [event["decision"] for event in result["approval_history"]] == [
        "REQUEST_CHANGES",
        "APPROVE",
    ]
    assert [event["revision_number"] for event in result["approval_history"]] == [
        0,
        1,
    ]
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "1. REQUEST_CHANGES" in summary
    assert f"Feedback: {feedback}" in summary
    assert "2. APPROVE" in summary


def test_rejection_safe_stops_without_downstream_work(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "rejected-run"
    artifact_dir.mkdir()
    (artifact_dir / "architecture.md").write_text("stale", encoding="utf-8")
    (artifact_dir / "test_plan.md").write_text("stale", encoding="utf-8")
    thread_id, _ = _start_demo(artifact_dir)

    result = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": "Not ready to proceed."},
        artifact_dir=artifact_dir,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == PLAN_REJECTED_REASON
    assert "architecture" not in result
    assert "test_plan" not in result
    assert not any("synchronize" in event for event in result["trace"])
    assert result["approval_history"][0]["decision"] == "REJECT"
    assert result["approval_history"][0]["feedback"] == "Not ready to proceed."
    assert {path.name for path in artifact_dir.iterdir()} == set(
        SAFE_STOP_ARTIFACT_FILENAMES
    )
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "Workflow result: safe_stopped" in summary
    assert PLAN_REJECTED_REASON in summary
    assert "Downstream architecture and test planning did not run." in summary


def test_revision_limit_safe_stops_without_looping_forever() -> None:
    thread_id, state = _start_demo()

    for revision in range(1, MAX_PLAN_REVISIONS + 1):
        state = resume_workflow(
            thread_id,
            {
                "decision": "REQUEST_CHANGES",
                "feedback": f"Revision request {revision}",
            },
        )
        assert state["workflow_status"] == "awaiting_approval"
        assert state["plan_revision_count"] == revision
        assert state.get("__interrupt__")

    result = resume_workflow(
        thread_id,
        {
            "decision": "REQUEST_CHANGES",
            "feedback": "One request beyond the allowed revision count",
        },
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == MAX_PLAN_REVISIONS_REASON
    assert result["plan_revision_count"] == MAX_PLAN_REVISIONS
    assert len(result["approval_history"]) == MAX_PLAN_REVISIONS + 1
    assert result["approval_history"][-1]["feedback"].startswith("One request")
    assert "architecture" not in result
    assert "test_plan" not in result


def test_entry_gate_failure_stops_downstream_work(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "failed-run"
    invalid_input: WorkflowState = {
        "project_name": "",
        "requirements": ["Accept a long URL."],
    }

    result = run_workflow(
        invalid_input, thread_id=uuid4().hex, artifact_dir=artifact_dir
    )

    assert result["entry_gate_passed"] is False
    assert result["workflow_status"] == "entry_gate_failed"
    assert "work_items" not in result
    assert "implementation_plan" not in result
    assert "architecture" not in result
    assert "test_plan" not in result
    assert not result.get("__interrupt__")
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
    assert "implementation plan approval" in result["errors"][0]
    assert "successful synchronization" in result["errors"][0]


def test_successful_run_writes_reviewable_artifact_set(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "demo-run"

    result = _approve_demo(artifact_dir)

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
    assert "## Human Approval History" in summary
    assert "1. APPROVE" in summary
    assert "Revision: 0" in summary


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
    monkeypatch.setattr("builtins.input", lambda _prompt: "a")

    assert cli.main(["demo"]) == 0
    output = capsys.readouterr()
    assert "Warning: workflow diagram was not generated" in output.err
    assert "[implementation_plan_approval] approve" in output.out
    assert (tmp_path / "artifacts" / "demo-run" / "summary.md").exists()
