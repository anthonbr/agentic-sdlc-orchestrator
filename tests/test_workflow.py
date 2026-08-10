"""Behavior tests for the governed orchestration workflow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import agentic_sdlc.__main__ as cli
from pydantic import ValidationError
from pytest import CaptureFixture, MonkeyPatch, raises

from agentic_sdlc.artifacts import ARTIFACT_FILENAMES
from agentic_sdlc.llm import (
    FakeRequirementAnalysisClient,
    FakeTaskPlanningClient,
    OpenAIRequirementAnalysisClient,
    OpenAITaskPlanningClient,
    RequirementAnalysisClientError,
    TaskPlanningClientError,
)
from agentic_sdlc.nodes import exit_gate
from agentic_sdlc.prompts import (
    REQUIREMENT_ANALYSIS_PROMPT_VERSION,
    REQUIREMENT_ANALYSIS_SYSTEM_PROMPT,
    TASK_PLANNING_PROMPT_VERSION,
    TASK_PLANNING_SYSTEM_PROMPT,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import ApprovedRequirementSpec
from agentic_sdlc.state import (
    MAX_REQUIREMENT_ANALYSIS_ATTEMPTS,
    MAX_REQUIREMENT_REVISIONS,
    MAX_REQUIREMENT_REVISIONS_REASON,
    MAX_TASK_GRAPH_REVISIONS,
    MAX_TASK_GRAPH_REVISIONS_REASON,
    REQUIREMENT_ANALYSIS_REJECTED_REASON,
    TASK_GRAPH_REJECTED_REASON,
    WorkflowState,
    demo_input,
)
from agentic_sdlc.task_execution import TaskGraphExecutionStatus
from agentic_sdlc.task_execution_contracts import (
    ArtifactMaterializationProposal,
    ArtifactOutput,
    EngineeringArtifactType,
    TaskExecutionResult,
)
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    TaskGraph,
    TaskMaterializationPolicy,
    TaskType,
)
from agentic_sdlc.workspace_integration_contracts import (
    WorkspaceBoundTaskExecutionRequest,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow


class RecordingTaskExecutor:
    """Deterministic network-free executor for complete workflow tests."""

    model_name = "recording-task-executor"

    def __init__(self) -> None:
        self.calls: list[WorkspaceBoundTaskExecutionRequest] = []

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        self.calls.append(request)
        artifact_types = {
            TaskType.DESIGN: EngineeringArtifactType.DESIGN,
            TaskType.IMPLEMENTATION: EngineeringArtifactType.SOURCE,
            TaskType.TEST: EngineeringArtifactType.TEST,
            TaskType.DOCUMENTATION: EngineeringArtifactType.DOCUMENTATION,
            TaskType.VALIDATION: EngineeringArtifactType.VALIDATION,
            TaskType.RELEASE: EngineeringArtifactType.OTHER,
        }
        return TaskExecutionResult(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            task_id=request.task_id,
            summary=f"Produced governed output for {request.task_id}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=artifact_types[request.task.task_type],
                    logical_name=request.task.expected_outputs[0],
                    content=(
                        f"Canonical proposal for {request.task.title}. "
                        f"Dependency artifacts: {len(request.dependency_artifacts)}."
                    ),
                ),
            ),
            materialization_proposals=(
                (
                    ArtifactMaterializationProposal(
                        output_index=1,
                        target_path={
                            "build_service": "src/url_shortener/service.py",
                            "verify_service": "tests/test_service.py",
                            "document_service": "README.md",
                        }[request.task.source_key],
                    ),
                )
                if request.task.materialization_policy
                is TaskMaterializationPolicy.REQUIRED
                else ()
            ),
            assumptions=(),
            risks=(),
        )


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
        ambiguities=["URL expiration behavior is unspecified."],
        assumptions=[
            "Materialization is limited to the disposable isolated workspace."
        ],
        acceptance_criteria=[
            "A submitted valid URL receives a unique short URL.",
            "An unknown short code returns a defined error.",
        ],
        risks=["Short-code collisions could produce incorrect redirects."],
        needs_clarification=True,
        confidence=0.85,
    )


def _proposed_task(
    key: str,
    title: str,
    *,
    task_type: TaskType = TaskType.DESIGN,
    depends_on: list[str] | None = None,
    requirement_refs: list[str] | None = None,
    acceptance_refs: list[str] | None = None,
    risk_refs: list[str] | None = None,
    ambiguity_refs: list[str] | None = None,
    materialization_policy: TaskMaterializationPolicy = (
        TaskMaterializationPolicy.FORBIDDEN
    ),
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=title,
        description=f"Produce the governed {title.lower()} output.",
        task_type=task_type,
        materialization_policy=materialization_policy,
        depends_on=depends_on or [],
        requirement_refs=requirement_refs or ["FR-001"],
        acceptance_criteria_refs=acceptance_refs or [],
        risk_refs=risk_refs or [],
        ambiguity_refs=ambiguity_refs or [],
        expected_outputs=[f"{key}.md"],
    )


def _proposal(version: str = "v1") -> ProposedTaskGraph:
    return ProposedTaskGraph(
        tasks=[
            _proposed_task(
                "define_api",
                f"Define API contract {version}",
                requirement_refs=["FR-001", "FR-003", "FR-004"],
                acceptance_refs=["AC-001", "AC-002"],
            ),
            _proposed_task(
                "define_storage",
                "Define persistence model",
                requirement_refs=["FR-002", "CON-001"],
                risk_refs=["RISK-001"],
            ),
            _proposed_task(
                "build_service",
                "Implement shortening behavior",
                task_type=TaskType.IMPLEMENTATION,
                depends_on=["define_api", "define_storage"],
                requirement_refs=["FR-001", "FR-002", "FR-003", "FR-004"],
                acceptance_refs=["AC-001", "AC-002"],
                ambiguity_refs=["AMB-001"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _proposed_task(
                "verify_service",
                "Verify approved behavior",
                task_type=TaskType.TEST,
                depends_on=["build_service"],
                requirement_refs=["NFR-001"],
                acceptance_refs=["AC-001", "AC-002"],
                risk_refs=["RISK-001"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _proposed_task(
                "document_service",
                "Document service contract",
                task_type=TaskType.DOCUMENTATION,
                depends_on=["verify_service"],
                requirement_refs=["FR-001"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )


def _interrupt_stage(state: WorkflowState) -> str:
    return state["__interrupt__"][0].value["stage"]


def _start_demo(
    artifact_dir: Path | None = None,
    *,
    analyst: FakeRequirementAnalysisClient | None = None,
    planner: FakeTaskPlanningClient | None = None,
    executor: RecordingTaskExecutor | None = None,
) -> tuple[Any, str, WorkflowState, FakeRequirementAnalysisClient, FakeTaskPlanningClient]:
    active_analyst = analyst or FakeRequirementAnalysisClient([_analysis()])
    active_planner = planner or FakeTaskPlanningClient([_proposal()])
    workflow = build_workflow(
        active_analyst, active_planner, executor or RecordingTaskExecutor()
    )
    thread_id = uuid4().hex
    state = run_workflow(
        demo_input(),
        thread_id=thread_id,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    assert state["workflow_status"] == "awaiting_approval"
    assert _interrupt_stage(state) == "requirement_analysis_review"
    return workflow, thread_id, state, active_analyst, active_planner


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
    assert _interrupt_stage(state) == "task_graph_review"
    return state


def _approve_demo(artifact_dir: Path | None = None) -> WorkflowState:
    workflow, thread_id, _, _, _ = _start_demo(artifact_dir)
    _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)
    return resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )


def test_valid_analysis_is_json_safe_before_requirement_review() -> None:
    _, _, paused, analyst, planner = _start_demo()

    assert paused["entry_gate_passed"] is True
    assert paused["requirement_analysis_status"] == "validated"
    assert isinstance(paused["requirement_analysis"], dict)
    assert paused["requirement_analysis_attempt_count"] == 1
    assert paused["requirement_analysis_history"][0]["prompt_version"] == (
        REQUIREMENT_ANALYSIS_PROMPT_VERSION
    )
    assert len(analyst.calls) == 1
    assert planner.calls == []
    assert "approved_requirement_spec" not in paused


def test_requirement_approval_builds_spec_and_reaches_task_graph_review() -> None:
    workflow, thread_id, _, _, planner = _start_demo()

    paused = _approve_requirements(workflow, thread_id)

    spec = paused["approved_requirement_spec"]
    assert spec["source_analysis_revision"] == 0
    assert spec["functional_requirements"][0]["item_id"] == "FR-001"
    assert spec["ambiguities"][0]["item_id"] == "AMB-001"
    assert paused["task_planning_status"] == "validated"
    assert paused["candidate_task_graph"]["tasks"][0]["task_id"] == "TASK-001"
    assert paused["task_graph_semantics"]["execution_layers"][0] == [
        "TASK-001",
        "TASK-002",
    ]
    assert len(planner.calls) == 1
    supplied_spec = planner.calls[0]["approved_spec"]
    assert isinstance(supplied_spec, ApprovedRequirementSpec)
    assert supplied_spec.spec_id == spec["spec_id"]
    assert "approved_task_graph" not in paused


def test_requirement_changes_preserve_feedback_and_lineage() -> None:
    analyst = FakeRequirementAnalysisClient([_analysis("v1"), _analysis("v2")])
    workflow, thread_id, paused, _, _ = _start_demo(analyst=analyst)
    original = paused["requirement_analysis"]
    feedback = (
        "Treat URL expiration behavior as an unresolved ambiguity and do not assume\n"
        "whether shortened URLs expire."
    )

    revised = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        workflow=workflow,
    )

    assert _interrupt_stage(revised) == "requirement_analysis_review"
    assert revised["requirement_analysis_revision_count"] == 1
    assert [
        record["revision_number"]
        for record in revised["requirement_analysis_history"]
    ] == [0, 1]
    assert revised["requirement_analysis_history"][1]["reviewer_feedback"] == feedback
    assert analyst.calls[1]["human_feedback"] == feedback
    prior = analyst.calls[1]["prior_analysis"]
    assert isinstance(prior, RequirementAnalysis)
    assert prior.model_dump(mode="json") == original


def test_requirement_rejection_safe_stops_before_spec_or_planning(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "requirement-rejected"
    workflow, thread_id, _, _, planner = _start_demo(artifact_dir)

    result = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": "Not ready."},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == REQUIREMENT_ANALYSIS_REJECTED_REASON
    assert "approved_requirement_spec" not in result
    assert "candidate_task_graph" not in result
    assert planner.calls == []
    assert {path.name for path in artifact_dir.iterdir()} == {
        "requirements.json",
        "requirement_analysis.md",
        "summary.md",
    }


def test_requirement_revision_limit_safe_stops() -> None:
    analyst = FakeRequirementAnalysisClient(
        [_analysis(f"v{number}") for number in range(MAX_REQUIREMENT_REVISIONS + 1)]
    )
    workflow, thread_id, state, _, _ = _start_demo(analyst=analyst)
    for revision in range(1, MAX_REQUIREMENT_REVISIONS + 1):
        state = resume_workflow(
            thread_id,
            {"decision": "REQUEST_CHANGES", "feedback": f"Revision {revision}"},
            workflow=workflow,
        )
        assert _interrupt_stage(state) == "requirement_analysis_review"

    result = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": "One too many"},
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == MAX_REQUIREMENT_REVISIONS_REASON


def test_invalid_requirement_output_retries_then_reaches_review() -> None:
    invalid = _analysis().model_dump()
    invalid["functional_requirements"] = []
    analyst = FakeRequirementAnalysisClient([invalid, _analysis("v2")])

    _, _, paused, _, _ = _start_demo(analyst=analyst)

    assert paused["requirement_analysis_attempt_count"] == 2
    assert len(paused["requirement_analysis_failures"]) == 1
    assert len(analyst.calls) == 2


def test_transient_requirement_provider_failure_retries() -> None:
    analyst = FakeRequirementAnalysisClient(
        [
            RequirementAnalysisClientError("Temporary outage.", retryable=True),
            _analysis("recovered"),
        ]
    )

    _, _, paused, _, _ = _start_demo(analyst=analyst)

    assert paused["requirement_analysis_attempt_count"] == 2
    assert paused["requirement_analysis_failures"][0]["reason"] == (
        "Temporary outage."
    )


def test_requirement_retry_exhaustion_safe_stops(tmp_path: Path) -> None:
    analyst = FakeRequirementAnalysisClient(
        [
            {"normalized_problem_statement": "Incomplete"}
            for _ in range(MAX_REQUIREMENT_ANALYSIS_ATTEMPTS)
        ]
    )
    planner = FakeTaskPlanningClient([_proposal()])
    workflow = build_workflow(analyst, planner)
    artifact_dir = tmp_path / "analysis-failed"

    result = run_workflow(
        demo_input(),
        thread_id=uuid4().hex,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert "failed after 3 attempts" in result["safe_stop_reason"]
    assert planner.calls == []
    assert {path.name for path in artifact_dir.iterdir()} == {
        "requirements.json",
        "summary.md",
    }


def test_missing_requirement_api_key_safe_stops_without_network(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    workflow = build_workflow(
        OpenAIRequirementAnalysisClient(api_key=""),
        FakeTaskPlanningClient([_proposal()]),
    )

    result = run_workflow(demo_input(), thread_id=uuid4().hex, workflow=workflow)

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == (
        "OPENAI_API_KEY is not configured; requirement analysis cannot run."
    )


def test_created_requirement_openai_client_disables_sdk_retries(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agentic_sdlc.llm.OpenAI", fake_openai)

    OpenAIRequirementAnalysisClient(api_key="test-key")._create_client()

    assert captured == {"api_key": "test-key", "max_retries": 0}


def test_openai_requirement_client_uses_structured_parse_without_network() -> None:
    calls: list[dict[str, Any]] = []

    class StubResponses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=_analysis("sdk"))

    client = OpenAIRequirementAnalysisClient(
        model_name="test-model", client=SimpleNamespace(responses=StubResponses())
    )

    result = client.invoke_structured("Build it.", _analysis("prior"), "Clarify it")

    assert isinstance(result, RequirementAnalysis)
    assert calls[0]["model"] == "test-model"
    assert calls[0]["text_format"] is RequirementAnalysis
    assert calls[0]["store"] is False
    assert "Prior validated analysis" in calls[0]["input"][1]["content"]
    assert "Authoritative human review feedback" in calls[0]["input"][1]["content"]


def test_requirement_prompt_preserves_authoritative_feedback_contract() -> None:
    prompt = " ".join(REQUIREMENT_ANALYSIS_SYSTEM_PROMPT.casefold().split())
    assert REQUIREMENT_ANALYSIS_PROMPT_VERSION == "requirement-analysis-v1.1"
    assert "authoritative revision instruction" in prompt
    assert "represent it as an ambiguity" in prompt


def test_openai_task_planner_uses_approved_spec_and_schema_without_network() -> None:
    workflow, thread_id, _, _, planner = _start_demo()
    paused = _approve_requirements(workflow, thread_id)
    spec = planner.calls[0]["approved_spec"]
    prior_graph = TaskGraph.model_validate_json(
        json.dumps(paused["candidate_task_graph"])
    )
    calls: list[dict[str, Any]] = []

    class StubResponses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=_proposal("sdk"))

    client = OpenAITaskPlanningClient(
        model_name="test-model", client=SimpleNamespace(responses=StubResponses())
    )
    result = client.invoke_structured(spec, prior_graph, "Add validation work.")

    assert isinstance(result, ProposedTaskGraph)
    assert calls[0]["text_format"] is ProposedTaskGraph
    assert calls[0]["store"] is False
    content = calls[0]["input"][1]["content"]
    assert "Human-approved requirement specification" in content
    assert spec.spec_id in content
    assert "Prior validated task graph" in content
    assert "Authoritative human task-graph review feedback" in content


def test_task_planning_prompt_reserves_authoritative_metadata() -> None:
    prompt = " ".join(TASK_PLANNING_SYSTEM_PROMPT.casefold().split())
    assert TASK_PLANNING_PROMPT_VERSION == "task-planning-v1.2"
    assert "cover every fr, nfr, con, and ac item" in prompt
    assert "deterministic application validation is authoritative" in prompt
    assert "do not assign task-### ids" in prompt
    assert "do not silently choose an implementation outcome" in prompt
    assert "do not derive this policy mechanically from task type" in prompt
    assert "no_change may eventually satisfy required" in prompt
    assert "do not execute tasks" in prompt


def test_invalid_task_graph_retries_then_reaches_review() -> None:
    invalid = _proposal().model_dump(mode="json")
    invalid["tasks"][0]["requirement_refs"] = ["FR-999"]
    planner = FakeTaskPlanningClient([invalid, _proposal("retry")])
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)

    paused = _approve_requirements(workflow, thread_id)

    assert paused["task_planning_attempt_count"] == 2
    assert len(paused["task_planning_failures"]) == 1
    assert "FR-999" in paused["task_planning_failures"][0]["reason"]
    assert len(planner.calls) == 2


def test_incomplete_core_coverage_retries_before_human_review() -> None:
    incomplete = _proposal().model_dump(mode="json")
    incomplete["tasks"][3]["requirement_refs"] = []
    planner = FakeTaskPlanningClient([incomplete, _proposal("retry")])
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)

    paused = _approve_requirements(workflow, thread_id)

    assert paused["task_planning_attempt_count"] == 2
    assert len(paused["task_planning_failures"]) == 1
    assert paused["task_planning_failures"][0]["reason"] == (
        "Uncovered approved specification items: NFR-001."
    )
    assert _interrupt_stage(paused) == "task_graph_review"
    assert len(planner.calls) == 2


def test_incomplete_core_coverage_exhaustion_safe_stops() -> None:
    responses = []
    for _ in range(3):
        incomplete = _proposal().model_dump(mode="json")
        incomplete["tasks"][3]["requirement_refs"] = []
        responses.append(incomplete)
    planner = FakeTaskPlanningClient(responses)
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert "Task planning failed after 3 attempts" in result["safe_stop_reason"]
    assert "NFR-001" in result["safe_stop_reason"]
    assert len(result["task_planning_failures"]) == 3
    assert "candidate_task_graph" not in result
    assert "approved_task_graph" not in result


def test_transient_task_provider_failure_retries() -> None:
    planner = FakeTaskPlanningClient(
        [TaskPlanningClientError("Planner unavailable.", retryable=True), _proposal()]
    )
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)

    paused = _approve_requirements(workflow, thread_id)

    assert paused["task_planning_attempt_count"] == 2
    assert paused["task_planning_failures"][0]["reason"] == "Planner unavailable."


def test_task_planning_retry_exhaustion_safe_stops(tmp_path: Path) -> None:
    planner = FakeTaskPlanningClient(
        [
            TaskPlanningClientError("Still unavailable.", retryable=True)
            for _ in range(3)
        ]
    )
    workflow, thread_id, _, _, _ = _start_demo(
        tmp_path / "task-failed", planner=planner
    )

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=tmp_path / "task-failed",
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert "Task planning failed after 3 attempts" in result["safe_stop_reason"]
    assert "candidate_task_graph" not in result
    assert "approved_task_graph" not in result
    assert "architecture" not in result


def test_missing_api_key_at_task_planning_safe_stops_without_fake_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    workflow = build_workflow(
        FakeRequirementAnalysisClient([_analysis()]),
        OpenAITaskPlanningClient(api_key=""),
    )
    thread_id = uuid4().hex
    paused = run_workflow(demo_input(), thread_id=thread_id, workflow=workflow)

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == (
        "OPENAI_API_KEY is not configured; task planning cannot run."
    )
    assert "approved_task_graph" not in result


def test_created_task_planning_openai_client_disables_sdk_retries(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agentic_sdlc.llm.OpenAI", fake_openai)

    OpenAITaskPlanningClient(api_key="test-key")._create_client()

    assert captured == {"api_key": "test-key", "max_retries": 0}


def test_task_graph_approval_runs_the_authoritative_task_graph_to_completion() -> None:
    workflow, thread_id, _, _, _ = _start_demo()
    paused = _approve_requirements(workflow, thread_id)

    assert "approved_task_graph" not in paused
    assert "architecture" not in paused
    assert "test_plan" not in paused

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )

    assert result["approved_task_graph"] == result["candidate_task_graph"]
    assert result["task_graph_execution"].status is (
        TaskGraphExecutionStatus.SUCCEEDED
    )
    assert [request.task_id for request in result["task_execution_requests"]] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
        "TASK-005",
    ]
    assert len(result["engineering_artifacts"]) == 5
    assert all(
        validation.passed
        for validation in result["task_execution_validations"]
    )
    assert "architecture" not in result
    assert "test_plan" not in result
    assert result["exit_gate_passed"] is True
    assert result["workflow_status"] == "success"
    assert result["task_graph_review_history"] == [
        {
            "sequence": 1,
            "checkpoint": "task_graph",
            "decision": "APPROVE",
            "feedback": "",
            "revision_number": 0,
        }
    ]


def test_task_graph_request_changes_replans_revalidates_and_can_be_approved() -> None:
    planner = FakeTaskPlanningClient([_proposal("v1"), _proposal("v2")])
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)
    first = _approve_requirements(workflow, thread_id)
    first_graph = first["candidate_task_graph"]
    feedback = "Add explicit validation coverage before documentation."

    revised = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        workflow=workflow,
    )

    assert _interrupt_stage(revised) == "task_graph_review"
    assert revised["task_graph_revision_count"] == 1
    assert revised["task_planning_attempt_count"] == 1
    assert len(revised["task_graph_history"]) == 2
    assert revised["task_graph_history"][1]["reviewer_feedback"] == feedback
    assert revised["candidate_task_graph"]["version"] == 2
    assert revised["candidate_task_graph"]["supersedes_graph_id"] == (
        first_graph["graph_id"]
    )
    assert planner.calls[1]["human_feedback"] == feedback
    prior = planner.calls[1]["prior_task_graph"]
    assert isinstance(prior, TaskGraph)
    assert prior.graph_id == first_graph["graph_id"]
    assert "approved_task_graph" not in revised

    result = resume_workflow(
        thread_id,
        {"decision": "APPROVE", "feedback": ""},
        workflow=workflow,
    )
    assert result["workflow_status"] == "success"
    assert [event["decision"] for event in result["task_graph_review_history"]] == [
        "REQUEST_CHANGES",
        "APPROVE",
    ]


def test_task_graph_rejection_safe_stops_without_parallel_artifacts(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "graph-rejected"
    workflow, thread_id, _, _, _ = _start_demo(artifact_dir)
    _approve_requirements(workflow, thread_id, artifact_dir=artifact_dir)

    result = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": "Graph is not acceptable."},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == TASK_GRAPH_REJECTED_REASON
    assert "approved_task_graph" not in result
    assert "architecture" not in result
    assert "test_plan" not in result
    assert {path.name for path in artifact_dir.iterdir()} == {
        "requirements.json",
        "requirement_analysis.md",
        "approved_requirement_spec.json",
        "task_graph.json",
        "task_graph.md",
        "summary.md",
    }


def test_task_graph_revision_limit_safe_stops() -> None:
    planner = FakeTaskPlanningClient(
        [_proposal(f"v{revision}") for revision in range(MAX_TASK_GRAPH_REVISIONS + 1)]
    )
    workflow, thread_id, _, _, _ = _start_demo(planner=planner)
    state = _approve_requirements(workflow, thread_id)
    for revision in range(1, MAX_TASK_GRAPH_REVISIONS + 1):
        state = resume_workflow(
            thread_id,
            {"decision": "REQUEST_CHANGES", "feedback": f"Graph revision {revision}"},
            workflow=workflow,
        )
        assert _interrupt_stage(state) == "task_graph_review"

    result = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": "One too many"},
        workflow=workflow,
    )

    assert result["workflow_status"] == "safe_stopped"
    assert result["safe_stop_reason"] == MAX_TASK_GRAPH_REVISIONS_REASON
    assert "approved_task_graph" not in result


def test_entry_gate_failure_stops_before_any_llm(tmp_path: Path) -> None:
    analyst = FakeRequirementAnalysisClient([_analysis()])
    planner = FakeTaskPlanningClient([_proposal()])
    workflow = build_workflow(analyst, planner)
    artifact_dir = tmp_path / "entry-failed"
    invalid: WorkflowState = {"project_name": "", "requirements": ["Valid text"]}

    result = run_workflow(
        invalid,
        thread_id=uuid4().hex,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    assert result["workflow_status"] == "entry_gate_failed"
    assert analyst.calls == []
    assert planner.calls == []
    assert not artifact_dir.exists()


def test_exit_gate_rejects_incomplete_governed_state() -> None:
    incomplete: WorkflowState = {
        "entry_gate_passed": True,
        "normalized_requirements": [{"id": "REQ-001", "text": "Requirement"}],
        "errors": [],
    }

    result = exit_gate(incomplete)

    assert result["exit_gate_passed"] is False
    assert "approved requirement specification" in result["errors"][0]
    assert "approved task graph" in result["errors"][0]


def test_successful_run_writes_canonical_artifact_set(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "demo-run"
    artifact_dir.mkdir()
    (artifact_dir / "architecture.md").write_text("legacy architecture")
    (artifact_dir / "test_plan.md").write_text("legacy test plan")

    result = _approve_demo(artifact_dir)

    assert result["workflow_status"] == "success"
    assert {path.name for path in artifact_dir.iterdir()} == set(ARTIFACT_FILENAMES)
    assert not (artifact_dir / "architecture.md").exists()
    assert not (artifact_dir / "test_plan.md").exists()
    spec = json.loads(
        (artifact_dir / "approved_requirement_spec.json").read_text()
    )
    graph = json.loads((artifact_dir / "task_graph.json").read_text())
    execution = json.loads((artifact_dir / "task_execution.json").read_text())
    engineering_artifacts = json.loads(
        (artifact_dir / "engineering_artifacts.json").read_text()
    )
    graph_markdown = (artifact_dir / "task_graph.md").read_text()
    summary = (artifact_dir / "summary.md").read_text()
    assert spec["functional_requirements"][0]["item_id"] == "FR-001"
    assert graph["tasks"][0]["task_id"] == "TASK-001"
    assert "Layer 1 — parallel" in graph_markdown
    assert "Execution status: SUCCEEDED" in graph_markdown
    assert execution["task_graph_execution"]["status"] == "SUCCEEDED"
    assert [request["task_id"] for request in execution["requests"]] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
        "TASK-005",
    ]
    assert len(engineering_artifacts) == 5
    assert "Required specification coverage: complete (FR/NFR/CON/AC)" in (
        graph_markdown
    )
    assert "Requirement Analysis" in summary
    assert "Engineering Task Graph" in summary
    assert "TaskGraph execution: SUCCEEDED" in summary
    assert "governed V0.5 workflow executed" in summary
    assert "No engineering task was executed" not in summary


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
    assert "Feedback is required" in capsys.readouterr().out


def test_cli_preserves_multiline_requirement_feedback_and_reaches_graph_review(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    analyst = FakeRequirementAnalysisClient([_analysis("v1"), _analysis("v2")])
    planner = FakeTaskPlanningClient([_proposal()])
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
    monkeypatch.setattr(
        cli,
        "WORKFLOW",
        build_workflow(analyst, planner, RecordingTaskExecutor()),
    )
    monkeypatch.setattr(cli, "write_workflow_diagram", write_stub_diagram)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert cli.main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "Please enter A, C, or R." not in output
    assert "Engineering task graph requires human review." in output
    assert "Layer 1 — parallel" in output
    assert analyst.calls[1]["human_feedback"] == expected_feedback
    assert "[task_graph_review] approve" in output
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

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "WORKFLOW",
        build_workflow(
            FakeRequirementAnalysisClient([_analysis()]),
            FakeTaskPlanningClient([_proposal()]),
            RecordingTaskExecutor(),
        ),
    )
    monkeypatch.setattr(cli, "write_workflow_diagram", fail_to_render)
    responses = iter(["a", "a"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    assert cli.main(["demo"]) == 0
    output = capsys.readouterr()
    assert "Warning: workflow diagram was not generated" in output.err
    assert "[task_graph_review] approve" in output.out
    assert (tmp_path / "artifacts" / "demo-run" / "summary.md").exists()
