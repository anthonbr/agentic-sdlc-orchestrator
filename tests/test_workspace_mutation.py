"""Tests for isolated transactional workspace mutation and rollback."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from pydantic import ValidationError
from pytest import MonkeyPatch, mark, raises, skip

import agentic_sdlc.workspace_mutation as mutation_module
import agentic_sdlc.workspace_runtime as runtime_module
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    EngineeringArtifactType,
    TaskExecutionValidationResult,
)
from agentic_sdlc.task_graph import Task, TaskMaterializationPolicy, TaskType
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationIntent,
    WorkspaceChangeSet,
    WorkspaceChangeSetIssueCode,
    WorkspaceChangeSetValidationIssue,
    WorkspaceChangeSetValidationResult,
    WorkspaceSnapshot,
    build_workspace_change_set,
    validate_artifact_materialization,
    validate_workspace_change_set,
    workspace_file_content_hash,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationIssueCode,
    WorkspaceMutationStatus,
    apply_workspace_change_set,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    create_isolated_workspace,
    snapshot_isolated_workspace,
)


def _artifact(
    path: str,
    content: str,
    *,
    index: int = 1,
    task_id: str = "TASK-001",
    request_id: str = "REQUEST-001",
    attempt_id: str = "ATTEMPT-001",
) -> EngineeringArtifact:
    artifact_id = f"ARTIFACT-{task_id}-{index:03d}"
    return EngineeringArtifact(
        artifact_id=artifact_id,
        lineage_id=f"lineage-{task_id}-{index:03d}",
        artifact_type=EngineeringArtifactType.SOURCE,
        logical_name=path,
        content=content,
        content_hash=hashlib.sha256(f"canonical:{artifact_id}".encode()).hexdigest(),
        output_index=index,
        requirement_spec_id="SPEC-001",
        graph_id="GRAPH-001",
        task_id=task_id,
        request_id=request_id,
        attempt_id=attempt_id,
        attempt_number=1,
        requirement_refs=("FR-001",),
        acceptance_criteria_refs=("AC-001",),
        risk_refs=(),
        ambiguity_refs=(),
        created_at="2026-08-10T12:00:00+00:00",
    )


def _task_validation(
    *artifacts: EngineeringArtifact,
) -> TaskExecutionValidationResult:
    first = artifacts[0]
    return TaskExecutionValidationResult(
        request_id=first.request_id,
        attempt_id=first.attempt_id,
        task_id=first.task_id,
        passed=True,
        artifact_ids=tuple(item.artifact_id for item in artifacts),
        checks=(),
        errors=(),
    )


def _task(artifact: EngineeringArtifact) -> Task:
    return Task(
        task_id=artifact.task_id,
        lineage_id=f"task-lineage-{artifact.task_id}",
        source_key=artifact.task_id.casefold().replace("-", "_"),
        title="Materialize desired files",
        description="Materialize validated desired repository state.",
        task_type=TaskType.IMPLEMENTATION,
        materialization_policy=TaskMaterializationPolicy.REQUIRED,
        depends_on=(),
        requirement_refs=artifact.requirement_refs,
        acceptance_criteria_refs=artifact.acceptance_criteria_refs,
        risk_refs=artifact.risk_refs,
        ambiguity_refs=artifact.ambiguity_refs,
        expected_outputs=("desired files",),
    )


def _change_set_from_snapshot(
    snapshot: WorkspaceSnapshot,
    *artifacts: EngineeringArtifact,
) -> tuple[WorkspaceChangeSet, WorkspaceChangeSetValidationResult]:
    task_validation = _task_validation(*artifacts)
    intents = tuple(
        ArtifactMaterializationIntent(
            artifact_id=artifact.artifact_id,
            target_path=artifact.logical_name,
        )
        for artifact in artifacts
    )
    materialization = validate_artifact_materialization(
        _task(artifacts[0]), task_validation, artifacts, intents
    )
    assert materialization.passed
    change_set = build_workspace_change_set(
        snapshot,
        task_validation,
        artifacts,
        materialization,
    )
    validation = validate_workspace_change_set(
        change_set, snapshot, artifacts, materialization
    )
    assert validation.passed
    return change_set, validation


def _change_set(
    workspace: IsolatedWorkspace,
    *artifacts: EngineeringArtifact,
) -> tuple[WorkspaceSnapshot, WorkspaceChangeSet, WorkspaceChangeSetValidationResult]:
    snapshot = snapshot_isolated_workspace(workspace)
    change_set, validation = _change_set_from_snapshot(snapshot, *artifacts)
    return snapshot, change_set, validation


def _issue_codes(result: object) -> set[WorkspaceMutationIssueCode]:
    return {item.code for item in result.issues}  # type: ignore[attr-defined]


def test_mutator_rejects_arbitrary_path_and_nonfactory_capability(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )

    path_result = apply_workspace_change_set(  # type: ignore[arg-type]
        workspace.root, change_set, validation
    )
    forged = object.__new__(IsolatedWorkspace)
    forged_result = apply_workspace_change_set(forged, change_set, validation)

    assert path_result.status is WorkspaceMutationStatus.REJECTED
    assert forged_result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(path_result) == {
        WorkspaceMutationIssueCode.INVALID_WORKSPACE_CAPABILITY
    }
    assert not (workspace.root / "created.txt").exists()


@mark.parametrize(
    "tamper",
    ("passed", "issues", "change_set", "workspace", "snapshot"),
)
def test_validation_evidence_mismatch_rejects_without_mutation(
    tmp_path: Path,
    tamper: str,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    if tamper == "passed":
        validation = validation.model_copy(update={"passed": False})
    elif tamper == "issues":
        validation = validation.model_copy(
            update={
                "issues": (
                    WorkspaceChangeSetValidationIssue(
                        code=WorkspaceChangeSetIssueCode.LINEAGE,
                        path=None,
                        detail="test issue",
                    ),
                )
            }
        )
    else:
        field = {
            "change_set": "change_set_id",
            "workspace": "workspace_id",
            "snapshot": "snapshot_id",
        }[tamper]
        validation = validation.model_copy(update={field: "MISMATCH"})

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.VALIDATION_EVIDENCE
    }


@mark.parametrize("tamper", ("content_and_hash", "safe_path"))
def test_current_change_set_identity_rejects_stale_validation_evidence(
    tmp_path: Path,
    tamper: str,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "approved\n")
    )
    change = change_set.file_changes[0]
    if tamper == "content_and_hash":
        changed_content = "unvalidated\n"
        change = change.model_copy(
            update={
                "desired_content": changed_content,
                "desired_content_hash": workspace_file_content_hash(
                    changed_content
                ),
            }
        )
    else:
        change = change.model_copy(update={"path": "also-safe.txt"})
    tampered = change_set.model_copy(update={"file_changes": (change,)})

    result = apply_workspace_change_set(workspace, tampered, validation)

    assert tampered.change_set_id == validation.change_set_id
    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.VALIDATION_EVIDENCE
    }
    assert tuple(workspace.root.iterdir()) == ()
    assert not (workspace.root / "created.txt").exists()


def test_workspace_id_mismatch_rejects_without_mutation(tmp_path: Path) -> None:
    first = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    second = create_isolated_workspace("WORKSPACE-002", parent_directory=tmp_path)
    _, change_set, validation = _change_set(
        first, _artifact("created.txt", "created\n")
    )

    result = apply_workspace_change_set(second, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.VALIDATION_EVIDENCE
    }
    assert tuple(second.root.iterdir()) == ()


def test_mutation_never_targets_orchestrator_repository_implicitly(
    tmp_path: Path,
) -> None:
    control_plane_readme = Path("README.md")
    before = control_plane_readme.read_bytes()
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("README.md", "isolated only\n")
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.APPLIED
    assert (workspace.root / "README.md").read_text() == "isolated only\n"
    assert control_plane_readme.read_bytes() == before


def test_disappeared_workspace_rejects_with_structured_evidence(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    workspace.root.rmdir()

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE
    }


def test_tampered_protected_path_is_rejected_before_mutation(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("safe.txt", "safe\n")
    )
    change = change_set.file_changes[0].model_copy(update={"path": ".env"})
    tampered = change_set.model_copy(update={"file_changes": (change,)})

    result = apply_workspace_change_set(workspace, tampered, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.VALIDATION_EVIDENCE
    }
    assert tuple(workspace.root.iterdir()) == ()


@mark.parametrize("symlink_kind", ("target", "parent"))
def test_symlink_target_or_parent_is_rejected_before_mutation(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    path = "link.txt" if symlink_kind == "target" else "linked/file.txt"
    _, change_set, validation = _change_set(
        workspace, _artifact(path, "desired\n")
    )
    outside = tmp_path / "outside"
    if symlink_kind == "target":
        outside.write_text("outside\n", encoding="utf-8")
        (workspace.root / "link.txt").symlink_to(outside)
    else:
        outside.mkdir()
        (workspace.root / "linked").symlink_to(outside, target_is_directory=True)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {
        WorkspaceMutationIssueCode.SYMLINK_DETECTED
    }
    assert not (outside / "file.txt").exists()


def test_directory_target_and_regular_file_parent_are_rejected_preflight(
    tmp_path: Path,
) -> None:
    directory_workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    (directory_workspace.root / "target").mkdir()
    _, directory_change, directory_validation = _change_set(
        directory_workspace, _artifact("target", "desired\n")
    )

    directory_result = apply_workspace_change_set(
        directory_workspace, directory_change, directory_validation
    )

    parent_workspace = create_isolated_workspace(
        "WORKSPACE-002", parent_directory=tmp_path
    )
    (parent_workspace.root / "parent").write_text("file\n", encoding="utf-8")
    _, parent_change, parent_validation = _change_set(
        parent_workspace, _artifact("parent/child.txt", "desired\n")
    )
    parent_result = apply_workspace_change_set(
        parent_workspace, parent_change, parent_validation
    )

    assert directory_result.status is WorkspaceMutationStatus.REJECTED
    assert parent_result.status is WorkspaceMutationStatus.REJECTED
    assert WorkspaceMutationIssueCode.UNSUPPORTED_FILE_TYPE in _issue_codes(
        directory_result
    )
    assert WorkspaceMutationIssueCode.UNSUPPORTED_FILE_TYPE in _issue_codes(
        parent_result
    )


def test_physical_create_destination_prevents_overwrite(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "desired\n")
    )
    target = workspace.root / "created.txt"
    target.write_text("external\n", encoding="utf-8")

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert WorkspaceMutationIssueCode.STALE_PRECONDITION in _issue_codes(result)
    assert target.read_text() == "external\n"


def test_case_alias_is_rejected_when_host_filesystem_exposes_it(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("src/A.py", "desired\n")
    )
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "a.py").write_text("existing\n", encoding="utf-8")
    if not (workspace.root / "src" / "A.py").exists():
        skip("host filesystem is case-sensitive for this test directory")

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert WorkspaceMutationIssueCode.PATH_CONTAINMENT in _issue_codes(result)
    assert (workspace.root / "src" / "a.py").read_text() == "existing\n"


def test_create_nested_file_writes_exact_utf8_and_records_parents(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    desired = "snowman: ☃\n"
    _, change_set, validation = _change_set(
        workspace, _artifact("src/pkg/service.py", desired)
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    target = workspace.root / "src" / "pkg" / "service.py"
    assert result.status is WorkspaceMutationStatus.APPLIED
    assert target.read_bytes() == desired.encode("utf-8")
    assert result.file_evidence[0].write_performed is True
    assert result.file_evidence[0].created_parent_paths == ("src", "src/pkg")
    assert result.file_evidence[0].observed_postimage_hash == (
        workspace_file_content_hash(desired)
    )
    assert result.post_mutation_snapshot_id == snapshot_isolated_workspace(
        workspace
    ).snapshot_id


def test_modify_preserves_mode_and_verifies_preimage_and_postimage(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "tool.sh"
    target.write_bytes(b"#!/bin/sh\necho before\n")
    target.chmod(0o751)
    _, change_set, validation = _change_set(
        workspace,
        _artifact("tool.sh", "#!/bin/sh\necho after\n"),
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.APPLIED
    assert target.read_bytes() == b"#!/bin/sh\necho after\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    evidence = result.file_evidence[0]
    assert evidence.observed_preimage_hash == workspace_file_content_hash(
        "#!/bin/sh\necho before\n"
    )
    assert evidence.observed_postimage_hash == workspace_file_content_hash(
        "#!/bin/sh\necho after\n"
    )


def test_stale_modify_is_rejected_before_write(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "service.py"
    target.write_text("before\n", encoding="utf-8")
    _, change_set, validation = _change_set(
        workspace, _artifact("service.py", "after\n")
    )
    target.write_text("external\n", encoding="utf-8")

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert WorkspaceMutationIssueCode.STALE_PRECONDITION in _issue_codes(result)
    assert target.read_text() == "external\n"


def test_failed_modify_staging_cleanup_removes_and_verifies_temporary_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "service.py"
    target.write_text("before\n", encoding="utf-8")
    _, change_set, validation = _change_set(
        workspace, _artifact("service.py", "after\n")
    )

    def fail_staged_write(descriptor: int, contents: bytes) -> None:
        os.write(descriptor, b"partial")
        raise OSError("injected staging write failure")

    monkeypatch.setattr(mutation_module, "_write_all", fail_staged_write)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.REJECTED
    assert _issue_codes(result) == {WorkspaceMutationIssueCode.MODIFY_FAILURE}
    assert target.read_text() == "before\n"
    assert tuple(workspace.root.glob(".agentic-sdlc-mutation-*")) == ()


def test_failed_modify_staging_cleanup_can_complete_during_rollback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "service.py"
    target.write_text("before\n", encoding="utf-8")
    _, change_set, validation = _change_set(
        workspace, _artifact("service.py", "after\n")
    )
    real_remove = mutation_module._remove_staging_file
    removal_calls = 0

    def fail_staged_write(descriptor: int, contents: bytes) -> None:
        os.write(descriptor, b"partial")
        raise OSError("injected staging write failure")

    def fail_initial_removal(path: Path) -> None:
        nonlocal removal_calls
        removal_calls += 1
        if removal_calls == 1:
            raise OSError("injected initial staging cleanup failure")
        real_remove(path)

    monkeypatch.setattr(mutation_module, "_write_all", fail_staged_write)
    monkeypatch.setattr(
        mutation_module, "_remove_staging_file", fail_initial_removal
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert removal_calls == 2
    assert target.read_text() == "before\n"
    assert tuple(workspace.root.glob(".agentic-sdlc-mutation-*")) == ()
    assert result.file_evidence[0].rollback_verified is True


def test_failed_modify_staging_residue_is_rollback_failed_not_rejected(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "service.py"
    target.write_text("before\n", encoding="utf-8")
    _, change_set, validation = _change_set(
        workspace, _artifact("service.py", "after\n")
    )

    def fail_staged_write(descriptor: int, contents: bytes) -> None:
        os.write(descriptor, b"partial")
        raise OSError("injected staging write failure")

    def refuse_staging_removal(path: Path) -> None:
        raise OSError("injected persistent staging cleanup failure")

    monkeypatch.setattr(mutation_module, "_write_all", fail_staged_write)
    monkeypatch.setattr(
        mutation_module, "_remove_staging_file", refuse_staging_removal
    )

    result = apply_workspace_change_set(workspace, change_set, validation)
    staging_files = tuple(workspace.root.glob(".agentic-sdlc-mutation-*"))

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert result.status is not WorkspaceMutationStatus.REJECTED
    assert result.status is not WorkspaceMutationStatus.APPLIED
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)
    assert WorkspaceMutationIssueCode.ROLLBACK_VERIFICATION_FAILURE in _issue_codes(
        result
    )
    assert target.read_text() == "before\n"
    assert len(staging_files) == 1
    assert staging_files[0].read_bytes() == b"partial"
    assert staging_files[0].name not in result.model_dump_json()
    assert str(workspace.root) not in result.model_dump_json()


def test_no_change_only_verifies_without_replacing_file(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "stable.txt"
    target.write_text("stable\n", encoding="utf-8")
    before = target.stat()
    _, change_set, validation = _change_set(
        workspace, _artifact("stable.txt", "stable\n")
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    after = target.stat()
    assert result.status is WorkspaceMutationStatus.APPLIED
    assert result.file_evidence[0].write_performed is False
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns
    assert target.read_text() == "stable\n"


def test_mixed_create_modify_and_no_change_transaction(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    (workspace.root / "modify.txt").write_text("before\n", encoding="utf-8")
    (workspace.root / "stable.txt").write_text("same\n", encoding="utf-8")
    artifacts = (
        _artifact("stable.txt", "same\n", index=3),
        _artifact("created.txt", "created\n", index=1),
        _artifact("modify.txt", "after\n", index=2),
    )
    _, change_set, validation = _change_set(workspace, *artifacts)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.APPLIED
    assert (workspace.root / "created.txt").read_text() == "created\n"
    assert (workspace.root / "modify.txt").read_text() == "after\n"
    assert (workspace.root / "stable.txt").read_text() == "same\n"
    assert tuple(item.path for item in result.file_evidence) == (
        "created.txt",
        "modify.txt",
        "stable.txt",
    )
    assert tuple(item.write_performed for item in result.file_evidence) == (
        True,
        True,
        False,
    )


def test_disjoint_same_base_change_sets_apply_after_global_snapshot_drift(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    (workspace.root / "a.txt").write_text("a-before\n", encoding="utf-8")
    (workspace.root / "b.txt").write_text("b-before\n", encoding="utf-8")
    base = snapshot_isolated_workspace(workspace)
    artifact_a = _artifact("a.txt", "a-after\n")
    artifact_b = _artifact(
        "b.txt",
        "b-after\n",
        task_id="TASK-002",
        request_id="REQUEST-002",
        attempt_id="ATTEMPT-002",
    )
    change_a, validation_a = _change_set_from_snapshot(base, artifact_a)
    change_b, validation_b = _change_set_from_snapshot(base, artifact_b)

    result_a = apply_workspace_change_set(workspace, change_a, validation_a)
    drifted = snapshot_isolated_workspace(workspace)
    result_b = apply_workspace_change_set(workspace, change_b, validation_b)

    assert result_a.status is WorkspaceMutationStatus.APPLIED
    assert drifted.snapshot_id != base.snapshot_id
    assert result_b.status is WorkspaceMutationStatus.APPLIED
    assert result_b.pre_mutation_snapshot_id == drifted.snapshot_id
    assert (workspace.root / "a.txt").read_text() == "a-after\n"
    assert (workspace.root / "b.txt").read_text() == "b-after\n"


def test_failure_after_prior_mutation_rolls_back_bytes_mode_file_and_directories(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    modified = workspace.root / "a_modify.sh"
    original = b"#!/bin/sh\necho before\n"
    modified.write_bytes(original)
    modified.chmod(0o751)
    artifacts = (
        _artifact("a_modify.sh", "#!/bin/sh\necho after\n", index=1),
        _artifact("nested/b_create.txt", "created\n", index=2),
    )
    base, change_set, validation = _change_set(workspace, *artifacts)
    real_write = mutation_module._write_all
    calls = 0

    def fail_second_write(descriptor: int, contents: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.write(descriptor, b"partial")
            raise OSError("injected create failure")
        real_write(descriptor, contents)

    monkeypatch.setattr(mutation_module, "_write_all", fail_second_write)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert modified.read_bytes() == original
    assert stat.S_IMODE(modified.stat().st_mode) == 0o751
    assert not (workspace.root / "nested" / "b_create.txt").exists()
    assert not (workspace.root / "nested").exists()
    assert result.rollback_snapshot_id == base.snapshot_id
    assert all(item.rollback_attempted for item in result.file_evidence)
    assert all(item.rollback_verified for item in result.file_evidence)


def test_rollback_failure_is_explicit_and_never_recursively_deletes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    artifacts = (
        _artifact("a_created.txt", "created\n", index=1),
        _artifact("nested/b_created.txt", "second\n", index=2),
    )
    _, change_set, validation = _change_set(workspace, *artifacts)
    real_write = mutation_module._write_all
    calls = 0

    def fail_second_write(descriptor: int, contents: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.write(descriptor, b"partial")
            raise OSError("injected create failure")
        real_write(descriptor, contents)

    def refuse_first_file_removal(path: Path) -> None:
        if path.name == "a_created.txt":
            (workspace.root / "unexpected.txt").write_text(
                "external\n", encoding="utf-8"
            )
            raise OSError("injected rollback failure")
        os.unlink(path)

    monkeypatch.setattr(mutation_module, "_write_all", fail_second_write)
    monkeypatch.setattr(
        mutation_module, "_remove_created_file", refuse_first_file_removal
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)
    assert WorkspaceMutationIssueCode.ROLLBACK_VERIFICATION_FAILURE in _issue_codes(
        result
    )
    assert result.issues == tuple(
        sorted(
            result.issues,
            key=lambda item: (item.path or "", item.code.value, item.detail),
        )
    )
    assert (workspace.root / "a_created.txt").exists()
    assert (workspace.root / "unexpected.txt").read_text() == "external\n"
    assert not (workspace.root / "nested" / "b_created.txt").exists()


def test_postimage_failure_triggers_verified_rollback(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )

    def fail_postimage(*args: object, **kwargs: object) -> None:
        raise mutation_module._MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "Injected postimage mismatch.",
            path="created.txt",
        )

    monkeypatch.setattr(mutation_module, "_verify_postimages", fail_postimage)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert not (workspace.root / "created.txt").exists()
    assert result.file_evidence[0].rollback_verified is True
    assert WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH in _issue_codes(result)


def test_create_close_failure_preserves_record_and_rolls_back(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    real_close = mutation_module.os.close
    close_calls = 0

    def close_once_after_closing(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        if close_calls == 1:
            raise OSError("injected CREATE close failure")

    monkeypatch.setattr(
        mutation_module.os, "close", close_once_after_closing
    )

    result = apply_workspace_change_set(workspace, change_set, validation)
    evidence = result.file_evidence[0]

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert result.status is not WorkspaceMutationStatus.REJECTED
    assert not (workspace.root / "created.txt").exists()
    assert evidence.write_performed is True
    assert evidence.rollback_attempted is True
    assert evidence.rollback_verified is True


def test_create_primary_and_close_failures_preserve_primary_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    real_close = mutation_module.os.close
    close_calls = 0

    def fail_partial_write(descriptor: int, contents: bytes) -> None:
        os.write(descriptor, b"partial")
        raise OSError("injected CREATE write failure")

    def close_once_after_closing(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        real_close(descriptor)
        if close_calls == 1:
            raise OSError("injected CREATE close failure")

    monkeypatch.setattr(mutation_module, "_write_all", fail_partial_write)
    monkeypatch.setattr(
        mutation_module.os, "close", close_once_after_closing
    )

    result = apply_workspace_change_set(workspace, change_set, validation)
    evidence = result.file_evidence[0]

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert result.status is not WorkspaceMutationStatus.REJECTED
    assert not (workspace.root / "created.txt").exists()
    assert evidence.write_performed is True
    assert evidence.rollback_attempted is True
    assert evidence.rollback_verified is True
    assert WorkspaceMutationIssueCode.CREATE_FAILURE in _issue_codes(result)


def test_modify_post_replacement_oserror_preserves_record_and_rolls_back(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "service.py"
    target.write_text("before\n", encoding="utf-8")
    _, change_set, validation = _change_set(
        workspace, _artifact("service.py", "after\n")
    )
    real_inspect = mutation_module._inspect_workspace_target
    inspection_calls = 0

    def fail_post_replacement_inspection(
        current_workspace: IsolatedWorkspace,
        path: str,
    ) -> object:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 3:
            raise OSError("injected post-replacement inspection failure")
        return real_inspect(current_workspace, path)

    monkeypatch.setattr(
        mutation_module,
        "_inspect_workspace_target",
        fail_post_replacement_inspection,
    )

    result = apply_workspace_change_set(workspace, change_set, validation)
    evidence = result.file_evidence[0]

    assert result.status is WorkspaceMutationStatus.ROLLED_BACK
    assert result.status is not WorkspaceMutationStatus.REJECTED
    assert target.read_text() == "before\n"
    assert evidence.write_performed is True
    assert evidence.rollback_attempted is True
    assert evidence.rollback_verified is True


def test_created_parent_identity_failure_is_not_clean_rejection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("nested/file.txt", "created\n")
    )

    def fail_created_directory_identity(path: Path) -> os.stat_result:
        raise OSError("injected created-directory metadata failure")

    monkeypatch.setattr(
        runtime_module,
        "_lstat_created_directory",
        fail_created_directory_identity,
    )

    result = apply_workspace_change_set(workspace, change_set, validation)
    evidence = result.file_evidence[0]

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert result.status is not WorkspaceMutationStatus.REJECTED
    assert (workspace.root / "nested").is_dir()
    assert not (workspace.root / "nested" / "file.txt").exists()
    assert evidence.write_performed is True
    assert evidence.created_parent_paths == ("nested",)
    assert evidence.rollback_attempted is True
    assert evidence.rollback_verified is False
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)


def test_create_rollback_refuses_in_place_external_content_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    target = workspace.root / "created.txt"

    def externally_change_then_fail(*args: object, **kwargs: object) -> None:
        original_inode = target.stat().st_ino
        with target.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"external\n")
            stream.truncate()
        assert target.stat().st_ino == original_inode
        raise mutation_module._MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "Injected postimage mismatch after external content change.",
            path="created.txt",
        )

    monkeypatch.setattr(
        mutation_module, "_verify_postimages", externally_change_then_fail
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert target.read_bytes() == b"external\n"
    assert result.file_evidence[0].rollback_verified is False
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)


def test_create_rollback_refuses_intervening_mode_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    _, change_set, validation = _change_set(
        workspace, _artifact("created.txt", "created\n")
    )
    target = workspace.root / "created.txt"

    def externally_chmod_then_fail(*args: object, **kwargs: object) -> None:
        original_inode = target.stat().st_ino
        target.chmod(0o640)
        assert target.stat().st_ino == original_inode
        raise mutation_module._MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "Injected postimage mismatch after external mode change.",
            path="created.txt",
        )

    monkeypatch.setattr(
        mutation_module, "_verify_postimages", externally_chmod_then_fail
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert target.read_bytes() == b"created\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert result.file_evidence[0].rollback_verified is False
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)


def test_modify_rollback_refuses_intervening_mode_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    target = workspace.root / "tool.sh"
    target.write_bytes(b"#!/bin/sh\necho before\n")
    target.chmod(0o751)
    _, change_set, validation = _change_set(
        workspace,
        _artifact("tool.sh", "#!/bin/sh\necho after\n"),
    )

    def externally_chmod_then_fail(*args: object, **kwargs: object) -> None:
        target.chmod(0o700)
        raise mutation_module._MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "Injected postimage mismatch after external mode change.",
            path="tool.sh",
        )

    monkeypatch.setattr(
        mutation_module, "_verify_postimages", externally_chmod_then_fail
    )

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert result.status is WorkspaceMutationStatus.ROLLBACK_FAILED
    assert target.read_bytes() == b"#!/bin/sh\necho after\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert result.file_evidence[0].rollback_verified is False
    assert WorkspaceMutationIssueCode.ROLLBACK_FAILURE in _issue_codes(result)


def test_result_evidence_is_immutable_canonical_and_does_not_leak_temp_root(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    artifacts = (
        _artifact("z.txt", "z\n", index=2),
        _artifact("a.txt", "a\n", index=1),
    )
    _, change_set, validation = _change_set(workspace, *artifacts)

    result = apply_workspace_change_set(workspace, change_set, validation)

    assert tuple(item.path for item in result.file_evidence) == ("a.txt", "z.txt")
    assert result.mutation_id.startswith("WORKSPACE-MUTATION-")
    assert result.workspace_id == workspace.workspace_id
    assert result.change_set_id == change_set.change_set_id
    assert result.base_snapshot_id == change_set.base_snapshot_id
    assert result.task_id == change_set.task_id
    assert result.request_id == change_set.request_id
    assert result.attempt_id == change_set.attempt_id
    assert str(workspace.root) not in result.model_dump_json()
    with raises(ValidationError):
        result.status = WorkspaceMutationStatus.REJECTED  # type: ignore[misc]
