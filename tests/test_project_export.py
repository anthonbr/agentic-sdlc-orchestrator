"""Tests for governed durable-project promotion."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pytest import MonkeyPatch, mark, raises

import agentic_sdlc.project_export as export_module
from agentic_sdlc.project_export import (
    ProjectExportIssueCode,
    ProjectExportRequest,
    ProjectExportStatus,
    ProjectExporter,
    ProjectNameError,
    normalize_project_name,
    project_export_request_from_state,
)
from agentic_sdlc.state import WorkflowState
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
    return ProjectExportRequest(
        run_id=run_id,
        workspace=workspace,
        session=session,
        authoritative_snapshot=snapshot,
        workflow_status="success",
        exit_gate_passed=True,
        requested_project_name=requested_project_name,
        workflow_project_name=workflow_project_name,
        export_root=tmp_path / "projects",
    )


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
    for relative_path, expected in files.items():
        assert (result.destination_directory / relative_path).read_bytes() == expected
    exported_snapshot = snapshot_directory_tree(
        result.destination_directory,
        workspace_id=request.session.workspace_id,
    )
    assert exported_snapshot == request.authoritative_snapshot


def test_explicit_existing_destination_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    request = _verified_request(tmp_path)
    existing = request.export_root / "my-project"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")

    result = ProjectExporter().export(request)

    assert result.status is ProjectExportStatus.FAILED
    assert result.issue_code is ProjectExportIssueCode.DESTINATION_EXISTS
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert tuple(request.export_root.glob(".my-project.staging-*")) == ()


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
        workflow_status=workflow_status,
        exit_gate_passed=exit_gate_passed,
        requested_project_name=request.requested_project_name,
        workflow_project_name=request.workflow_project_name,
        export_root=request.export_root,
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
        workflow_status=request.workflow_status,
        exit_gate_passed=request.exit_gate_passed,
        requested_project_name=request.requested_project_name,
        workflow_project_name=request.workflow_project_name,
        export_root=request.export_root,
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


def test_state_request_selects_exact_authoritative_snapshot(tmp_path: Path) -> None:
    request = _verified_request(tmp_path)
    state: WorkflowState = {
        "run_id": request.run_id,
        "project_name": "State Project",
        "governed_workspace_session": request.session,
        "workspace_snapshots": [request.authoritative_snapshot],
        "workflow_status": "success",
        "exit_gate_passed": True,
    }

    rebuilt = project_export_request_from_state(
        state,
        workspace=request.workspace,
        export_root=tmp_path / "durable",
    )

    assert rebuilt.workspace is request.workspace
    assert rebuilt.session is request.session
    assert rebuilt.authoritative_snapshot is request.authoritative_snapshot
    assert rebuilt.workflow_project_name == "State Project"
