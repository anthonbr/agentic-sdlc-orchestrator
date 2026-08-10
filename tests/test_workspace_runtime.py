"""Tests for factory-created isolated workspaces and real snapshots."""

from __future__ import annotations

import hashlib
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

from pytest import MonkeyPatch, raises

import agentic_sdlc.workspace_runtime as runtime_module

from agentic_sdlc.workspace_contracts import workspace_file_content_hash
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    WorkspaceRuntimeIssueCode,
    create_isolated_workspace,
    snapshot_isolated_workspace,
)


def test_factory_creates_unique_empty_workspaces_below_requested_parent(
    tmp_path: Path,
) -> None:
    first = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    second = create_isolated_workspace("WORKSPACE-002", parent_directory=tmp_path)

    assert first.workspace_id == "WORKSPACE-001"
    assert first.root.is_absolute()
    assert first.root.parent == tmp_path.resolve()
    assert first.root != second.root
    assert tuple(first.root.iterdir()) == ()
    assert tuple(second.root.iterdir()) == ()


def test_capability_cannot_wrap_an_arbitrary_existing_path(tmp_path: Path) -> None:
    with raises(WorkspaceRuntimeError) as caught:
        IsolatedWorkspace("WORKSPACE-001", tmp_path)

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY


def test_capability_is_immutable(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )

    with raises(FrozenInstanceError):
        workspace.workspace_id = "OTHER"  # type: ignore[misc]


def test_empty_workspace_snapshot_is_deterministic(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )

    first = snapshot_isolated_workspace(workspace)
    second = snapshot_isolated_workspace(workspace)

    assert first == second
    assert first.workspace_id == workspace.workspace_id
    assert first.files == ()


def test_snapshot_hashes_text_binary_and_nested_regular_files(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    (workspace.root / "src").mkdir()
    text = "snowman: ☃\n"
    binary = b"\x00\xff\x10binary\n"
    (workspace.root / "src" / "service.py").write_text(text, encoding="utf-8")
    (workspace.root / "asset.bin").write_bytes(binary)

    snapshot = snapshot_isolated_workspace(workspace)

    assert tuple(item.path for item in snapshot.files) == (
        "asset.bin",
        "src/service.py",
    )
    assert snapshot.file_state("src/service.py").content_hash == (
        workspace_file_content_hash(text)
    )
    assert snapshot.file_state("asset.bin").content_hash == hashlib.sha256(
        binary
    ).hexdigest()


def test_snapshot_identity_ignores_filesystem_enumeration_order(
    tmp_path: Path,
) -> None:
    first = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    second = create_isolated_workspace("WORKSPACE-001", parent_directory=tmp_path)
    for workspace, names in (
        (first, ("z.txt", "a.txt")),
        (second, ("a.txt", "z.txt")),
    ):
        for name in names:
            (workspace.root / name).write_text(name, encoding="utf-8")

    assert snapshot_isolated_workspace(first).snapshot_id == (
        snapshot_isolated_workspace(second).snapshot_id
    )


def test_snapshot_rejects_symlink_file(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace.root / "link.txt").symlink_to(outside)

    with raises(WorkspaceRuntimeError) as caught:
        snapshot_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.SYMLINK_DETECTED
    assert caught.value.path == "link.txt"


def test_snapshot_rejects_symlink_directory(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "linked").symlink_to(outside, target_is_directory=True)

    with raises(WorkspaceRuntimeError) as caught:
        snapshot_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.SYMLINK_DETECTED
    assert caught.value.path == "linked"


def test_snapshot_rejects_unsupported_special_file(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    os.mkfifo(workspace.root / "events.fifo")

    with raises(WorkspaceRuntimeError) as caught:
        snapshot_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE
    assert caught.value.path == "events.fifo"


def test_snapshot_fails_closed_when_workspace_root_disappears(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    workspace.root.rmdir()

    with raises(WorkspaceRuntimeError) as caught:
        snapshot_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE


def test_regular_file_close_failure_is_structured_runtime_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("contents\n", encoding="utf-8")
    real_close = runtime_module.os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("injected descriptor close failure")

    monkeypatch.setattr(runtime_module.os, "close", close_then_fail)

    with raises(WorkspaceRuntimeError) as caught:
        runtime_module._read_regular_file(target, "file.txt")

    assert caught.value.code is WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE
    assert caught.value.path == "file.txt"


def test_regular_file_close_failure_does_not_replace_prior_structured_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    real_close = runtime_module.os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("injected descriptor close failure")

    monkeypatch.setattr(runtime_module.os, "close", close_then_fail)

    with raises(WorkspaceRuntimeError) as caught:
        runtime_module._read_regular_file(directory, "directory")

    assert caught.value.code is WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE
    assert caught.value.path == "directory"
