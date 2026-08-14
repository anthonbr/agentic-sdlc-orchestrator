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
    discard_isolated_workspace,
    snapshot_directory_tree,
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


def test_factory_owned_workspace_cleanup_is_descriptor_relative(tmp_path: Path) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-DISCARD", parent_directory=tmp_path
    )
    root = workspace.root
    (root / "nested/deeper").mkdir(parents=True)
    (root / "nested/deeper/value.txt").write_text("owned\n", encoding="utf-8")

    discard_isolated_workspace(workspace)

    assert not root.exists()


def test_workspace_cleanup_rejects_replacement_directory_and_preserves_it(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-REPLACED", parent_directory=tmp_path
    )
    root = workspace.root
    original = tmp_path / "preserved-original"
    root.rename(original)
    root.mkdir()
    marker = root / "replacement.txt"
    marker.write_text("do not delete\n", encoding="utf-8")

    with raises(WorkspaceRuntimeError) as caught:
        discard_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY
    assert marker.read_text(encoding="utf-8") == "do not delete\n"
    assert original.is_dir()


def test_workspace_cleanup_rejects_symlink_root_and_preserves_target(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-SYMLINK", parent_directory=tmp_path
    )
    root = workspace.root
    original = tmp_path / "preserved-symlink-original"
    target = tmp_path / "symlink-target"
    root.rename(original)
    target.mkdir()
    marker = target / "target.txt"
    marker.write_text("do not follow\n", encoding="utf-8")
    root.symlink_to(target, target_is_directory=True)

    with raises(WorkspaceRuntimeError) as caught:
        discard_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY
    assert root.is_symlink()
    assert marker.read_text(encoding="utf-8") == "do not follow\n"


def test_workspace_prefix_lookalike_does_not_grant_cleanup_authority(
    tmp_path: Path,
) -> None:
    lookalike = tmp_path / "agentic-sdlc-workspace-lookalike"
    lookalike.mkdir()
    marker = lookalike / "unowned.txt"
    marker.write_text("unowned\n", encoding="utf-8")

    with raises(WorkspaceRuntimeError) as caught:
        discard_isolated_workspace(lookalike)  # type: ignore[arg-type]

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY
    assert marker.read_text(encoding="utf-8") == "unowned\n"


def test_workspace_cleanup_rejects_forged_capability(tmp_path: Path) -> None:
    lookalike = tmp_path / "agentic-sdlc-workspace-forged"
    lookalike.mkdir()
    marker = lookalike / "unowned.txt"
    marker.write_text("unowned\n", encoding="utf-8")
    metadata = lookalike.stat()
    forged = object.__new__(IsolatedWorkspace)
    object.__setattr__(forged, "workspace_id", "FORGED")
    object.__setattr__(forged, "root", lookalike)
    object.__setattr__(forged, "_root_device", metadata.st_dev)
    object.__setattr__(forged, "_root_inode", metadata.st_ino)
    object.__setattr__(forged, "_authority", object())

    with raises(WorkspaceRuntimeError) as caught:
        discard_isolated_workspace(forged)

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY
    assert marker.read_text(encoding="utf-8") == "unowned\n"


def test_workspace_cleanup_rejects_root_identity_change_during_removal(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-RACE", parent_directory=tmp_path
    )
    root = workspace.root
    (root / "owned.txt").write_text("owned\n", encoding="utf-8")
    original = tmp_path / "preserved-race-original"
    real_remove = runtime_module._remove_empty_owned_directory_at

    def replace_before_remove(
        parent_descriptor: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        relative_path: str,
    ) -> None:
        if relative_path == "<workspace-root>":
            root.rename(original)
            root.mkdir()
            (root / "replacement.txt").write_text(
                "do not delete\n", encoding="utf-8"
            )
        real_remove(
            parent_descriptor,
            name,
            expected_identity=expected_identity,
            relative_path=relative_path,
        )

    monkeypatch.setattr(
        runtime_module,
        "_remove_empty_owned_directory_at",
        replace_before_remove,
    )

    with raises(WorkspaceRuntimeError) as caught:
        discard_isolated_workspace(workspace)

    assert caught.value.code is WorkspaceRuntimeIssueCode.INVALID_CAPABILITY
    assert (root / "replacement.txt").read_text(encoding="utf-8") == (
        "do not delete\n"
    )
    assert original.is_dir()


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


def test_directory_tree_snapshot_reuses_workspace_snapshot_identity(
    tmp_path: Path,
) -> None:
    workspace = create_isolated_workspace(
        "WORKSPACE-001", parent_directory=tmp_path
    )
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "service.py").write_text(
        "value = 1\n",
        encoding="utf-8",
    )

    assert snapshot_directory_tree(
        workspace.root,
        workspace_id=workspace.workspace_id,
    ) == snapshot_isolated_workspace(workspace)


def test_directory_tree_snapshot_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with raises(WorkspaceRuntimeError) as caught:
        snapshot_directory_tree(linked, workspace_id="WORKSPACE-001")

    assert caught.value.code is WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE


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
