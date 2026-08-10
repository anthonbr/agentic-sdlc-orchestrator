"""Focused proof for the completed deterministic governed brownfield demo."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pytest import MonkeyPatch

import agentic_sdlc.nodes as nodes
import agentic_sdlc.requirement_spec as requirement_spec
import agentic_sdlc.task_graph as task_graph
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.state import WorkflowState
from agentic_sdlc.task_execution import (
    TaskExecutionStatus,
    TaskGraphExecutionStatus,
)
from agentic_sdlc.task_graph import TaskMaterializationPolicy
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow
from agentic_sdlc.workspace_contracts import WorkspaceChangeOperation
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDisposition,
    WorkspaceIntegrityStatus,
)
from agentic_sdlc.workspace_mutation import WorkspaceMutationStatus
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedResult,
    seed_isolated_workspace_from_approved_files,
)
from tests.demo_brownfield_scenario import (
    ANALYTICS_APP,
    ANALYTICS_README,
    ANALYTICS_SERVICE,
    ANALYTICS_TESTS,
    BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY,
    BROWNFIELD_IMPACT_ANALYSIS,
    BROWNFIELD_IMPACTED_PATHS,
    BROWNFIELD_RUN_ID,
    BROWNFIELD_SOURCE_LABEL,
    BROWNFIELD_SOURCE_PATHS,
    BROWNFIELD_UNCHANGED_PATHS,
    BrownfieldAnalyticsExecutor,
    BrownfieldRepositoryContextPathProvider,
    brownfield_analysis,
    brownfield_input,
    brownfield_task_graph_proposal,
    export_verified_brownfield_workspace,
    write_brownfield_review_artifacts,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
GREENFIELD_PROJECT = REPOSITORY_ROOT / "artifacts/demo-run/generated-project"
FIXED_TIME = datetime.fromisoformat("2026-08-10T12:00:00+00:00")


class FixedDateTime:
    """Fixed application clock for deterministic canonical identities."""

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        del tz
        return FIXED_TIME


def _run_brownfield(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    artifact_dir: Path | None = None,
    run_id: str = BROWNFIELD_RUN_ID,
) -> tuple[
    WorkflowState,
    WorkspaceSeedResult,
    BrownfieldAnalyticsExecutor,
    GovernedWorkspaceRuntime,
]:
    monkeypatch.setattr(requirement_spec, "datetime", FixedDateTime)
    monkeypatch.setattr(task_graph, "datetime", FixedDateTime)
    monkeypatch.setattr(nodes, "datetime", FixedDateTime)

    tmp_path.mkdir(parents=True, exist_ok=True)
    source_before = _project_bytes(GREENFIELD_PROJECT)
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    workspace = runtime.establish_workspace_for_run(run_id)
    seed_result, seed_snapshot = seed_isolated_workspace_from_approved_files(
        workspace,
        source_root=GREENFIELD_PROJECT,
        source_root_label=BROWNFIELD_SOURCE_LABEL,
        relative_paths=BROWNFIELD_SOURCE_PATHS,
    )
    executor = BrownfieldAnalyticsExecutor()
    workflow = build_workflow(
        FakeRequirementAnalysisClient(
            [brownfield_analysis()], model_name="deterministic-brownfield-analyst"
        ),
        FakeTaskPlanningClient(
            [brownfield_task_graph_proposal()],
            model_name="deterministic-brownfield-planner",
        ),
        executor,
        workspace_runtime=runtime,
        repository_context_path_provider=BrownfieldRepositoryContextPathProvider(),
    )
    state = run_workflow(
        brownfield_input(),
        thread_id=run_id,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    state = resume_workflow(
        run_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    state = resume_workflow(
        run_id,
        {"decision": "APPROVE", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    if artifact_dir is not None:
        final_snapshot = _snapshot_by_id(
            state, state["governed_workspace_session"].authoritative_snapshot_id
        )
        export_verified_brownfield_workspace(
            workspace.root,
            artifact_dir / "enhanced-project",
            final_snapshot,
        )
        write_brownfield_review_artifacts(artifact_dir, state, seed_result)
    assert _project_bytes(GREENFIELD_PROJECT) == source_before
    assert seed_snapshot.snapshot_id == seed_result.baseline_snapshot_id
    return state, seed_result, executor, runtime


def _snapshot_by_id(state: WorkflowState, snapshot_id: str):
    return next(
        snapshot
        for snapshot in state["workspace_snapshots"]
        if snapshot.snapshot_id == snapshot_id
    )


def _project_bytes(root: Path) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in BROWNFIELD_SOURCE_PATHS}


def _project_hashes(root: Path) -> dict[str, str]:
    return {
        path: hashlib.sha256(contents).hexdigest()
        for path, contents in _project_bytes(root).items()
    }


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_completed_brownfield_graph_policies_layers_and_wave_bindings(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, _, executor, _ = _run_brownfield(tmp_path, monkeypatch)

    tasks = state["approved_task_graph"]["tasks"]
    assert [task["task_id"] for task in tasks] == [
        "TASK-001",
        "TASK-002",
        "TASK-003",
        "TASK-004",
        "TASK-005",
    ]
    assert [task["depends_on"] for task in tasks] == [
        [],
        ["TASK-001"],
        ["TASK-001"],
        ["TASK-002", "TASK-003"],
        ["TASK-002", "TASK-003"],
    ]
    assert [task["materialization_policy"] for task in tasks] == [
        TaskMaterializationPolicy.FORBIDDEN,
        TaskMaterializationPolicy.REQUIRED,
        TaskMaterializationPolicy.REQUIRED,
        TaskMaterializationPolicy.REQUIRED,
        TaskMaterializationPolicy.REQUIRED,
    ]
    assert state["task_graph_semantics"]["execution_layers"] == [
        ["TASK-001"],
        ["TASK-002", "TASK-003"],
        ["TASK-004", "TASK-005"],
    ]
    assert [
        [attempt.task_id for attempt in wave.task_attempts]
        for wave in state["task_execution_waves"]
    ] == [
        ["TASK-001"],
        ["TASK-002", "TASK-003"],
        ["TASK-004", "TASK-005"],
    ]
    calls = executor.calls_by_task_id
    assert calls["TASK-002"].workspace_binding == calls["TASK-003"].workspace_binding
    assert calls["TASK-004"].workspace_binding == calls["TASK-005"].workspace_binding
    assert calls["TASK-002"].workspace_binding != calls["TASK-004"].workspace_binding


def test_task_scoped_context_and_dependency_postimages_are_exact(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, seed, executor, _ = _run_brownfield(tmp_path, monkeypatch)
    tasks_by_id = {
        task["task_id"]: task for task in state["approved_task_graph"]["tasks"]
    }
    expected_paths = {
        "TASK-001": BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY["impact_analysis"],
        "TASK-002": BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY["service_analytics"],
        "TASK-003": BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY["analytics_http_api"],
        "TASK-004": (
            "src/url_shortener/app.py",
            "src/url_shortener/service.py",
            "tests/test_service.py",
        ),
        "TASK-005": (
            "README.md",
            "src/url_shortener/app.py",
            "src/url_shortener/service.py",
        ),
    }
    for task_id, request in executor.calls_by_task_id.items():
        observations = request.repository_context.observations
        assert tuple(item.path for item in observations) == expected_paths[task_id]
        snapshot = _snapshot_by_id(state, request.workspace_binding.snapshot_id)
        snapshot_hashes = {item.path: item.content_hash for item in snapshot.files}
        assert all(
            item.content_hash == snapshot_hashes[item.path] for item in observations
        )
        assert request.task.source_key == tasks_by_id[task_id]["source_key"]

    assert executor.calls_by_task_id["TASK-001"].workspace_binding.snapshot_id == (
        seed.baseline_snapshot_id
    )
    task_2_dependencies = executor.calls_by_task_id["TASK-002"].dependency_artifacts
    task_3_dependencies = executor.calls_by_task_id["TASK-003"].dependency_artifacts
    assert [item.task_id for item in task_2_dependencies] == ["TASK-001"]
    assert [item.task_id for item in task_3_dependencies] == ["TASK-001"]
    assert next(
        item.content
        for item in executor.calls_by_task_id[
            "TASK-004"
        ].repository_context.observations
        if item.path == "src/url_shortener/service.py"
    ) == ANALYTICS_SERVICE
    assert next(
        item.content
        for item in executor.calls_by_task_id[
            "TASK-005"
        ].repository_context.observations
        if item.path == "src/url_shortener/app.py"
    ) == ANALYTICS_APP


def test_impact_analysis_is_nonmutating_and_defines_parallel_contract(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, seed, _, _ = _run_brownfield(tmp_path, monkeypatch)
    impact_artifact = next(
        item for item in state["engineering_artifacts"] if item.task_id == "TASK-001"
    )

    assert impact_artifact.content == BROWNFIELD_IMPACT_ANALYSIS
    assert "URLShortener.redirect_count(code: str) -> int" in impact_artifact.content
    assert "recognized before the generic `GET /{code}`" in impact_artifact.content
    assert "holding the existing service" in impact_artifact.content
    assert not any(
        item.task_id == "TASK-001" for item in state["workspace_change_sets"]
    )
    first_wave = state["workspace_execution_waves"][0]
    second_wave = state["workspace_execution_waves"][1]
    assert first_wave.binding.snapshot_id == seed.baseline_snapshot_id
    assert second_wave.binding.snapshot_id == seed.baseline_snapshot_id


def test_four_governed_modifies_have_verified_preimages_and_postimages(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, seed, _, _ = _run_brownfield(tmp_path, monkeypatch)
    expected_by_task = {
        "TASK-002": "src/url_shortener/service.py",
        "TASK-003": "src/url_shortener/app.py",
        "TASK-004": "tests/test_service.py",
        "TASK-005": "README.md",
    }

    assert len(state["workspace_change_sets"]) == 4
    assert len(state["workspace_mutation_results"]) == 4
    assert len(state["workspace_conflict_evidence"]) == 2
    assert all(
        item.analysis.has_conflicts is False
        for item in state["workspace_conflict_evidence"]
    )
    operations = []
    mutation_by_change_set = {
        item.change_set_id: item for item in state["workspace_mutation_results"]
    }
    for change_set in state["workspace_change_sets"]:
        assert len(change_set.file_changes) == 1
        change = change_set.file_changes[0]
        result = mutation_by_change_set[change_set.change_set_id]
        evidence = result.file_evidence[0]
        operations.append(change.operation)
        assert change.path == expected_by_task[change_set.task_id]
        assert change.operation is WorkspaceChangeOperation.MODIFY
        assert result.status is WorkspaceMutationStatus.APPLIED
        assert evidence.expected_preimage_hash == evidence.observed_preimage_hash
        assert evidence.desired_postimage_hash == evidence.observed_postimage_hash
        assert evidence.write_performed is True
        assert result.pre_mutation_snapshot_id is not None
        assert result.post_mutation_snapshot_id is not None
    assert operations == [WorkspaceChangeOperation.MODIFY] * 4
    assert not any(
        operation is WorkspaceChangeOperation.CREATE for operation in operations
    )
    assert state["workspace_snapshots"][0].snapshot_id == seed.baseline_snapshot_id
    assert len(state["workspace_snapshots"]) == 5
    assert len({item.snapshot_id for item in state["workspace_snapshots"]}) == 5


def test_final_workspace_and_export_match_expected_six_file_state(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts"
    state, seed, _, _ = _run_brownfield(
        tmp_path / "workspaces", monkeypatch, artifact_dir=artifact_dir
    )
    session = state["governed_workspace_session"]
    baseline = _snapshot_by_id(state, seed.baseline_snapshot_id)
    final = _snapshot_by_id(state, session.authoritative_snapshot_id)
    baseline_hashes = {item.path: item.content_hash for item in baseline.files}
    final_hashes = {item.path: item.content_hash for item in final.files}

    assert state["workflow_status"] == "success"
    assert state["exit_gate_passed"] is True
    assert state["task_graph_execution"].status is TaskGraphExecutionStatus.SUCCEEDED
    assert all(
        item.status is TaskExecutionStatus.SUCCEEDED
        for item in state["task_graph_execution"].task_states
    )
    assert session.integrity_status is WorkspaceIntegrityStatus.VERIFIED
    assert tuple(final_hashes) == BROWNFIELD_SOURCE_PATHS
    for path in BROWNFIELD_UNCHANGED_PATHS:
        assert final_hashes[path] == baseline_hashes[path]
    for path in BROWNFIELD_IMPACTED_PATHS:
        assert final_hashes[path] != baseline_hashes[path]
    assert _project_hashes(artifact_dir / "enhanced-project") == final_hashes
    assert _project_hashes(GREENFIELD_PROJECT) == baseline_hashes
    assert (artifact_dir / "enhanced-project/src/url_shortener/service.py").read_text() == (
        ANALYTICS_SERVICE
    )
    assert (artifact_dir / "enhanced-project/src/url_shortener/app.py").read_text() == (
        ANALYTICS_APP
    )
    assert (artifact_dir / "enhanced-project/tests/test_service.py").read_text() == (
        ANALYTICS_TESTS
    )
    assert (artifact_dir / "enhanced-project/README.md").read_text() == (
        ANALYTICS_README
    )
    assert all(
        item.disposition is TaskAttemptExitDisposition.SUCCEED_TASK
        for item in state["task_attempt_exit_decisions"]
    )


def test_brownfield_review_artifacts_regenerate_byte_identically(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    first = tmp_path / "first-artifacts"
    second = tmp_path / "second-artifacts"
    _run_brownfield(
        tmp_path / "first-workspaces", monkeypatch, artifact_dir=first
    )
    _run_brownfield(
        tmp_path / "second-workspaces", monkeypatch, artifact_dir=second
    )

    assert _artifact_bytes(first) == _artifact_bytes(second)
    summary = (first / "summary.md").read_text()
    enhanced_readme = (first / "enhanced-project/README.md").read_text()
    assert "Scenario type: BROWNFIELD" in summary
    assert "4 MODIFY, 0 CREATE, 0 DELETE" in summary
    assert "Enhanced-project hashes match final snapshot: VERIFIED" in summary
    assert "## Brownfield codebase reasoning" in summary
    assert "one controlled parallel wave" in summary
    assert "conflict-free" in summary
    assert "originally produced by the\ngoverned greenfield scenario" in enhanced_readme
    assert "transactionally modified four existing files" in enhanced_readme
    assert "transactionally created this project" not in enhanced_readme
    assert (first / "workspace_seed.json").is_file()
