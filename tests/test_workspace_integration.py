"""Tests for governed workspace sessions and bounded repository context."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from pytest import MonkeyPatch, mark, raises

import agentic_sdlc.workspace_integration as integration_module
from agentic_sdlc.task_execution_contracts import (
    TaskExecutionRequest,
    TaskRequirementContext,
)
from agentic_sdlc.task_graph import Task, TaskMaterializationPolicy, TaskType
from agentic_sdlc.workspace_contracts import workspace_file_content_hash
from agentic_sdlc.workspace_integration import (
    DeterministicRepositoryContextPathProvider,
    GovernedWorkspaceRuntime,
    WorkspaceIntegrationError,
    WorkspaceIntegrationIssueCode,
    advance_governed_workspace_session,
    establish_governed_workspace_session,
    provide_repository_context,
)
from agentic_sdlc.workspace_integration_contracts import (
    GovernedWorkspaceSession,
    WorkspaceBinding,
    WorkspaceBoundTaskExecutionRequest,
    WorkspaceIntegrityStatus,
    build_workspace_bound_task_execution_request,
)
from agentic_sdlc.workspace_runtime import (
    create_isolated_workspace,
    snapshot_isolated_workspace,
)


def _request() -> TaskExecutionRequest:
    task = Task(
        task_id="TASK-001",
        lineage_id="task-lineage",
        source_key="task",
        title="Review repository evidence",
        description="Review the explicitly supplied bounded evidence.",
        task_type=TaskType.VALIDATION,
        materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
        depends_on=(),
        requirement_refs=("FR-001",),
        acceptance_criteria_refs=("AC-001",),
        risk_refs=(),
        ambiguity_refs=(),
        expected_outputs=("review",),
    )
    return TaskExecutionRequest(
        request_id="REQUEST-001",
        attempt_id="ATTEMPT-001",
        graph_id="GRAPH-001",
        requirement_spec_id="SPEC-001",
        task_id=task.task_id,
        attempt_number=1,
        task=task,
        requirement_context=TaskRequirementContext(
            normalized_problem_statement="Review bounded repository evidence.",
            requirement_type="brownfield",
            assumptions=(),
            functional_requirements=(),
            nonfunctional_requirements=(),
            constraints=(),
            acceptance_criteria=(),
            risks=(),
            ambiguities=(),
        ),
        dependency_artifacts=(),
    )


def test_session_establishment_binds_real_initial_snapshot(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "README.md").write_text("initial\n")

    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )

    assert session.workspace_id == workspace.workspace_id == snapshot.workspace_id
    assert session.baseline_snapshot_id == snapshot.snapshot_id
    assert session.authoritative_snapshot_id == snapshot.snapshot_id
    assert session.integrity_status is WorkspaceIntegrityStatus.VERIFIED
    assert snapshot.file_state("README.md") is not None
    with raises(ValidationError):
        session.authoritative_snapshot_id = "OTHER"  # type: ignore[misc]


def test_repository_context_records_text_and_nonexistence_canonically(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "src").mkdir()
    (workspace.root / "src/service.py").write_text("def service():\n    pass\n")
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )

    context = provide_repository_context(
        workspace,
        session,
        snapshot,
        ("tests/test_service.py", "src/service.py"),
    )

    assert context.binding == WorkspaceBinding(
        workspace_id=workspace.workspace_id,
        snapshot_id=snapshot.snapshot_id,
    )
    assert tuple(item.path for item in context.observations) == (
        "src/service.py",
        "tests/test_service.py",
    )
    existing, absent = context.observations
    assert existing.content == "def service():\n    pass\n"
    assert existing.content_hash == workspace_file_content_hash(existing.content)
    assert absent.exists is False
    assert absent.content is absent.content_hash is None


def test_repository_context_rejects_duplicate_requested_paths(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(
            workspace, session, snapshot, ("README.md", "README.md")
        )

    assert error.value.code is WorkspaceIntegrationIssueCode.DUPLICATE_PATH


def test_repository_context_rejects_path_outside_repository_policy(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(workspace, session, snapshot, ("../outside",))

    assert error.value.code is WorkspaceIntegrationIssueCode.PATH_POLICY


@mark.parametrize("mismatch", ("workspace", "snapshot", "integrity"))
def test_repository_context_rejects_unverified_authority(
    tmp_path: Path, mismatch: str
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    if mismatch == "workspace":
        session = session.model_copy(update={"workspace_id": "OTHER"})
        expected = WorkspaceIntegrationIssueCode.WORKSPACE_ID
    elif mismatch == "snapshot":
        session = session.model_copy(update={"authoritative_snapshot_id": "OTHER"})
        expected = WorkspaceIntegrationIssueCode.SNAPSHOT_ID
    else:
        session = session.model_copy(
            update={"integrity_status": WorkspaceIntegrityStatus.UNPROVABLE}
        )
        expected = WorkspaceIntegrationIssueCode.INTEGRITY

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(workspace, session, snapshot, ())

    assert error.value.code is expected


def test_repository_context_detects_live_workspace_drift(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "README.md").write_text("before\n")
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    (workspace.root / "README.md").write_text("after\n")

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(
            workspace, session, snapshot, ("README.md",)
        )

    assert error.value.code is WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT


def test_repository_context_detects_drift_during_explicit_read(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "README.md").write_text("bound\n")
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    original = integration_module.read_isolated_workspace_file

    def read_then_drift(workspace_arg: object, path: str) -> bytes | None:
        contents = original(workspace_arg, path)  # type: ignore[arg-type]
        (workspace.root / "drift.txt").write_text("intervening\n")
        return contents

    monkeypatch.setattr(
        integration_module, "read_isolated_workspace_file", read_then_drift
    )

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(workspace, session, snapshot, ("README.md",))

    assert error.value.code is WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT


def test_repository_context_rejects_binary_requested_file(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "asset.bin").write_bytes(b"\xff\xfe\x00")
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(workspace, session, snapshot, ("asset.bin",))

    assert error.value.code is WorkspaceIntegrationIssueCode.NON_TEXT_FILE


def test_repository_context_preserves_runtime_symlink_rejection(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    (workspace.root / "target.txt").write_text("target\n")
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    (workspace.root / "link.txt").symlink_to(workspace.root / "target.txt")

    with raises(WorkspaceIntegrationError) as error:
        provide_repository_context(workspace, session, snapshot, ("link.txt",))

    assert error.value.code in {
        WorkspaceIntegrationIssueCode.RUNTIME,
        WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
    }


def test_workspace_bound_request_requires_one_exact_binding(tmp_path: Path) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    context = provide_repository_context(workspace, session, snapshot, ())
    request = _request()

    bound = build_workspace_bound_task_execution_request(
        request, context.binding, context
    )

    assert bound.request is request
    assert set(WorkspaceBoundTaskExecutionRequest.model_fields) == {
        "request",
        "workspace_binding",
        "repository_context",
    }
    assert "root" not in bound.model_dump(mode="json")
    with raises(ValidationError):
        bound.workspace_binding = WorkspaceBinding(  # type: ignore[misc]
            workspace_id="OTHER", snapshot_id="OTHER"
        )


def test_workspace_bound_request_rejects_mismatched_context_binding(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, snapshot = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    context = provide_repository_context(workspace, session, snapshot, ())

    with raises(ValidationError):
        build_workspace_bound_task_execution_request(
            _request(),
            WorkspaceBinding(workspace_id="OTHER", snapshot_id=snapshot.snapshot_id),
            context,
        )


def test_governed_runtime_reuses_one_capability_per_run(tmp_path: Path) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)

    first = runtime.establish_workspace_for_run("RUN-001")
    second = runtime.workspace_for_run("RUN-001")
    other = runtime.establish_workspace_for_run("RUN-002")

    assert first is second
    assert first != other
    assert first.root.parent == tmp_path.resolve()
    assert other.root.parent == tmp_path.resolve()


def test_governed_runtime_never_replaces_an_unavailable_capability(
    tmp_path: Path,
) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)

    with raises(WorkspaceIntegrationError) as error:
        runtime.workspace_for_run("RUN-MISSING")

    assert error.value.code is WorkspaceIntegrationIssueCode.RUNTIME


def test_governed_runtime_accepts_one_precreated_workspace(tmp_path: Path) -> None:
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path)
    supplied = create_isolated_workspace("WORKSPACE-SUPPLIED", parent_directory=tmp_path)
    (supplied.root / "README.md").write_text("brownfield\n")

    runtime.bind_workspace("RUN-001", supplied)

    assert runtime.workspace_for_run("RUN-001") is supplied
    with raises(WorkspaceIntegrationError) as error:
        runtime.bind_workspace(
            "RUN-001",
            create_isolated_workspace("WORKSPACE-OTHER", parent_directory=tmp_path),
        )
    assert error.value.code is WorkspaceIntegrationIssueCode.WORKSPACE_ID


def test_authoritative_snapshot_advances_without_changing_baseline(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    session, baseline = establish_governed_workspace_session(
        workspace, run_id="RUN-001"
    )
    (workspace.root / "README.md").write_text("created\n")
    postimage = snapshot_isolated_workspace(workspace)

    advanced = advance_governed_workspace_session(session, postimage)

    assert advanced.baseline_snapshot_id == baseline.snapshot_id
    assert advanced.authoritative_snapshot_id == postimage.snapshot_id
    assert advanced.integrity_status is WorkspaceIntegrityStatus.VERIFIED


def test_repository_context_path_provider_is_bounded_and_canonical() -> None:
    provider = DeterministicRepositoryContextPathProvider(("README.md",))

    paths = provider.paths_for_attempt(
        _request().task,
        dependency_paths=("src/service.py",),
        retry_paths=("README.md", "tests/test_service.py"),
    )

    assert paths == ("README.md", "src/service.py", "tests/test_service.py")
