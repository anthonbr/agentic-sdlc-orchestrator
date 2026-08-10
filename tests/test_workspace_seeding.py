"""Tests for trusted pre-session brownfield baseline population."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import ValidationError
from pytest import raises

from agentic_sdlc.workspace_runtime import create_isolated_workspace
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedingError,
    WorkspaceSeedingIssueCode,
    seed_isolated_workspace_from_approved_files,
)


def _source(root: Path) -> tuple[Path, tuple[str, ...]]:
    source = root / "approved-source"
    (source / "src").mkdir(parents=True)
    (source / "README.md").write_text("approved readme\n")
    (source / "src/service.py").write_text("VALUE = 1\n")
    (source / "unapproved.txt").write_text("not in explicit manifest\n")
    return source, ("src/service.py", "README.md")


def test_seed_populates_only_explicit_files_and_verifies_source_hashes(
    tmp_path: Path,
) -> None:
    source, paths = _source(tmp_path)
    source_before = {
        path: (source / path).read_bytes() for path in (*paths, "unapproved.txt")
    }
    workspace = create_isolated_workspace(
        "WORKSPACE-SEED-TEST", parent_directory=tmp_path
    )

    result, snapshot = seed_isolated_workspace_from_approved_files(
        workspace,
        source_root=source,
        source_root_label="approved/source",
        relative_paths=paths,
    )

    assert result.verified is True
    assert result.workspace_id == workspace.workspace_id
    assert result.baseline_snapshot_id == snapshot.snapshot_id
    assert tuple(item.path for item in result.files) == (
        "README.md",
        "src/service.py",
    )
    assert tuple(item.path for item in snapshot.files) == (
        "README.md",
        "src/service.py",
    )
    assert not (workspace.root / "unapproved.txt").exists()
    for item in result.files:
        expected = hashlib.sha256(source_before[item.path]).hexdigest()
        assert item.source_content_hash == expected
        assert item.seeded_content_hash == expected
        assert (workspace.root / item.path).read_bytes() == source_before[item.path]
    assert {
        path: (source / path).read_bytes() for path in (*paths, "unapproved.txt")
    } == source_before


def test_seed_identity_and_order_are_deterministic(tmp_path: Path) -> None:
    first_source, paths = _source(tmp_path / "first")
    second_source, _ = _source(tmp_path / "second")
    first_workspace = create_isolated_workspace(
        "WORKSPACE-SAME", parent_directory=tmp_path
    )
    second_workspace = create_isolated_workspace(
        "WORKSPACE-SAME", parent_directory=tmp_path
    )

    first, first_snapshot = seed_isolated_workspace_from_approved_files(
        first_workspace,
        source_root=first_source,
        source_root_label="approved/source",
        relative_paths=paths,
    )
    second, second_snapshot = seed_isolated_workspace_from_approved_files(
        second_workspace,
        source_root=second_source,
        source_root_label="approved/source",
        relative_paths=tuple(reversed(paths)),
    )

    assert first == second
    assert first_snapshot == second_snapshot
    with raises(ValidationError):
        first.files[0].path = "other.py"  # type: ignore[misc]


def test_seed_rejects_duplicate_and_escaping_paths(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    workspace = create_isolated_workspace(
        "WORKSPACE-PATHS", parent_directory=tmp_path
    )

    with raises(WorkspaceSeedingError) as duplicate:
        seed_isolated_workspace_from_approved_files(
            workspace,
            source_root=source,
            source_root_label="approved/source",
            relative_paths=("README.md", "README.md"),
        )
    assert duplicate.value.code is WorkspaceSeedingIssueCode.DUPLICATE_PATH

    with raises(WorkspaceSeedingError) as traversal:
        seed_isolated_workspace_from_approved_files(
            workspace,
            source_root=source,
            source_root_label="approved/source",
            relative_paths=("../outside.txt",),
        )
    assert traversal.value.code is WorkspaceSeedingIssueCode.SOURCE_PATH
    assert not (tmp_path / "outside.txt").exists()


def test_seed_rejects_source_symlink_without_following_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (source / "linked.txt").symlink_to(outside)
    workspace = create_isolated_workspace(
        "WORKSPACE-SYMLINK", parent_directory=tmp_path
    )

    with raises(WorkspaceSeedingError) as raised:
        seed_isolated_workspace_from_approved_files(
            workspace,
            source_root=source,
            source_root_label="approved/source",
            relative_paths=("linked.txt",),
        )

    assert raised.value.code is WorkspaceSeedingIssueCode.SOURCE_SYMLINK
    assert tuple(workspace.root.iterdir()) == ()


def test_seed_rejects_unsupported_source_type(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "directory.py").mkdir(parents=True)
    workspace = create_isolated_workspace(
        "WORKSPACE-TYPE", parent_directory=tmp_path
    )

    with raises(WorkspaceSeedingError) as raised:
        seed_isolated_workspace_from_approved_files(
            workspace,
            source_root=source,
            source_root_label="approved/source",
            relative_paths=("directory.py",),
        )

    assert raised.value.code is WorkspaceSeedingIssueCode.UNSUPPORTED_SOURCE_TYPE
    assert tuple(workspace.root.iterdir()) == ()


def test_seed_rejects_nonempty_workspace(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    workspace = create_isolated_workspace(
        "WORKSPACE-NONEMPTY", parent_directory=tmp_path
    )
    (workspace.root / "existing.txt").write_text("existing\n")

    with raises(WorkspaceSeedingError) as raised:
        seed_isolated_workspace_from_approved_files(
            workspace,
            source_root=source,
            source_root_label="approved/source",
            relative_paths=("README.md",),
        )

    assert raised.value.code is WorkspaceSeedingIssueCode.WORKSPACE_NOT_EMPTY
    assert (workspace.root / "existing.txt").read_text() == "existing\n"


def test_seed_supports_binary_regular_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    contents = bytes(range(256))
    (source / "fixture.bin").write_bytes(contents)
    workspace = create_isolated_workspace(
        "WORKSPACE-BINARY", parent_directory=tmp_path
    )

    result, snapshot = seed_isolated_workspace_from_approved_files(
        workspace,
        source_root=source,
        source_root_label="approved/source",
        relative_paths=("fixture.bin",),
    )

    assert result.files[0].seeded_content_hash == hashlib.sha256(contents).hexdigest()
    assert snapshot.files[0].content_hash == result.files[0].seeded_content_hash
    assert (workspace.root / "fixture.bin").read_bytes() == contents
