"""Behavior tests for the governed V0.3 orchestration workflow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import agentic_sdlc.__main__ as cli
from pydantic import ValidationError
from pytest import CaptureFixture, MonkeyPatch, raises

from agentic_sdlc.artifacts import (
    ARTIFACT_FILENAMES,
    SAFE_STOP_ARTIFACT_FILENAMES,
)
from agentic_sdlc.llm import (
    FakeRequirementAnalysisClient,
    OpenAIRequirementAnalysisClient,
    RequirementAnalysisClientError,
)
from agentic_sdlc.nodes import exit_gate, synchronize
from agentic_sdlc.prompts import (
    REQUIREMENT_ANALYSIS_PROMPT_VERSION,
    REQUIREMENT_ANALYSIS_SYSTEM_PROMPT,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.state import (
    MAX_PLAN_REVISIONS,
    MAX_PLAN_REVISIONS_REASON,
    MAX_REQUIREMENT_ANALYSIS_ATTEMPTS,
    MAX_REQUIREMENT_REVISIONS,
    MAX_REQUIREMENT_REVISIONS_REASON,
    PLAN_REJECTED_REASON,
    REQUIREMENT_ANALYSIS_REJECTED_REASON,
    ArchitectureArtifact,
    WorkflowState,
    demo_input,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow


def _analysis(version: str = "v1") -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement=(
            f"{version}: Provide short URLs that resolve to submitted long URLs."
        ),
        requirement_type="greenfield",
        functional_requirements=[
            "Accept a long URL.",
            "Generate a unique short URL.",
            "Redirect the short URL to the original URL.",
            "Return an error for unknown short URLs.",
        ],
        nonfunctional_requirements=["Short-code lookup should be reliable."],
        constraints=["The persistence technology is not yet selected."],
        ambiguities=["Repeated-submission identity behavior is unspecified."],
        assumptions=["The prototype analyzes requirements but builds no service."],
        acceptance_criteria=[
            "A submitted valid URL receives a unique short URL.",
            "An unknown short code returns a defined error.",
        ],
        risks=["Short-code collisions could produce incorrect redirects."],
        needs_clarification=True,
        confidence=0.85,
    )


def _interrupt_stage(state: WorkflowState) -> str:
    return state["__interrupt__"][0].value["stage"]


def _start_demo(
    artifact_dir: Path | None = None,
    *,
    client: FakeRequirementAnalysisClient | None = None,
) -> tuple[Any, str, WorkflowState, FakeRequirementAnalysisClient]:
    analyst = client or FakeRequirementAnalysisClient([_analysis()])
    workflow = build_workflow(analyst)
    thread_id = uuid4().hex
    state = run_workflow(
        demo_input(),
        thread_id=thread_id,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    assert state["workflow_status"] == "awaiting_approval"
    assert _interrupt_stage(state) == "requirement_analysis_review"
    return workflow, thread_id, state, analyst


def _approve_requirements(
    workflow: Any,
    thread_id: str,
    *,
    artifact_dir: Path | None = None,
) -> WorkflowState:
    state = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    assert state["workflow_status"] == "awaiting_approval"
    assert _interrupt_stage(state) == "implementation_plan_review"
    return state


def _approve_demo(artifact_dir: Path | None = None) -> WorkflowState:
    workflow, thread_id, _, _ = _start_demo(artifact_dir)
    _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)
    return resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )


def test_valid_analysis_is_stored_before_requirement_review() -> None:
    _, _, paused, analyst = _start_demo()

    assert paused["entry_gate_passed"] is True
    assert paused["requirement_analysis_status"] == "validated"
    assert paused["requirement_analysis"]["normalized_problem_statement"].startswith(
        "v1:"
    )
    assert paused["requirement_analysis_attempt_count"] == 1
    assert paused["requirement_analysis_history"][0]["prompt_version"] == (
        REQUIREMENT_ANALYSIS_PROMPT_VERSION
    )
    assert paused["requirement_analysis_history"][0]["model_name"] == (
        "fake-requirement-analyst"
    )
    assert len(analyst.calls) == 1
    assert "work_items" not in paused
    assert "implementation_plan" not in paused


def test_requirement_approval_reaches_existing_plan_approval() -> None:
    workflow, thread_id, _, _ = _start_demo()

    paused = _approve_requirements(workflow, thread_id)

    assert paused["requirement_review_decision"] == "APPROVE"
    assert paused["requirement_review_history"][0]["checkpoint"] == (
        "requirement_analysis"
    )
    assert paused["implementation_plan"]
    assert "architecture" not in paused
    assert "test_plan" not in paused


def test_requirement_changes_preserve_feedback_and_analysis_lineage(
    tmp_path: Path,
) -> None:
    original_proposal = _analysis("v1").model_copy(
        update={"assumptions": ["An expired short URL returns an error."]}
    )
    revised_proposal = _analysis("v2").model_copy(
        update={
            "ambiguities": [
                "Do shortened URLs expire?",
                "If expiration is supported, what happens when one is requested?",
            ],
            "assumptions": [],
        }
    )
    analyst = FakeRequirementAnalysisClient(
        [original_proposal, revised_proposal]
    )
    artifact_dir = tmp_path / "analysis-revised"
    workflow, thread_id, paused, _ = _start_demo(
        artifact_dir, client=analyst
    )
    original_analysis = paused["requirement_analysis"]
    feedback = (
        "Treat URL expiration behavior as an unresolved ambiguity and do not assume\n"
        "whether shortened URLs expire."
    )

    revised = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert revised["workflow_status"] == "awaiting_approval"
    assert _interrupt_stage(revised) == "requirement_analysis_review"
    assert revised["requirement_analysis_revision_count"] == 1
    assert revised["requirement_analysis"][
        "normalized_problem_statement"
    ].startswith("v2:")
    assert [
        record["revision_number"]
        for record in revised["requirement_analysis_history"]
    ] == [0, 1]
    assert revised["requirement_analysis_history"][1]["reviewer_feedback"] == feedback
    assert revised["requirement_review_history"][0]["decision"] == (
        "REQUEST_CHANGES"
    )
    assert revised["requirement_review_history"][0]["feedback"] == feedback
    prior_analysis = analyst.calls[1]["prior_analysis"]
    assert isinstance(prior_analysis, RequirementAnalysis)
    assert prior_analysis.model_dump(mode="json") == original_analysis
    assert analyst.calls[1]["human_feedback"] == feedback
    assert revised["requirement_analysis"]["ambiguities"] == [
        "Do shortened URLs expire?",
        "If expiration is supported, what happens when one is requested?",
    ]
    assert revised["requirement_analysis"]["assumptions"] == []
    assert "implementation_plan" not in revised

    _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)
    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "success"
    artifact = (artifact_dir / "requirement_analysis.md").read_text(
        encoding="utf-8"
    )
    assert "Normalized problem: v1:" in artifact
    assert "Normalized problem: v2:" in artifact
    assert f"Reviewer feedback: {feedback}" in artifact


def test_requirement_rejection_safe_stops_before_downstream_work(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "requirement-rejected"
    workflow, thread_id, _, _ = _start_demo(artifact_dir)

    result = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": "The requirement is not ready."},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == REQUIREMENT_ANALYSIS_REJECTED_REASON
    assert result["requirement_review_history"][0]["feedback"] == (
        "The requirement is not ready."
    )
    assert "work_items" not in result
    assert "implementation_plan" not in result
    assert "architecture" not in result
    assert "test_plan" not in result
    assert {path.name for path in artifact_dir.iterdir()} == {
        "requirements.json",
        "requirement_analysis.md",
        "summary.md",
    }


def test_requirement_revision_limit_safe_stops() -> None:
    analyst = FakeRequirementAnalysisClient(
        [_analysis(f"v{number}") for number in range(MAX_REQUIREMENT_REVISIONS + 1)]
    )
    workflow, thread_id, state, _ = _start_demo(client=analyst)

    for revision in range(1, MAX_REQUIREMENT_REVISIONS + 1):
        state = resume_workflow(
            thread_id,
            {
                "decision": "REQUEST_CHANGES",
                "feedback": f"Requirement revision {revision}",
            },
            workflow=workflow,
        )
        assert state["workflow_status"] == "awaiting_approval"
        assert state["requirement_analysis_revision_count"] == revision

    result = resume_workflow(
        thread_id,
        {
            "decision": "REQUEST_CHANGES",
            "feedback": "One change beyond the allowed revision count",
        },
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == MAX_REQUIREMENT_REVISIONS_REASON
    assert len(result["requirement_analysis_history"]) == (
        MAX_REQUIREMENT_REVISIONS + 1
    )
    assert len(result["requirement_review_history"]) == (
        MAX_REQUIREMENT_REVISIONS + 1
    )
    assert "implementation_plan" not in result


def test_invalid_structured_output_retries_then_reaches_review() -> None:
    invalid = _analysis().model_dump()
    invalid["functional_requirements"] = []
    analyst = FakeRequirementAnalysisClient([invalid, _analysis("v2")])

    _, _, paused, _ = _start_demo(client=analyst)

    assert paused["requirement_analysis_attempt_count"] == 2
    assert len(paused["requirement_analysis_failures"]) == 1
    assert "functional_requirements" in (
        paused["requirement_analysis_failures"][0]["reason"]
    )
    assert paused["requirement_analysis_history"][0]["attempt_number"] == 2
    assert len(analyst.calls) == 2


def test_transient_provider_failure_uses_same_bounded_retry_policy() -> None:
    analyst = FakeRequirementAnalysisClient(
        [
            RequirementAnalysisClientError(
                "Temporary provider outage.", retryable=True
            ),
            _analysis("recovered"),
        ]
    )

    _, _, paused, _ = _start_demo(client=analyst)

    assert paused["requirement_analysis_attempt_count"] == 2
    assert paused["requirement_analysis_failures"][0]["reason"] == (
        "Temporary provider outage."
    )
    assert paused["requirement_analysis"][
        "normalized_problem_statement"
    ].startswith("recovered:")


def test_analysis_retry_exhaustion_safe_stops_without_downstream_work(
    tmp_path: Path,
) -> None:
    invalid = {"normalized_problem_statement": "Missing required fields"}
    analyst = FakeRequirementAnalysisClient(
        [invalid for _ in range(MAX_REQUIREMENT_ANALYSIS_ATTEMPTS)]
    )
    workflow = build_workflow(analyst)
    artifact_dir = tmp_path / "analysis-failed"

    result = run_workflow(
        demo_input(),
        thread_id=uuid4().hex,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["requirement_analysis_attempt_count"] == (
        MAX_REQUIREMENT_ANALYSIS_ATTEMPTS
    )
    assert len(result["requirement_analysis_failures"]) == (
        MAX_REQUIREMENT_ANALYSIS_ATTEMPTS
    )
    assert "failed after 3 attempts" in result["safe_stop_reason"]
    assert len(analyst.calls) == MAX_REQUIREMENT_ANALYSIS_ATTEMPTS
    assert not result.get("__interrupt__")
    assert "work_items" not in result
    assert "implementation_plan" not in result
    assert {path.name for path in artifact_dir.iterdir()} == {
        "requirements.json",
        "summary.md",
    }
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "Requirement-analysis failures" in summary
    assert "failed after 3 attempts" in summary


def test_missing_api_key_safe_stops_without_network_call(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    workflow = build_workflow(OpenAIRequirementAnalysisClient(api_key=""))

    result = run_workflow(
        demo_input(), thread_id=uuid4().hex, workflow=workflow
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["requirement_analysis_attempt_count"] == 1
    assert result["requirement_analysis_failures"][0]["retryable"] is False
    assert result["safe_stop_reason"] == (
        "OPENAI_API_KEY is not configured; requirement analysis cannot run."
    )
    assert "implementation_plan" not in result


def test_openai_client_uses_sdk_structured_parse_without_network() -> None:
    calls: list[dict[str, Any]] = []
    feedback = (
        "Treat URL expiration behavior as an unresolved ambiguity and do not assume\n"
        "whether shortened URLs expire."
    )

    class StubResponses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=_analysis("sdk"))

    stub_client = SimpleNamespace(responses=StubResponses())
    client = OpenAIRequirementAnalysisClient(
        model_name="test-model", client=stub_client
    )

    result = client.invoke_structured(
        "Build a URL shortener.", _analysis("prior"), feedback
    )

    assert isinstance(result, RequirementAnalysis)
    assert calls[0]["model"] == "test-model"
    assert calls[0]["text_format"] is RequirementAnalysis
    assert "Prior validated analysis" in calls[0]["input"][1]["content"]
    assert "Authoritative human review feedback:" in (
        calls[0]["input"][1]["content"]
    )
    assert feedback in calls[0]["input"][1]["content"]


def test_requirement_prompt_makes_human_revision_feedback_authoritative() -> None:
    prompt = " ".join(REQUIREMENT_ANALYSIS_SYSTEM_PROMPT.casefold().split())

    assert REQUIREMENT_ANALYSIS_PROMPT_VERSION == "requirement-analysis-v1.1"
    assert "authoritative revision instruction" in prompt
    assert "asked to remove" in prompt
    assert "represent it as an ambiguity" in prompt
    assert "does not authorize inventing new requirements" in prompt


def test_openai_schema_parse_failure_is_retryable_without_network() -> None:
    with raises(ValidationError) as validation:
        RequirementAnalysis.model_validate({})
    schema_error = validation.value

    class StubResponses:
        def parse(self, **_kwargs: Any) -> None:
            raise schema_error

    client = OpenAIRequirementAnalysisClient(
        model_name="test-model",
        client=SimpleNamespace(responses=StubResponses()),
    )

    with raises(RequirementAnalysisClientError) as raised:
        client.invoke_structured("Build a URL shortener.", None, "")

    assert raised.value.retryable is True
    assert "failed schema parsing" in str(raised.value)


def test_immediate_plan_approval_runs_existing_parallel_workflow() -> None:
    workflow, thread_id, _, _ = _start_demo()
    paused = _approve_requirements(workflow, thread_id)

    assert paused["implementation_plan"]
    assert "architecture" not in paused
    assert "test_plan" not in paused
    assert "synchronize" not in " ".join(paused["trace"])

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )

    assert [requirement["id"] for requirement in result["normalized_requirements"]] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
        "REQ-004",
    ]
    assert [
        (item["source_requirement_id"], item["id"]) for item in result["work_items"]
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
    assert [event["decision"] for event in result["requirement_review_history"]] == [
        "APPROVE"
    ]
    assert [event["decision"] for event in result["approval_history"]] == [
        "APPROVE"
    ]
    assert result["approval_history"] == [
        {
            "sequence": 1,
            "checkpoint": "implementation_plan",
            "decision": "APPROVE",
            "feedback": "",
            "revision_number": 0,
        }
    ]


def test_plan_request_changes_revises_then_can_be_approved(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "revised-run"
    workflow, thread_id, _, _ = _start_demo(artifact_dir)
    paused = _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)
    original_plan = paused["implementation_plan"]
    feedback = "Add explicit API validation tasks."

    revised = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert revised["workflow_status"] == "awaiting_approval"
    assert _interrupt_stage(revised) == "implementation_plan_review"
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
        workflow=workflow,
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


def test_plan_rejection_safe_stops_without_parallel_work(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "plan-rejected"
    artifact_dir.mkdir()
    (artifact_dir / "architecture.md").write_text("stale", encoding="utf-8")
    (artifact_dir / "test_plan.md").write_text("stale", encoding="utf-8")
    workflow, thread_id, _, _ = _start_demo(artifact_dir)
    _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)

    result = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": "Not ready to proceed."},
        artifact_dir=artifact_dir,
        workflow=workflow,
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


def test_plan_revision_limit_safe_stops_without_looping_forever() -> None:
    workflow, thread_id, _, _ = _start_demo()
    state = _approve_requirements(workflow, thread_id)

    for revision in range(1, MAX_PLAN_REVISIONS + 1):
        state = resume_workflow(
            thread_id,
            {
                "decision": "REQUEST_CHANGES",
                "feedback": f"Revision request {revision}",
            },
            workflow=workflow,
        )
        assert state["workflow_status"] == "awaiting_approval"
        assert state["plan_revision_count"] == revision

    result = resume_workflow(
        thread_id,
        {
            "decision": "REQUEST_CHANGES",
            "feedback": "One request beyond the allowed revision count",
        },
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == MAX_PLAN_REVISIONS_REASON
    assert result["plan_revision_count"] == MAX_PLAN_REVISIONS
    assert len(result["approval_history"]) == MAX_PLAN_REVISIONS + 1
    assert result["approval_history"][-1]["feedback"].startswith("One request")
    assert "architecture" not in result
    assert "test_plan" not in result


def test_entry_gate_failure_stops_before_llm_or_downstream_work(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "failed-run"
    analyst = FakeRequirementAnalysisClient([_analysis()])
    workflow = build_workflow(analyst)
    invalid_input: WorkflowState = {
        "project_name": "",
        "requirements": ["Accept a long URL."],
    }

    result = run_workflow(
        invalid_input,
        thread_id=uuid4().hex,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["entry_gate_passed"] is False
    assert result["workflow_status"] == "entry_gate_failed"
    assert analyst.calls == []
    assert "requirement_analysis" not in result
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
    assert "approved requirement analysis" in result["errors"][0]
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
    assert requirements["raw_requirement"].startswith("Build a URL Shortener")
    assert requirements["submitted_requirements"][0] == "Accept a long URL."
    assert requirements["normalized_requirements"][0] == {
        "id": "REQ-001",
        "text": "Accept a long URL.",
    }

    analysis = (artifact_dir / "requirement_analysis.md").read_text(
        encoding="utf-8"
    )
    assert "## Analysis lineage" in analysis
    assert f"Prompt: {REQUIREMENT_ANALYSIS_PROMPT_VERSION}" in analysis
    assert "## Human requirement-review history" in analysis
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "Workflow result: success" in summary
    assert "Entry gate: passed" in summary
    assert "Requirement review: APPROVE" in summary
    assert "Exit gate: passed" in summary
    assert "## Human Approval History" in summary
    assert "### Requirement Analysis" in summary
    assert "### Implementation Plan" in summary
    assert "1. APPROVE" in summary
    assert "Revision: 0" in summary


def test_empty_revision_feedback_reprompts_and_one_line_still_works(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    responses = iter(["c", "", "Add explicit validation coverage.", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    response = cli._prompt_for_decision()

    assert response == {
        "decision": "REQUEST_CHANGES",
        "feedback": "Add explicit validation coverage.",
    }
    assert (
        "Feedback is required when requesting changes."
        in capsys.readouterr().out
    )
    with raises(StopIteration):
        next(responses)


def test_cli_preserves_multiline_feedback_without_leaking_to_next_review(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    analyst = FakeRequirementAnalysisClient([_analysis("v1"), _analysis("v2")])
    feedback_lines = [
        "Treat URL expiration behavior as an unresolved ambiguity and do not assume",
        "whether shortened URLs expire.",
    ]
    expected_feedback = "\n".join(feedback_lines)
    responses = iter(["c", *feedback_lines, "", "a", "a"])

    def write_stub_diagram(output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "WORKFLOW", build_workflow(analyst))
    monkeypatch.setattr(cli, "write_workflow_diagram", write_stub_diagram)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert cli.main(["demo"]) == 0

    output = capsys.readouterr().out
    assert "Please enter A, C, or R." not in output
    assert "[requirement_analysis_review] request_changes" in output
    assert "[requirement_analysis_review] approve" in output
    assert "[implementation_plan_approval] approve" in output
    assert analyst.calls[1]["human_feedback"] == expected_feedback
    assert isinstance(analyst.calls[1]["prior_analysis"], RequirementAnalysis)
    summary = (
        tmp_path / "artifacts" / "demo-run" / "summary.md"
    ).read_text(encoding="utf-8")
    assert expected_feedback in summary
    with raises(StopIteration):
        next(responses)


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

    analyst = FakeRequirementAnalysisClient([_analysis()])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "WORKFLOW", build_workflow(analyst))
    monkeypatch.setattr(cli, "write_workflow_diagram", fail_to_render)
    responses = iter(["a", "a"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

    assert cli.main(["demo"]) == 0
    output = capsys.readouterr()
    assert "Warning: workflow diagram was not generated" in output.err
    assert "[requirement_analysis_review] approve" in output.out
    assert "[implementation_plan_approval] approve" in output.out
    assert (tmp_path / "artifacts" / "demo-run" / "summary.md").exists()
