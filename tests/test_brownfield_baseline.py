"""Focused tests for first-class published-project brownfield baselines."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunLifecycleError,
    GovernedRunMode,
    GovernedRunRequest,
    GovernedRunService,
)
from agentic_sdlc.brownfield_baseline import (
    BrownfieldBaselineProvenance,
    PublishedProjectBaselineError,
    PublishedProjectBaselineIssueCode,
    PublishedProjectCatalog,
    brownfield_baseline_from_value,
    build_brownfield_baseline_provenance,
)
from agentic_sdlc.brownfield_context import BrownfieldCodebaseContext
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.project_delivery import ProjectDeliveryMode
from agentic_sdlc.project_export import (
    ProjectExportRequest,
    ProjectExporter,
)
from agentic_sdlc.run_artifacts import (
    LiveRunArtifactBundle,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.requirement_analysis import (
    BrownfieldImpactAnalysis,
    BrownfieldImpactItem,
    RequirementAnalysis,
)
from agentic_sdlc.state import demo_input
from agentic_sdlc.workspace_integration import (
    GovernedWorkspaceRuntime,
    WorkspaceIntegrationError,
    establish_governed_workspace_session,
)
from agentic_sdlc.workspace_contracts import WorkspaceChangeOperation
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    snapshot_isolated_workspace,
)
from agentic_sdlc.workspace_seeding import (
    seed_isolated_workspace_from_approved_files,
)
from tests.test_application import _service
from tests.test_workspace_contracts import _artifact, _build_change_set, _validation
from tests.test_workflow import _analysis, _proposal


ENGINEERING_FILES = {
    "README.md": b"# Published Project\n",
    "src/service.py": b"VALUE = 1\n",
    "tests/test_service.py": b"def test_value():\n    assert 1 == 1\n",
}


class _BaselineAwareRequirementAnalysisClient(FakeRequirementAnalysisClient):
    """Return a proposal correlated to the application-supplied context."""

    def __init__(self) -> None:
        super().__init__([])

    def invoke_structured(
        self,
        raw_requirement: str,
        prior_analysis: RequirementAnalysis | None,
        human_feedback: str,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        assert brownfield_codebase_context is not None
        self.calls.append(
            {
                "raw_requirement": raw_requirement,
                "prior_analysis": prior_analysis,
                "human_feedback": human_feedback,
                "brownfield_codebase_context": brownfield_codebase_context,
            }
        )
        value = _analysis().model_dump(mode="json")
        value["requirement_type"] = "brownfield"
        value["brownfield_impact"] = BrownfieldImpactAnalysis(
            baseline_id=brownfield_codebase_context.baseline_id,
            codebase_context_id=brownfield_codebase_context.context_id,
            impacted_modules=(
                BrownfieldImpactItem(
                    target="src/service.py",
                    reason="The requested behavior extends the existing service.",
                ),
            ),
            preserved_behaviors=(
                BrownfieldImpactItem(
                    target="existing service behavior",
                    reason="Unchanged baseline behavior must remain compatible.",
                ),
            ),
        )
        return RequirementAnalysis.model_validate(value)


def _publish_project(
    repository_root: Path,
    *,
    project_name: str = "published-project",
    run_id: str = "published-run",
    files: dict[str, bytes] | None = None,
) -> Path:
    workspace_parent = repository_root / "publication-workspaces"
    workspace_parent.mkdir(exist_ok=True)
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)
    workspace = runtime.establish_workspace_for_run(run_id)
    for relative_path, contents in (files or ENGINEERING_FILES).items():
        destination = workspace.root.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    session, snapshot = establish_governed_workspace_session(
        workspace,
        run_id=run_id,
    )
    artifact_bundle = LiveRunArtifactBundle.under_repository(
        repository_root,
        run_id,
    )
    artifact_bundle.artifact_dir.mkdir(parents=True)
    (artifact_bundle.artifact_dir / "workspace_execution.json").write_text(
        json.dumps(
            {
                "session": session.model_dump(mode="json"),
                "snapshots": [snapshot.model_dump(mode="json")],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_bundle.artifact_dir / "summary.md").write_text(
        "# Successful governed publication\n",
        encoding="utf-8",
    )
    write_sdlc_artifact_manifest(
        {
            "run_id": run_id,
            "project_name": "Workflow Project",
            "project_delivery_policy": {"mode": "ENGINEERING_ARTIFACTS"},
            "workflow_status": "success",
            "exit_gate_passed": True,
        },
        artifact_bundle,
    )
    result = ProjectExporter().export(
        ProjectExportRequest(
            run_id=run_id,
            workspace=workspace,
            session=session,
            authoritative_snapshot=snapshot,
            artifact_bundle=artifact_bundle,
            workflow_status="success",
            exit_gate_passed=True,
            requested_project_name=project_name,
            workflow_project_name="Workflow Project",
            export_root=repository_root / "projects",
            project_delivery_policy=ProjectDeliveryMode.ENGINEERING_ARTIFACTS,
        )
    )
    assert result.succeeded
    assert result.destination_directory is not None
    return result.destination_directory


def _seed_selected_baseline(
    tmp_path: Path,
    *,
    workspace_id: str,
) -> tuple[BrownfieldBaselineProvenance, IsolatedWorkspace]:
    catalog = PublishedProjectCatalog(tmp_path)
    selected = catalog.select("published-project")
    runtime = GovernedWorkspaceRuntime(parent_directory=tmp_path / "workspaces")
    (tmp_path / "workspaces").mkdir(exist_ok=True)
    workspace = runtime.establish_workspace_for_run(workspace_id)
    seed_result, snapshot = seed_isolated_workspace_from_approved_files(
        workspace,
        source_root=selected.project_root,
        source_root_label="projects/published-project",
        relative_paths=tuple(item.path for item in selected.engineering_files),
    )
    return (
        build_brownfield_baseline_provenance(selected, seed_result, snapshot),
        workspace,
    )


def test_catalog_enumerates_only_successful_evidence_backed_projects(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    projects_root = tmp_path / "projects"
    (projects_root / "unrelated-directory").mkdir()
    (projects_root / "ordinary-file").write_text("not a project\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (projects_root / "linked-project").symlink_to(outside, target_is_directory=True)

    eligible = PublishedProjectCatalog(tmp_path).eligible_projects()

    assert [item.project_name for item in eligible] == ["published-project"]
    assert eligible[0].project_root == project
    assert eligible[0].originating_run_id == "published-run"
    assert eligible[0].workflow_project_name == "Workflow Project"


def test_projection_uses_authoritative_inventory_and_excludes_sdlc_and_extras(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    (project / ".venv").mkdir()
    (project / ".venv" / "local-only.txt").write_text("local\n")
    (project / ".DS_Store").write_bytes(b"local metadata")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (project / "untracked-link").symlink_to(outside)

    baseline = PublishedProjectCatalog(tmp_path).select("published-project")

    assert tuple(item.path for item in baseline.engineering_files) == (
        "README.md",
        "src/service.py",
        "tests/test_service.py",
    )
    assert all(
        not item.path.startswith("sdlc-artifacts/")
        for item in baseline.engineering_files
    )
    assert all(".venv" not in item.path for item in baseline.engineering_files)
    assert all(".DS_Store" not in item.path for item in baseline.engineering_files)
    assert all("untracked-link" not in item.path for item in baseline.engineering_files)


@pytest.mark.parametrize(
    "project_name",
    ("../published-project", "published/project", "published\\project", ".."),
)
def test_catalog_rejects_path_traversal_and_unsafe_names(
    tmp_path: Path,
    project_name: str,
) -> None:
    _publish_project(tmp_path)

    with pytest.raises(PublishedProjectBaselineError) as raised:
        PublishedProjectCatalog(tmp_path).select(project_name)

    assert raised.value.code is PublishedProjectBaselineIssueCode.INVALID_PROJECT_NAME


def test_catalog_rejects_missing_and_symlinked_project_roots(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (projects / "linked-project").symlink_to(outside, target_is_directory=True)
    catalog = PublishedProjectCatalog(tmp_path)

    with pytest.raises(PublishedProjectBaselineError) as missing:
        catalog.select("missing-project")
    with pytest.raises(PublishedProjectBaselineError) as linked:
        catalog.select("linked-project")

    assert missing.value.code is PublishedProjectBaselineIssueCode.PROJECT_NOT_FOUND
    assert linked.value.code is PublishedProjectBaselineIssueCode.PROJECT_ROOT_SYMLINK


def test_catalog_rejects_malformed_or_incomplete_publication_evidence(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    manifest_path = project / "sdlc-artifacts" / "manifest.json"
    manifest_path.write_text("{not-json\n")
    catalog = PublishedProjectCatalog(tmp_path)

    with pytest.raises(PublishedProjectBaselineError) as raised:
        catalog.select("published-project")

    assert raised.value.code is PublishedProjectBaselineIssueCode.EVIDENCE_INVALID
    assert catalog.eligible_projects() == ()


def test_catalog_rejects_authoritative_file_drift_symlink_and_special_entry(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    (project / "README.md").write_text("changed after publication\n")

    with pytest.raises(PublishedProjectBaselineError) as drifted:
        PublishedProjectCatalog(tmp_path).select("published-project")
    assert drifted.value.code is PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT

    (project / "README.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("# Published Project\n")
    (project / "README.md").symlink_to(outside)
    with pytest.raises(PublishedProjectBaselineError) as linked:
        PublishedProjectCatalog(tmp_path).select("published-project")
    assert linked.value.code is PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT

    if hasattr(os, "mkfifo"):
        (project / "README.md").unlink()
        os.mkfifo(project / "README.md")
        with pytest.raises(PublishedProjectBaselineError) as special:
            PublishedProjectCatalog(tmp_path).select("published-project")
        assert (
            special.value.code
            is PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT
        )


def test_output_protection_rejects_baseline_existing_and_unsafe_destinations(
    tmp_path: Path,
) -> None:
    _publish_project(tmp_path)
    (tmp_path / "projects" / "already-exists").mkdir()
    catalog = PublishedProjectCatalog(tmp_path)

    assert catalog.require_available_output(
        "enhanced-project",
        baseline_project_name="published-project",
    ) == "enhanced-project"
    for output in ("published-project", "already-exists"):
        with pytest.raises(PublishedProjectBaselineError) as raised:
            catalog.require_available_output(
                output,
                baseline_project_name="published-project",
            )
        assert raised.value.code is PublishedProjectBaselineIssueCode.DESTINATION_EXISTS
    with pytest.raises(PublishedProjectBaselineError) as unsafe:
        catalog.require_available_output(
            "../escape",
            baseline_project_name="published-project",
        )
    assert unsafe.value.code is PublishedProjectBaselineIssueCode.INVALID_PROJECT_NAME


def test_seeding_retains_verified_lineage_and_does_not_modify_source(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    source_before = {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    }

    first, first_workspace = _seed_selected_baseline(
        tmp_path,
        workspace_id="brownfield-run-one",
    )
    second, second_workspace = _seed_selected_baseline(
        tmp_path,
        workspace_id="brownfield-run-two",
    )

    assert first.originating_run_id == "published-run"
    assert first.publication_bundle_sha256
    assert first.source_snapshot_id
    assert first.seed_result.verified is True
    assert first.governed_baseline_snapshot_id == (
        first.seed_result.baseline_snapshot_id
    )
    assert first.seed_result.workspace_id != second.seed_result.workspace_id
    assert first.baseline_id != second.baseline_id
    assert first_workspace.root != second_workspace.root
    assert snapshot_isolated_workspace(first_workspace).snapshot_id == (
        first.governed_baseline_snapshot_id
    )
    assert snapshot_isolated_workspace(second_workspace).snapshot_id == (
        second.governed_baseline_snapshot_id
    )
    assert {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    } == source_before
    for path, expected in source_before.items():
        assert (first_workspace.root / path).read_bytes() == expected
        assert (second_workspace.root / path).read_bytes() == expected


def test_provenance_contract_rejects_mismatched_seed_hashes(tmp_path: Path) -> None:
    _publish_project(tmp_path)
    provenance, _ = _seed_selected_baseline(
        tmp_path,
        workspace_id="brownfield-contract",
    )
    value = provenance.model_dump(mode="json")
    value["engineering_files"][0]["content_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="seed evidence"):
        BrownfieldBaselineProvenance.model_validate_json(json.dumps(value))


def test_seeded_snapshot_reuses_create_modify_and_no_change_derivation(
    tmp_path: Path,
) -> None:
    _publish_project(tmp_path)
    _, workspace = _seed_selected_baseline(
        tmp_path,
        workspace_id="brownfield-change-derivation",
    )
    snapshot = snapshot_isolated_workspace(workspace)
    create = _artifact(path="src/new.py", content="NEW = True\n")
    modify = _artifact(
        path="src/service.py",
        content="VALUE = 2\n",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    unchanged = _artifact(
        path="README.md",
        content="# Published Project\n",
        artifact_id="ARTIFACT-003",
        lineage_id="artifact-lineage-003",
        output_index=3,
    )

    change_set = _build_change_set(
        snapshot,
        _validation(create, modify, unchanged),
        (create, modify, unchanged),
    )
    operations = {
        item.path: item.operation for item in change_set.file_changes
    }

    assert operations == {
        "README.md": WorkspaceChangeOperation.NO_CHANGE,
        "src/new.py": WorkspaceChangeOperation.CREATE,
        "src/service.py": WorkspaceChangeOperation.MODIFY,
    }
    assert snapshot.file_state("tests/test_service.py") is not None


def test_run_request_contract_preserves_greenfield_and_requires_brownfield_identity(
) -> None:
    greenfield = GovernedRunRequest(command="run", workflow_input=demo_input())
    assert greenfield.run_mode is GovernedRunMode.GREENFIELD
    assert greenfield.baseline_project_name is None

    with pytest.raises(ValueError, match="must not specify"):
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            baseline_project_name="published-project",
        )
    with pytest.raises(ValueError, match="baseline"):
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            run_mode=GovernedRunMode.BROWNFIELD,
            requested_project_name="enhanced-project",
        )
    with pytest.raises(ValueError, match="explicit output"):
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            run_mode=GovernedRunMode.BROWNFIELD,
            baseline_project_name="published-project",
        )
    with pytest.raises(ValueError, match="must differ"):
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            run_mode=GovernedRunMode.BROWNFIELD,
            baseline_project_name="Published Project",
            requested_project_name="published-project",
        )


def test_greenfield_application_state_has_no_brownfield_lineage(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="greenfield-compatibility",
    )

    snapshot = service.start_run(
        GovernedRunRequest(command="run", workflow_input=demo_input())
    )

    assert "brownfield_baseline" not in snapshot.workflow_state


def test_application_seeds_baseline_before_session_and_retains_provenance(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    source_before = {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    }
    service, runtime, executor = _service(
        tmp_path,
        analyst=_BaselineAwareRequirementAnalysisClient(),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="brownfield-application",
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

    assert requirement_review.application_status is (
        GovernedRunApplicationStatus.AWAITING_HUMAN
    )
    provenance = brownfield_baseline_from_value(
        requirement_review.workflow_state["brownfield_baseline"]
    )
    workspace = runtime.workspace_for_run(requirement_review.run_id)
    seeded_snapshot = snapshot_isolated_workspace(workspace)
    assert seeded_snapshot.snapshot_id == provenance.governed_baseline_snapshot_id
    assert seeded_snapshot.files == provenance.engineering_files

    graph_review = service.resume_run(
        requirement_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_review.human_gate.gate_token,  # type: ignore[union-attr]
    )
    terminal = service.resume_run(
        graph_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_review.human_gate.gate_token,  # type: ignore[union-attr]
    )

    assert terminal.application_status is GovernedRunApplicationStatus.SUCCEEDED
    session = terminal.workflow_state["governed_workspace_session"]
    assert session.baseline_snapshot_id == provenance.governed_baseline_snapshot_id
    assert terminal.export_result is not None
    assert terminal.export_result.project_name == "enhanced-project"
    workspace_evidence = json.loads(
        (
            terminal.artifact_bundle.artifact_dir / "workspace_execution.json"
        ).read_text(encoding="utf-8")
    )
    assert workspace_evidence["brownfield_baseline"]["baseline_id"] == (
        provenance.baseline_id
    )
    assert workspace_evidence["brownfield_codebase_context"]["baseline_id"] == (
        provenance.baseline_id
    )
    assert executor.calls
    assert {
        item.path for item in executor.calls[0].repository_context.observations
    } == set(ENGINEERING_FILES)
    assert {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    } == source_before


def test_application_rejects_existing_output_before_workflow_or_workspace(
    tmp_path: Path,
) -> None:
    _publish_project(tmp_path)
    (tmp_path / "projects" / "occupied-output").mkdir()
    service, runtime, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="brownfield-output",
    )

    with pytest.raises(GovernedRunLifecycleError, match="already exists"):
        service.start_run(
            GovernedRunRequest(
                command="run",
                workflow_input=demo_input(),
                requested_project_name="occupied-output",
                run_mode=GovernedRunMode.BROWNFIELD,
                baseline_project_name="published-project",
            )
        )
    with pytest.raises(WorkspaceIntegrationError):
        runtime.workspace_for_run("run-brownfield-output")


def test_workflow_factory_failure_creates_no_brownfield_workspace(
    tmp_path: Path,
) -> None:
    project = _publish_project(tmp_path)
    source_before = {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    }
    workspace_parent = tmp_path / "run-workspaces"
    workspace_parent.mkdir()
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)

    def failing_workflow_factory(**_: object) -> object:
        raise RuntimeError("workflow construction failed")

    service = GovernedRunService(
        repository_root=tmp_path,
        workflow_factory=failing_workflow_factory,  # type: ignore[arg-type]
        workspace_runtime_factory=lambda: runtime,
        run_id_factory=lambda command: f"{command}-factory-failure",
    )

    with pytest.raises(RuntimeError, match="workflow construction failed"):
        service.start_run(
            GovernedRunRequest(
                command="run",
                workflow_input=demo_input(),
                requested_project_name="enhanced-project",
                run_mode=GovernedRunMode.BROWNFIELD,
                baseline_project_name="published-project",
            )
        )

    with pytest.raises(WorkspaceIntegrationError):
        runtime.workspace_for_run("run-factory-failure")
    assert tuple(workspace_parent.iterdir()) == ()
    assert {
        path: (project / path).read_bytes() for path in ENGINEERING_FILES
    } == source_before
