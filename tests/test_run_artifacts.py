"""Tests for application-owned live workflow-run evidence bundles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, cast

from pytest import mark, raises

from agentic_sdlc.run_artifacts import (
    SDLC_ARTIFACT_MANIFEST_FILENAME,
    SDLC_ARTIFACT_MANIFEST_SCHEMA_VERSION,
    SDLCArtifactManifest,
    LiveRunArtifactBundle,
    build_sdlc_artifact_manifest,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.state import WorkflowState


def _terminal_state(
    run_id: str,
    *,
    workflow_status: Literal["success", "safe_stopped"] = "success",
    exit_gate_passed: bool = True,
) -> WorkflowState:
    return cast(
        WorkflowState,
        {
            "run_id": run_id,
            "project_name": "URL Shortener",
            "project_delivery_policy": {"mode": "RUNNABLE_PROJECT"},
            "workflow_status": workflow_status,
            "exit_gate_passed": exit_gate_passed,
        },
    )


def test_live_run_bundle_uses_existing_run_id_for_owned_paths(
    tmp_path: Path,
) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-fixed-run")

    assert bundle.run_id == "demo-fixed-run"
    assert bundle.run_root == tmp_path / "runs" / "demo-fixed-run"
    assert bundle.artifact_dir == bundle.run_root / "sdlc-artifacts"
    assert bundle.workflow_diagram_path == (
        bundle.artifact_dir / "workflow_diagram.png"
    )


@mark.parametrize(
    "run_id",
    ("", " ", ".", "..", "../escape", "a/b", "a\\b", "C:escape", "bad\0id"),
)
def test_live_run_bundle_rejects_unsafe_run_id_components(
    tmp_path: Path,
    run_id: str,
) -> None:
    with raises(ValueError, match="safe filesystem path component"):
        LiveRunArtifactBundle.under_repository(tmp_path, run_id)


def test_success_manifest_binds_sorted_actual_files_and_diagram(
    tmp_path: Path,
) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-success")
    bundle.artifact_dir.mkdir(parents=True)
    files = {
        "summary.md": b"# Complete\n",
        "requirements.json": b'{"requirements": []}\n',
        "workflow_diagram.png": b"\x89PNG\r\n\x1a\n",
    }
    for name, contents in files.items():
        (bundle.artifact_dir / name).write_bytes(contents)

    manifest_path = write_sdlc_artifact_manifest(
        _terminal_state(bundle.run_id),
        bundle,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == SDLC_ARTIFACT_MANIFEST_SCHEMA_VERSION
    assert manifest["run_id"] == bundle.run_id
    assert manifest["project_name"] == "URL Shortener"
    assert manifest["workflow_status"] == "success"
    assert manifest["project_delivery_policy"] == "RUNNABLE_PROJECT"
    assert manifest["exit_gate_passed"] is True
    assert [record["path"] for record in manifest["files"]] == sorted(files)
    assert SDLC_ARTIFACT_MANIFEST_FILENAME not in {
        record["path"] for record in manifest["files"]
    }
    for record in manifest["files"]:
        contents = files[record["path"]]
        assert record["sha256"] == hashlib.sha256(contents).hexdigest()
        assert record["size_bytes"] == len(contents)
    binding = {
        key: value for key, value in manifest.items() if key != "bundle_sha256"
    }
    expected_bundle_hash = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert manifest["bundle_sha256"] == expected_bundle_hash


def test_bundle_hash_is_deterministic_and_changes_with_bound_content(
    tmp_path: Path,
) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-hash")
    bundle.artifact_dir.mkdir(parents=True)
    requirements = bundle.artifact_dir / "requirements.json"
    requirements.write_bytes(b"first\n")
    state = _terminal_state(bundle.run_id)

    first = build_sdlc_artifact_manifest(state, bundle)
    second = build_sdlc_artifact_manifest(state, bundle)
    requirements.write_bytes(b"second\n")
    changed = build_sdlc_artifact_manifest(state, bundle)

    assert first == second
    assert first.bundle_sha256 != changed.bundle_sha256
    assert first.files[0].sha256 != changed.files[0].sha256


def test_safe_stop_manifest_lists_only_available_partial_evidence(
    tmp_path: Path,
) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-stopped")
    bundle.artifact_dir.mkdir(parents=True)
    (bundle.artifact_dir / "requirements.json").write_text(
        '{"requirements": []}\n', encoding="utf-8"
    )
    (bundle.artifact_dir / "summary.md").write_text(
        "# Safely stopped\n", encoding="utf-8"
    )

    manifest_path = write_sdlc_artifact_manifest(
        _terminal_state(
            bundle.run_id,
            workflow_status="safe_stopped",
            exit_gate_passed=False,
        ),
        bundle,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["workflow_status"] == "safe_stopped"
    assert manifest["exit_gate_passed"] is False
    assert [record["path"] for record in manifest["files"]] == [
        "requirements.json",
        "summary.md",
    ]
    assert "workflow_diagram.png" not in {
        record["path"] for record in manifest["files"]
    }
    assert "task_graph.json" not in {
        record["path"] for record in manifest["files"]
    }


def test_manifest_rejects_a_state_owned_by_another_run(tmp_path: Path) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-owner")
    bundle.artifact_dir.mkdir(parents=True)
    (bundle.artifact_dir / "summary.md").write_text("# Summary\n")

    with raises(ValueError, match="does not own"):
        build_sdlc_artifact_manifest(_terminal_state("demo-other"), bundle)


@mark.parametrize(
    "unsafe_path",
    ("manifest.json", "../escape", "nested/file.json", "nested\\file.json"),
)
def test_manifest_model_rejects_reserved_or_escaping_file_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "demo-path-policy")
    bundle.artifact_dir.mkdir(parents=True)
    (bundle.artifact_dir / "summary.md").write_text("# Summary\n")
    manifest = build_sdlc_artifact_manifest(
        _terminal_state(bundle.run_id),
        bundle,
    ).model_dump(mode="json")
    manifest["files"][0]["path"] = unsafe_path

    with raises(ValueError):
        SDLCArtifactManifest.model_validate_json(json.dumps(manifest))
