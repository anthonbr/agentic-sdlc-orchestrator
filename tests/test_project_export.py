"""Tests for governed durable-project promotion."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pytest import MonkeyPatch, mark, raises

import agentic_sdlc.project_export as export_module
from agentic_sdlc.project_delivery import ProjectDeliveryMode
from agentic_sdlc.project_export import (
    ProjectExportContractError,
    ProjectExportIssueCode,
    ProjectExportRequest,
    ProjectExportStatus,
    ProjectExporter,
    ProjectNameError,
    normalize_project_name,
    project_export_request_from_state,
)
from agentic_sdlc.run_artifacts import (
    LiveRunArtifactBundle,
    SDLCArtifactFileRecord,
    compute_sdlc_artifact_bundle_sha256,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.state import WorkflowState
from agentic_sdlc.workspace_contracts import build_workspace_snapshot
from agentic_sdlc.workspace_integration import (
    GovernedWorkspaceRuntime,
    establish_governed_workspace_session,
)
from agentic_sdlc.workspace_integration_contracts import WorkspaceIntegrityStatus
from agentic_sdlc.workspace_runtime import snapshot_directory_tree


def _verified_request(
    tmp_path: Path,
    *,
    requested_project_name: str | None = "My Project",
    workflow_project_name: str | None = "Workflow Project",
    files: dict[str, bytes] | None = None,
    run_id: str = "run-123",
) -> ProjectExportRequest:
    workspace_parent = tmp_path / "workspaces"
    workspace_parent.mkdir()
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)
    workspace = runtime.establish_workspace_for_run(run_id)
    for relative_path, contents in (files or {"README.md": b"# Project\n"}).items():
        destination = workspace.root.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    session, snapshot = establish_governed_workspace_session(
        workspace,
        run_id=run_id,
    )
    artifact_bundle = LiveRunArtifactBundle.under_repository(tmp_path, run_id)
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
        "# Test workflow summary\n",
        encoding="utf-8",
    )
    write_sdlc_artifact_manifest(
        {
            "run_id": run_id,
            "project_name": workflow_project_name or "Workflow Project",
            "project_delivery_policy": {"mode": "ENGINEERING_ARTIFACTS"},
            "workflow_status": "success",
            "exit_gate_passed": True,
        },
        artifact_bundle,
    )
    return ProjectExportRequest(
        run_id=run_id,
        workspace=workspace,
        session=session,
        authoritative_snapshot=snapshot,
        artifact_bundle=artifact_bundle,
        workflow_status="success",
        exit_gate_passed=True,
        requested_project_name=requested_project_name,
        workflow_project_name=workflow_project_name,
        export_root=tmp_path / "projects",
        project_delivery_policy=ProjectDeliveryMode.ENGINEERING_ARTIFACTS,
    )


def _manifest_data(request: ProjectExportRequest) -> dict[str, object]:
    return json.loads(
        (request.artifact_bundle.artifact_dir / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


def _write_manifest_data(
    request: ProjectExportRequest,
    manifest: dict[str, object],
    *,
    recompute_bundle_hash: bool,
) -> None:
    if recompute_bundle_hash:
        records = tuple(
            SDLCArtifactFileRecord.model_validate(record)
            for record in manifest["files"]
        )
        policy = manifest["project_delivery_policy"]
        manifest["bundle_sha256"] = compute_sdlc_artifact_bundle_sha256(
            schema_version=str(manifest["schema_version"]),
            run_id=str(manifest["run_id"]),
            project_name=(
                str(manifest["project_name"])
                if manifest["project_name"] is not None
                else None
            ),
            workflow_status=str(manifest["workflow_status"]),
            project_delivery_policy=(
                ProjectDeliveryMode(str(policy)) if policy is not None else None
            ),
            exit_gate_passed=(
                bool(manifest["exit_gate_passed"])
                if manifest["exit_gate_passed"] is not None
                else None
            ),
            files=records,
        )
    (request.artifact_bundle.artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _bundle_bytes(request: ProjectExportRequest) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in request.artifact_bundle.artifact_dir.iterdir()
        if path.is_file() and not path.is_symlink()
    }


@mark.parametrize(
    ("supplied", "expected"),
    (
        ("my-project", "my-project"),
        ("My URL Shortener", "my-url-shortener"),
        ("my_url_shortener", "my-url-shortener"),
        ("  Release 2026  ", "release-2026"),
    ),
)
def test_project_name_normalization(supplied: str, expected: str) -> None:
    assert normalize_project_name(supplied) == expected


@mark.parametrize(
    "supplied",
    (
        "",
        "   ",
        ".",
        "..",
        "../escape",
        "release..candidate",
        "/absolute/project",
        "folder/project",
        "folder\\project",
        "C:\\project",
        "---",
        "東京",
    ),
)
def test_project_name_rejects_unsafe_or_meaningless_values(supplied: str) -> None:
    with raises(ProjectNameError):
        normalize_project_name(supplied)


def test_successful_export_preserves_nested_empty_text_and_binary_files(
    tmp_path: Path,
) -> None:
    files = {
        "README.md": b"# Durable Project\n",
        "empty.txt": b"",
        "src/package/__init__.py": b"",
        "src/package/data.bin": b"\x00\xff\x10binary\n",
        "tests/test_service.py": b"def test_placeholder():\n    assert True\n",
    }
    request = _verified_request(tmp_path, files=files)

    result = ProjectExporter().export(request)

    assert result.status is ProjectExportStatus.SUCCEEDED
    assert result.succeeded is True
    assert result.project_name == "my-project"
    assert result.destination_directory == (tmp_path / "projects" / "my-project")
    assert result.exported_file_count == len(files)
    assert result.packaged_artifact_file_count == 3
    assert result.validation.source_matches_authority is True
    assert result.validation.export_matches_authority is True
    assert result.validation.pre_export_snapshot_id == (
        request.authoritative_snapshot.snapshot_id
    )
    assert result.validation.staged_snapshot_id == (
        request.authoritative_snapshot.snapshot_id
    )
    assert result.validation.post_export_snapshot_id == (
        request.authoritative_snapshot.snapshot_id
    )
    assert result.validation.evidence_source_valid is True
    assert result.validation.staged_evidence_matches is True
    assert result.validation.post_export_evidence_matches is True
    for relative_path, expected in files.items():
        assert (result.destination_directory / relative_path).read_bytes() == expected
    exported_snapshot = snapshot_directory_tree(
        result.destination_directory,
        workspace_id=request.session.workspace_id,
    )
    project_snapshot = build_workspace_snapshot(
        request.session.workspace_id,
        tuple(
            item
            for item in exported_snapshot.files
            if not item.path.startswith("sdlc-artifacts/")
        ),
    )
    assert project_snapshot == request.authoritative_snapshot
    for source in request.artifact_bundle.artifact_dir.iterdir():
        assert (
            result.destination_directory / "sdlc-artifacts" / source.name
        ).read_bytes() == source.read_bytes()


def test_explicit_existing_destination_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    original_bundle = _bundle_bytes(request)
    existing = request.export_root / "my-project"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")

    result = ProjectExporter().export(request)

    assert result.status is ProjectExportStatus.FAILED
    assert result.issue_code is ProjectExportIssueCode.DESTINATION_EXISTS
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not (existing / "sdlc-artifacts").exists()
    assert tuple(request.export_root.glob(".my-project.staging-*")) == ()
    assert _bundle_bytes(request) == original_bundle


def test_default_workflow_project_name_is_used_when_name_is_omitted(
    tmp_path: Path,
) -> None:
    request = _verified_request(
        tmp_path,
        requested_project_name=None,
        workflow_project_name="URL Shortener",
    )

    result = ProjectExporter().export(request)

    assert result.succeeded is True
    assert result.project_name == "url-shortener"
    assert result.destination_directory == request.export_root / "url-shortener"


def test_default_name_falls_back_to_stable_run_derived_slug(tmp_path: Path) -> None:
    request = _verified_request(
        tmp_path,
        requested_project_name=None,
        workflow_project_name="../unsafe",
        run_id="stable-run",
    )
    expected = "project-" + hashlib.sha256(b"stable-run").hexdigest()[:8]

    result = ProjectExporter().export(request)

    assert result.succeeded is True
    assert result.project_name == expected


def test_automatic_name_collision_uses_deterministic_run_suffix(
    tmp_path: Path,
) -> None:
    request = _verified_request(
        tmp_path,
        requested_project_name=None,
        workflow_project_name="URL Shortener",
        run_id="collision-run",
    )
    occupied = request.export_root / "url-shortener"
    occupied.mkdir(parents=True)
    suffix = hashlib.sha256(b"collision-run").hexdigest()[:8]

    result = ProjectExporter().export(request)

    assert result.succeeded is True
    assert result.project_name == f"url-shortener-{suffix}"
    assert result.destination_directory == request.export_root / result.project_name
    assert occupied.is_dir()


@mark.parametrize(
    ("workflow_status", "exit_gate_passed"),
    (
        ("safe_stopped", False),
        ("exit_gate_failed", False),
        ("pending", False),
        ("success", False),
    ),
)
def test_non_successful_workflow_is_ineligible_and_creates_no_export_root(
    tmp_path: Path,
    workflow_status: str,
    exit_gate_passed: bool,
) -> None:
    request = _verified_request(tmp_path)
    request = ProjectExportRequest(
        run_id=request.run_id,
        workspace=request.workspace,
        session=request.session,
        authoritative_snapshot=request.authoritative_snapshot,
        artifact_bundle=request.artifact_bundle,
        workflow_status=workflow_status,
        exit_gate_passed=exit_gate_passed,
        requested_project_name=request.requested_project_name,
        workflow_project_name=request.workflow_project_name,
        export_root=request.export_root,
        project_delivery_policy=request.project_delivery_policy,
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.INELIGIBLE_WORKFLOW
    assert not request.export_root.exists()


def test_unprovable_workspace_is_ineligible(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    request = ProjectExportRequest(
        run_id=request.run_id,
        workspace=request.workspace,
        session=request.session.model_copy(
            update={"integrity_status": WorkspaceIntegrityStatus.UNPROVABLE}
        ),
        authoritative_snapshot=request.authoritative_snapshot,
        artifact_bundle=request.artifact_bundle,
        workflow_status=request.workflow_status,
        exit_gate_passed=request.exit_gate_passed,
        requested_project_name=request.requested_project_name,
        workflow_project_name=request.workflow_project_name,
        export_root=request.export_root,
        project_delivery_policy=request.project_delivery_policy,
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.INELIGIBLE_WORKFLOW
    assert not request.export_root.exists()


def test_workspace_drift_after_authoritative_validation_rejects_export(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    (request.workspace.root / "README.md").write_text(
        "drifted\n",
        encoding="utf-8",
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.SOURCE_INTEGRITY
    assert result.validation.source_matches_authority is False
    assert not request.export_root.exists()


def test_symlink_in_workspace_is_rejected_before_export(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (request.workspace.root / "linked.txt").symlink_to(outside)

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.SOURCE_INTEGRITY
    assert "linked.txt" in result.failure_reason
    assert not request.export_root.exists()


def test_special_file_in_workspace_is_rejected_before_export(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    os.mkfifo(request.workspace.root / "events.fifo")

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.SOURCE_INTEGRITY
    assert "events.fifo" in result.failure_reason
    assert not request.export_root.exists()


@mark.parametrize(
    "reserved_path",
    (
        "sdlc-artifacts",
        "sdlc-artifacts/owned.json",
        "SDLC-ARTIFACTS/owned.json",
    ),
)
def test_authoritative_workspace_reserved_namespace_is_rejected(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    request = _verified_request(
        tmp_path,
        files={"README.md": b"# Project\n", reserved_path: b"not owned\n"},
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.RESERVED_NAMESPACE
    assert "reserved top-level sdlc-artifacts/" in result.failure_reason
    assert not request.export_root.exists()


def test_empty_reserved_workspace_directory_is_rejected(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    (request.workspace.root / "sdlc-artifacts").mkdir()

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.RESERVED_NAMESPACE
    assert not request.export_root.exists()


@mark.parametrize(
    ("defect", "expected_reason"),
    (
        ("missing_manifest", "missing manifest.json"),
        ("wrong_run", "different governed run"),
        ("wrong_project", "project identity differs"),
        ("wrong_policy", "delivery policy differs"),
        ("safe_stopped", "Only successful exit-gated"),
        ("missing_file", "exactly match manifest.json"),
        ("extra_file", "exactly match manifest.json"),
        ("changed_content", "differs from manifest.json"),
        ("changed_size", "differs from manifest.json"),
        ("changed_bundle_hash", "bundle_sha256 is not canonical"),
        ("directory", "not a regular file"),
        ("malformed", "manifest is malformed"),
    ),
)
def test_invalid_live_evidence_bundle_is_rejected_before_publication(
    tmp_path: Path,
    defect: str,
    expected_reason: str,
) -> None:
    request = _verified_request(tmp_path)
    bundle_dir = request.artifact_bundle.artifact_dir
    manifest_path = bundle_dir / "manifest.json"
    manifest = _manifest_data(request)

    if defect == "missing_manifest":
        manifest_path.unlink()
    elif defect == "wrong_run":
        manifest["run_id"] = "another-run"
        _write_manifest_data(request, manifest, recompute_bundle_hash=True)
    elif defect == "wrong_project":
        manifest["project_name"] = "Another Project"
        _write_manifest_data(request, manifest, recompute_bundle_hash=True)
    elif defect == "wrong_policy":
        manifest["project_delivery_policy"] = "RUNNABLE_PROJECT"
        _write_manifest_data(request, manifest, recompute_bundle_hash=True)
    elif defect == "safe_stopped":
        manifest["workflow_status"] = "safe_stopped"
        manifest["exit_gate_passed"] = False
        _write_manifest_data(request, manifest, recompute_bundle_hash=True)
    elif defect == "missing_file":
        (bundle_dir / "summary.md").unlink()
    elif defect == "extra_file":
        (bundle_dir / "unmanifested.txt").write_text("extra\n", encoding="utf-8")
    elif defect == "changed_content":
        (bundle_dir / "summary.md").write_text("changed\n", encoding="utf-8")
    elif defect == "changed_size":
        records = manifest["files"]
        assert isinstance(records, list)
        summary_record = next(
            record for record in records if record["path"] == "summary.md"
        )
        summary_record["size_bytes"] += 1
        _write_manifest_data(request, manifest, recompute_bundle_hash=True)
    elif defect == "changed_bundle_hash":
        manifest["bundle_sha256"] = "0" * 64
        _write_manifest_data(request, manifest, recompute_bundle_hash=False)
    elif defect == "directory":
        (bundle_dir / "unmanifested-directory").mkdir()
    elif defect == "malformed":
        manifest_path.write_text("{not-json\n", encoding="utf-8")
    else:  # pragma: no cover - guards the parametrized fixture itself
        raise AssertionError(defect)

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert expected_reason in result.failure_reason
    assert not request.export_root.exists()


def test_symlink_in_live_evidence_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    outside = tmp_path / "outside-evidence.txt"
    outside.write_text("preserve\n", encoding="utf-8")
    (request.artifact_bundle.artifact_dir / "linked.txt").symlink_to(outside)

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert "not a regular file" in result.failure_reason
    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert not request.export_root.exists()


def test_special_entry_in_live_evidence_is_rejected(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    os.mkfifo(request.artifact_bundle.artifact_dir / "events.fifo")

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert "not a regular file" in result.failure_reason
    assert not request.export_root.exists()


def test_live_evidence_must_bind_the_supplied_authoritative_workspace(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    workspace_evidence_path = (
        request.artifact_bundle.artifact_dir / "workspace_execution.json"
    )
    workspace_evidence = json.loads(
        workspace_evidence_path.read_text(encoding="utf-8")
    )
    workspace_evidence["session"]["authoritative_snapshot_id"] = (
        "WORKSPACE-SNAPSHOT-UNRELATED"
    )
    workspace_evidence_path.write_text(
        json.dumps(workspace_evidence, indent=2) + "\n",
        encoding="utf-8",
    )
    write_sdlc_artifact_manifest(
        {
            "run_id": request.run_id,
            "project_name": request.workflow_project_name or "Project",
            "project_delivery_policy": {"mode": "ENGINEERING_ARTIFACTS"},
            "workflow_status": "success",
            "exit_gate_passed": True,
        },
        request.artifact_bundle,
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert "not bound to the supplied authoritative workspace" in (
        result.failure_reason
    )
    assert not request.export_root.exists()


def test_live_evidence_path_substitution_after_validation_fails_safely(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    outside = tmp_path / "outside-live-evidence"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    moved_bundle = request.artifact_bundle.run_root / "retained-owned-evidence"
    real_copy = export_module._copy_sdlc_artifact_files
    substituted = False

    def substitute_before_copy(*args: object, **kwargs: object) -> None:
        nonlocal substituted
        request.artifact_bundle.artifact_dir.rename(moved_bundle)
        request.artifact_bundle.artifact_dir.symlink_to(
            outside,
            target_is_directory=True,
        )
        substituted = True
        real_copy(*args, **kwargs)

    monkeypatch.setattr(
        export_module,
        "_copy_sdlc_artifact_files",
        substitute_before_copy,
    )

    result = ProjectExporter().export(request)

    assert substituted is True
    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert "path identity changed" in result.failure_reason
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert tuple(outside.iterdir()) == (marker,)
    assert not (request.export_root / "my-project").exists()


def test_staging_evidence_directory_substitution_cannot_redirect_writes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    outside = tmp_path / "outside-staged-evidence"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    real_open = export_module.os.open
    substituted = False

    def substitute_evidence_directory_before_create(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal substituted
        if not substituted and path == "manifest.json" and flags & os.O_CREAT:
            staging = next(request.export_root.glob(".my-project.staging-*"))
            evidence = staging / "sdlc-artifacts"
            evidence.rmdir()
            evidence.symlink_to(outside, target_is_directory=True)
            substituted = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        export_module.os,
        "open",
        substitute_evidence_directory_before_create,
    )
    monkeypatch.setattr(
        export_module.os,
        "supports_dir_fd",
        export_module.os.supports_dir_fd
        | {substitute_evidence_directory_before_create},
    )

    result = ProjectExporter().export(request)

    assert substituted is True
    assert result.issue_code is ProjectExportIssueCode.COPY_FAILED
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not (outside / "manifest.json").exists()
    assert not (request.export_root / "my-project").exists()


def test_symlink_export_root_is_rejected(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    actual_root = tmp_path / "actual-projects"
    actual_root.mkdir()
    request.export_root.symlink_to(actual_root, target_is_directory=True)

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EXPORT_ROOT
    assert tuple(actual_root.iterdir()) == ()


def test_staging_ancestor_substitution_cannot_redirect_file_creation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(
        tmp_path,
        files={"nested/file.txt": b"authoritative\n"},
    )
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    real_open = export_module.os.open
    substituted = False

    def substitute_ancestor_before_create(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal substituted
        if not substituted and flags & os.O_CREAT:
            staging = next(request.export_root.glob(".my-project.staging-*"))
            nested = staging / "nested"
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
            substituted = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(export_module.os, "open", substitute_ancestor_before_create)
    monkeypatch.setattr(
        export_module.os,
        "supports_dir_fd",
        export_module.os.supports_dir_fd | {substitute_ancestor_before_create},
    )

    result = ProjectExporter().export(request)

    assert substituted is True
    assert result.succeeded is False
    assert result.issue_code is ProjectExportIssueCode.COPY_FAILED
    assert not (outside / "file.txt").exists()
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not (request.export_root / "my-project").exists()


def test_destination_substitution_cannot_redirect_staged_promotion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    outside = tmp_path / "outside-destination"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    destination = request.export_root / "my-project"
    real_rename = export_module.os.rename
    substituted = False

    def substitute_destination_before_rename(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal substituted
        if not substituted:
            destination.rmdir()
            destination.symlink_to(outside, target_is_directory=True)
            substituted = True
        real_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        export_module.os,
        "rename",
        substitute_destination_before_rename,
    )
    monkeypatch.setattr(
        export_module.os,
        "supports_dir_fd",
        export_module.os.supports_dir_fd | {substitute_destination_before_rename},
    )

    result = ProjectExporter().export(request)

    assert substituted is True
    assert result.succeeded is False
    assert result.issue_code in {
        ProjectExportIssueCode.COPY_FAILED,
        ProjectExportIssueCode.POST_EXPORT_INTEGRITY,
    }
    assert not (outside / "README.md").exists()
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert destination.is_symlink()
    assert "destination cleanup was not provable" in result.failure_reason


def test_export_fails_closed_without_descriptor_relative_support(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    monkeypatch.setattr(export_module.os, "supports_dir_fd", frozenset())

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EXPORT_ROOT
    assert "descriptor-relative" in result.failure_reason
    assert not request.export_root.exists()


def test_post_copy_mismatch_fails_and_removes_new_destination(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    real_snapshot = export_module.snapshot_directory_tree
    calls = 0

    def snapshot_then_tamper(
        root: Path,
        *,
        workspace_id: str,
    ):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "README.md").write_text("post-copy drift\n", encoding="utf-8")
        return real_snapshot(root, workspace_id=workspace_id)

    monkeypatch.setattr(
        export_module,
        "snapshot_directory_tree",
        snapshot_then_tamper,
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.POST_EXPORT_INTEGRITY
    assert result.validation.export_matches_authority is False
    assert not (request.export_root / "my-project").exists()
    assert tuple(request.export_root.glob(".*.staging-*")) == ()


@mark.parametrize(
    "defect",
    (
        "modified_application",
        "extra_project_file",
        "extra_project_directory",
        "modified_evidence",
        "extra_evidence_file",
    ),
)
def test_staging_composite_projection_defects_fail_and_clean_up(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    defect: str,
) -> None:
    request = _verified_request(tmp_path)
    original_bundle = _bundle_bytes(request)
    real_project_copy = export_module._copy_authoritative_files
    real_evidence_copy = export_module._copy_sdlc_artifact_files

    def tamper_after_project_copy(*args: object, **kwargs: object) -> None:
        real_project_copy(*args, **kwargs)
        staging = next(request.export_root.glob(".my-project.staging-*"))
        if defect == "modified_application":
            (staging / "README.md").write_text("changed\n", encoding="utf-8")
        elif defect == "extra_project_file":
            (staging / "unexplained.txt").write_text("extra\n", encoding="utf-8")
        else:
            (staging / "empty-unexplained").mkdir()

    def tamper_after_evidence_copy(*args: object, **kwargs: object) -> None:
        real_evidence_copy(*args, **kwargs)
        staging = next(request.export_root.glob(".my-project.staging-*"))
        evidence = staging / "sdlc-artifacts"
        if defect == "modified_evidence":
            (evidence / "summary.md").write_text("changed\n", encoding="utf-8")
        else:
            (evidence / "unexplained.txt").write_text("extra\n", encoding="utf-8")

    if defect.startswith("modified_evidence") or defect.startswith("extra_evidence"):
        monkeypatch.setattr(
            export_module,
            "_copy_sdlc_artifact_files",
            tamper_after_evidence_copy,
        )
    else:
        monkeypatch.setattr(
            export_module,
            "_copy_authoritative_files",
            tamper_after_project_copy,
        )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.COPY_FAILED
    assert not (request.export_root / "my-project").exists()
    assert tuple(request.export_root.glob(".*.staging-*")) == ()
    assert _bundle_bytes(request) == original_bundle


@mark.parametrize(
    "defect",
    (
        "modified_application",
        "modified_evidence",
        "missing_evidence",
        "extra_project_file",
        "evidence_symlink",
        "evidence_directory_symlink",
    ),
)
def test_final_composite_projection_defects_fail_and_remove_owned_destination(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    defect: str,
) -> None:
    request = _verified_request(tmp_path)
    original_bundle = _bundle_bytes(request)
    outside = tmp_path / "outside-final-evidence.txt"
    outside.write_text("preserve\n", encoding="utf-8")
    real_promote = export_module._promote_staged_entries

    def promote_then_tamper(*args: object, **kwargs: object) -> None:
        real_promote(*args, **kwargs)
        destination = request.export_root / "my-project"
        evidence = destination / "sdlc-artifacts"
        if defect == "modified_application":
            (destination / "README.md").write_text("changed\n", encoding="utf-8")
        elif defect == "modified_evidence":
            (evidence / "summary.md").write_text("changed\n", encoding="utf-8")
        elif defect == "missing_evidence":
            (evidence / "summary.md").unlink()
        elif defect == "extra_project_file":
            (destination / "unexplained.txt").write_text("extra\n", encoding="utf-8")
        elif defect == "evidence_symlink":
            (evidence / "summary.md").unlink()
            (evidence / "summary.md").symlink_to(outside)
        else:
            evidence.rename(destination / "retained-evidence")
            evidence.symlink_to(outside.parent, target_is_directory=True)

    monkeypatch.setattr(
        export_module,
        "_promote_staged_entries",
        promote_then_tamper,
    )

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.POST_EXPORT_INTEGRITY
    assert not (request.export_root / "my-project").exists()
    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert _bundle_bytes(request) == original_bundle


def test_evidence_copy_failure_cleans_staging_and_retains_live_bundle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    request = _verified_request(tmp_path)
    original_bundle = _bundle_bytes(request)

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise export_module._ProjectExportFailure(
            ProjectExportIssueCode.EVIDENCE_INTEGRITY,
            "controlled evidence copy failure",
        )

    monkeypatch.setattr(export_module, "_copy_sdlc_artifact_files", fail_copy)

    result = ProjectExporter().export(request)

    assert result.issue_code is ProjectExportIssueCode.EVIDENCE_INTEGRITY
    assert not (request.export_root / "my-project").exists()
    assert tuple(request.export_root.glob(".*.staging-*")) == ()
    assert _bundle_bytes(request) == original_bundle


def test_state_request_selects_exact_authoritative_snapshot(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    state: WorkflowState = {
        "run_id": request.run_id,
        "project_name": "State Project",
        "governed_workspace_session": request.session,
        "workspace_snapshots": [request.authoritative_snapshot],
        "project_delivery_policy": {"mode": "RUNNABLE_PROJECT"},
        "workflow_status": "success",
        "exit_gate_passed": True,
    }

    rebuilt = project_export_request_from_state(
        state,
        workspace=request.workspace,
        artifact_bundle=request.artifact_bundle,
        export_root=tmp_path / "durable",
    )

    assert rebuilt.workspace is request.workspace
    assert rebuilt.session is request.session
    assert rebuilt.authoritative_snapshot is request.authoritative_snapshot
    assert rebuilt.artifact_bundle is request.artifact_bundle
    assert rebuilt.workflow_project_name == "State Project"
    assert rebuilt.project_delivery_policy is ProjectDeliveryMode.RUNNABLE_PROJECT


def test_state_request_rejects_missing_project_delivery_policy(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    state: WorkflowState = {
        "run_id": request.run_id,
        "project_name": "State Project",
        "governed_workspace_session": request.session,
        "workspace_snapshots": [request.authoritative_snapshot],
        "workflow_status": "success",
        "exit_gate_passed": True,
    }

    with raises(ProjectExportContractError, match="no project delivery policy"):
        project_export_request_from_state(
            state,
            workspace=request.workspace,
            artifact_bundle=request.artifact_bundle,
            export_root=tmp_path / "durable",
        )


def test_state_request_rejects_invalid_project_delivery_policy(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    state: WorkflowState = {
        "run_id": request.run_id,
        "project_name": "State Project",
        "governed_workspace_session": request.session,
        "workspace_snapshots": [request.authoritative_snapshot],
        "project_delivery_policy": {"mode": "UNKNOWN_POLICY"},
        "workflow_status": "success",
        "exit_gate_passed": True,
    }

    with raises(ProjectExportContractError, match="delivery policy is invalid"):
        project_export_request_from_state(
            state,
            workspace=request.workspace,
            artifact_bundle=request.artifact_bundle,
            export_root=tmp_path / "durable",
        )
