"""Focused proof for the governed ambiguous-requirement reviewer scenario."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from agentic_sdlc.artifacts import ARTIFACT_FILENAMES
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import ApprovedRequirementSpec
from agentic_sdlc.task_execution import (
    TaskExecutionStatus,
    TaskGraphExecutionStatus,
)
from agentic_sdlc.task_graph import TaskMaterializationPolicy
from agentic_sdlc.workspace_contracts import WorkspaceChangeOperation
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDisposition,
    WorkspaceIntegrityStatus,
)
from agentic_sdlc.workspace_mutation import WorkspaceMutationStatus
from tests.demo_ambiguity_scenario import (
    AMBIGUITY_CONTEXT_PATHS_BY_SOURCE_KEY,
    AMBIGUITY_IMPACTED_PATHS,
    AMBIGUITY_RAW_REQUIREMENT,
    AMBIGUITY_SOURCE_LABEL,
    AMBIGUITY_SOURCE_PATHS,
    AMBIGUITY_UNCHANGED_PATHS,
    EXPIRATION_IMPACT_ANALYSIS,
    EXPIRATION_README,
    EXPIRATION_SERVICE,
    EXPIRATION_TESTS,
    HUMAN_EXPIRATION_CLARIFICATION,
    INITIAL_AMBIGUITIES,
    AmbiguityDemoRun,
    run_ambiguity_demo,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
BROWNFIELD_PROJECT = (
    REPOSITORY_ROOT / "artifacts/brownfield-demo-run/enhanced-project"
)


@pytest.fixture
def ambiguity_run(tmp_path: Path) -> AmbiguityDemoRun:
    return run_ambiguity_demo(
        tmp_path / "workspaces",
        source_root=BROWNFIELD_PROJECT,
    )


def _interrupt_payload(state: dict[str, Any]) -> dict[str, Any]:
    return state["__interrupt__"][0].value


def _snapshot_by_id(run: AmbiguityDemoRun, snapshot_id: str):
    return next(
        snapshot
        for snapshot in run.final_state["workspace_snapshots"]
        if snapshot.snapshot_id == snapshot_id
    )


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_revision_zero_is_blocked_with_all_ambiguities_and_zero_planner_calls(
    ambiguity_run: AmbiguityDemoRun,
) -> None:
    initial = ambiguity_run.initial_state
    readiness = initial["requirement_planning_readiness"]
    interrupt = _interrupt_payload(initial)

    assert initial["raw_requirement"] == AMBIGUITY_RAW_REQUIREMENT
    assert initial["requirement_analysis_revision_count"] == 0
    assert initial["requirement_analysis"]["needs_clarification"] is True
    assert tuple(initial["requirement_analysis"]["ambiguities"]) == INITIAL_AMBIGUITIES
    assert readiness == {
        "analysis_revision": 0,
        "status": "BLOCKED",
        "needs_clarification": True,
        "blocking_ambiguities": list(INITIAL_AMBIGUITIES),
        "reason_code": "UNRESOLVED_REQUIREMENT_AMBIGUITY",
    }
    assert interrupt["allowed_decisions"] == ["REQUEST_CHANGES", "REJECT"]
    assert ambiguity_run.planner_calls_at_initial_block == 0
    assert "approved_requirement_spec" not in initial
    assert "candidate_task_graph" not in initial
    assert "approved_task_graph" not in initial


def test_request_changes_preserves_exact_feedback_and_immutable_revision_lineage(
    ambiguity_run: AmbiguityDemoRun,
) -> None:
    initial = ambiguity_run.initial_state
    revised = ambiguity_run.revised_state
    history = revised["requirement_analysis_history"]

    assert [record["revision_number"] for record in history] == [0, 1]
    assert history[0] == initial["requirement_analysis_history"][0]
    assert history[0]["planning_readiness"]["status"] == "BLOCKED"
    assert tuple(history[0]["analysis"]["ambiguities"]) == INITIAL_AMBIGUITIES
    assert history[1]["planning_readiness"]["status"] == "READY"
    assert history[1]["planning_readiness"]["blocking_ambiguities"] == []
    assert history[1]["analysis"]["needs_clarification"] is False
    assert history[1]["reviewer_feedback"] == HUMAN_EXPIRATION_CLARIFICATION
    assert revised["requirement_review_history"] == [
        {
            "sequence": 1,
            "checkpoint": "requirement_analysis",
            "decision": "REQUEST_CHANGES",
            "feedback": HUMAN_EXPIRATION_CLARIFICATION,
            "revision_number": 0,
        }
    ]
    prior = ambiguity_run.analyst.calls[1]["prior_analysis"]
    assert isinstance(prior, RequirementAnalysis)
    assert prior.model_dump(mode="json") == history[0]["analysis"]
    assert ambiguity_run.analyst.calls[1]["human_feedback"] == (
        HUMAN_EXPIRATION_CLARIFICATION
    )
    assert ambiguity_run.planner_calls_after_revision == 0
    assert "approved_requirement_spec" not in revised


def test_approval_builds_revised_authority_and_planner_consumes_only_that_spec(
    ambiguity_run: AmbiguityDemoRun,
) -> None:
    graph_review = ambiguity_run.graph_review_state
    spec = graph_review["approved_requirement_spec"]
    planner_call = ambiguity_run.planner.calls[0]
    supplied_spec = planner_call["approved_spec"]

    assert len(ambiguity_run.planner.calls) == 1
    assert spec["source_analysis_revision"] == 1
    assert spec["ambiguities"] == []
    assert spec["version"] == 1
    assert isinstance(supplied_spec, ApprovedRequirementSpec)
    assert supplied_spec.model_dump(mode="json") == spec
    assert all(
        ambiguity not in supplied_spec.model_dump_json()
        for ambiguity in INITIAL_AMBIGUITIES
    )
    assert [event["decision"] for event in graph_review["requirement_review_history"]] == [
        "REQUEST_CHANGES",
        "APPROVE",
    ]
    graph = graph_review["candidate_task_graph"]
    assert graph["requirement_spec_id"] == spec["spec_id"]
    assert graph["requirement_spec_version"] == spec["version"]


def test_small_task_graph_uses_governed_nonmutation_and_parallel_exit_work(
    ambiguity_run: AmbiguityDemoRun,
) -> None:
    state = ambiguity_run.final_state
    tasks = state["approved_task_graph"]["tasks"]

    assert [task["task_id"] for task in tasks] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
    ]
    assert [task["depends_on"] for task in tasks] == [
        [],
        ["TASK-001"],
        ["TASK-002"],
        ["TASK-002"],
    ]
    assert [task["materialization_policy"] for task in tasks] == [
        TaskMaterializationPolicy.FORBIDDEN,
        TaskMaterializationPolicy.REQUIRED,
        TaskMaterializationPolicy.REQUIRED,
        TaskMaterializationPolicy.REQUIRED,
    ]
    assert state["task_graph_semantics"]["execution_layers"] == [
        ["TASK-001"],
        ["TASK-002"],
        ["TASK-003", "TASK-004"],
    ]
    assert [
        [attempt.task_id for attempt in wave.task_attempts]
        for wave in state["task_execution_waves"]
    ] == [["TASK-001"], ["TASK-002"], ["TASK-003", "TASK-004"]]
    impact = next(
        artifact for artifact in state["engineering_artifacts"] if artifact.task_id == "TASK-001"
    )
    assert impact.content == EXPIRATION_IMPACT_ANALYSIS
    assert not any(
        change_set.task_id == "TASK-001"
        for change_set in state["workspace_change_sets"]
    )
    assert (
        ambiguity_run.executor.calls_by_task_id["TASK-003"].workspace_binding
        == ambiguity_run.executor.calls_by_task_id["TASK-004"].workspace_binding
    )


def test_task_scoped_context_and_three_modifies_have_verified_images(
    ambiguity_run: AmbiguityDemoRun,
) -> None:
    state = ambiguity_run.final_state
    expected_paths = {
        "TASK-001": AMBIGUITY_CONTEXT_PATHS_BY_SOURCE_KEY["expiration_impact"],
        "TASK-002": AMBIGUITY_CONTEXT_PATHS_BY_SOURCE_KEY[
            "expiration_implementation"
        ],
        "TASK-003": (
            "src/url_shortener/app.py",
            "src/url_shortener/service.py",
            "tests/test_service.py",
        ),
        "TASK-004": (
            "README.md",
            "src/url_shortener/app.py",
            "src/url_shortener/service.py",
        ),
    }
    for task_id, request in ambiguity_run.executor.calls_by_task_id.items():
        assert tuple(
            observation.path for observation in request.repository_context.observations
        ) == expected_paths[task_id]

    expected_change_path = {
        "TASK-002": "src/url_shortener/service.py",
        "TASK-003": "tests/test_service.py",
        "TASK-004": "README.md",
    }
    assert len(state["workspace_change_sets"]) == 3
    assert len(state["workspace_mutation_results"]) == 3
    mutation_by_id = {
        result.change_set_id: result for result in state["workspace_mutation_results"]
    }
    for change_set in state["workspace_change_sets"]:
        assert len(change_set.file_changes) == 1
        change = change_set.file_changes[0]
        result = mutation_by_id[change_set.change_set_id]
        evidence = result.file_evidence[0]
        assert change.path == expected_change_path[change_set.task_id]
        assert change.operation is WorkspaceChangeOperation.MODIFY
        assert result.status is WorkspaceMutationStatus.APPLIED
        assert evidence.expected_preimage_hash == evidence.observed_preimage_hash
        assert evidence.desired_postimage_hash == evidence.observed_postimage_hash
        assert evidence.write_performed is True


def test_execution_export_and_application_validation_are_verified(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    run = run_ambiguity_demo(
        tmp_path / "workspaces",
        source_root=BROWNFIELD_PROJECT,
        artifact_dir=artifact_dir,
    )
    state = run.final_state
    session = state["governed_workspace_session"]
    baseline = _snapshot_by_id(run, run.seed_result.baseline_snapshot_id)
    final = _snapshot_by_id(run, session.authoritative_snapshot_id)
    baseline_hashes = {item.path: item.content_hash for item in baseline.files}
    final_hashes = {item.path: item.content_hash for item in final.files}

    assert run.seed_result.source_root == AMBIGUITY_SOURCE_LABEL
    assert run.seed_result.verified is True
    assert tuple(final_hashes) == AMBIGUITY_SOURCE_PATHS
    assert state["workflow_status"] == "success"
    assert state["exit_gate_passed"] is True
    assert state["task_graph_execution"].status is TaskGraphExecutionStatus.SUCCEEDED
    assert all(
        task.status is TaskExecutionStatus.SUCCEEDED
        for task in state["task_graph_execution"].task_states
    )
    assert all(
        decision.disposition is TaskAttemptExitDisposition.SUCCEED_TASK
        for decision in state["task_attempt_exit_decisions"]
    )
    assert session.integrity_status is WorkspaceIntegrityStatus.VERIFIED
    for path in AMBIGUITY_IMPACTED_PATHS:
        assert final_hashes[path] != baseline_hashes[path]
    for path in AMBIGUITY_UNCHANGED_PATHS:
        assert final_hashes[path] == baseline_hashes[path]
    assert run.exported_application_test_count == 20
    export_root = artifact_dir / "expiration-project"
    assert (export_root / "src/url_shortener/service.py").read_text() == (
        EXPIRATION_SERVICE
    )
    assert (export_root / "tests/test_service.py").read_text() == EXPIRATION_TESTS
    assert (export_root / "README.md").read_text() == EXPIRATION_README
    exported_hashes = {
        path: hashlib.sha256((export_root / path).read_bytes()).hexdigest()
        for path in AMBIGUITY_SOURCE_PATHS
    }
    assert exported_hashes == final_hashes


def test_fixed_clock_proves_before_at_and_after_expiration_boundaries() -> None:
    namespace: dict[str, Any] = {"__name__": "expiration_service_under_test"}
    exec(compile(EXPIRATION_SERVICE, "service.py", "exec"), namespace)
    shortener_type = namespace["URLShortener"]
    error_type = namespace["UnknownShortCodeError"]
    created_at = datetime(2030, 1, 1, tzinfo=UTC)

    class MutableClock:
        current = created_at

        def __call__(self) -> datetime:
            return self.current

    clock = MutableClock()
    service = shortener_type(clock=clock)
    url = "https://example.com/reviewer-boundary"
    code = service.shorten(url)

    clock.current = created_at + timedelta(hours=24) - timedelta(seconds=1)
    assert service.resolve(code) == url
    assert service.redirect_count(code) == 1

    clock.current = created_at + timedelta(hours=24)
    with pytest.raises(error_type):
        service.resolve(code)
    with pytest.raises(error_type):
        service.redirect_count(code)

    clock.current = created_at + timedelta(hours=24, seconds=1)
    with pytest.raises(error_type):
        service.resolve(code)
    with pytest.raises(error_type):
        service.redirect_count(code)


def test_reviewer_artifacts_are_complete_network_free_and_byte_deterministic(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_run = run_ambiguity_demo(
        tmp_path / "first-workspaces",
        source_root=BROWNFIELD_PROJECT,
        artifact_dir=first,
    )
    second_run = run_ambiguity_demo(
        tmp_path / "second-workspaces",
        source_root=BROWNFIELD_PROJECT,
        artifact_dir=second,
    )

    assert _artifact_bytes(first) == _artifact_bytes(second)
    assert first_run.exported_application_test_count == 20
    assert second_run.exported_application_test_count == 20
    assert first_run.analyst.model_name == "deterministic-ambiguity-analyst"
    assert first_run.planner.model_name == "deterministic-ambiguity-planner"
    assert first_run.executor.model_name == "deterministic-ambiguity-expiration-executor"
    assert set(path.name for path in first.iterdir() if path.is_file()) == {
        *ARTIFACT_FILENAMES,
        "ambiguity_resolution.json",
        "workspace_seed.json",
    }
    resolution = json.loads((first / "ambiguity_resolution.json").read_text())
    assert resolution["raw_requirement"] == AMBIGUITY_RAW_REQUIREMENT
    assert resolution["human_review"]["feedback"] == HUMAN_EXPIRATION_CLARIFICATION
    assert resolution["planning_attempts"][0] == {
        "attempt": 1,
        "source": "requirement_analysis_revision_0",
        "status": "BLOCKED",
        "reason": "UNRESOLVED_REQUIREMENT_AMBIGUITY",
        "planner_invoked": False,
    }
    assert resolution["planning_attempts"][1]["status"] == "PLANNED"
    assert resolution["planning_attempts"][1]["reason"] == "CLARIFICATION_RESOLVED"
    assert resolution["planning_attempts"][1]["planner_invoked"] is True
    assert resolution["task_graph"]["requirement_spec_id"] == (
        resolution["approved_requirement_spec"]["spec_id"]
    )
    assert resolution["execution"]["workspace_integrity"] == "VERIFIED"
    assert resolution["execution"]["exported_application_validation"] == {
        "status": "PASSED",
        "test_count": 20,
        "network_required": False,
        "api_credentials_required": False,
    }
    summary = (first / "summary.md").read_text()
    for heading in (
        "## Scenario",
        "## Before clarification",
        "## Human decision",
        "## After clarification",
        "## Downstream consequence",
        "## Governed execution",
        "## Scope boundary",
    ):
        assert heading in summary
    assert HUMAN_EXPIRATION_CLARIFICATION in summary
    assert "3 MODIFY" in summary
    assert "Final workspace integrity: `VERIFIED`" in summary
    assert "sleep(" not in EXPIRATION_SERVICE
    assert "sleep(" not in EXPIRATION_TESTS
