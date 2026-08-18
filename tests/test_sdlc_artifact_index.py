"""Focused tests for the manifest-driven SDLC artifact presentation index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sdlc.run_artifacts import (
    LiveRunArtifactBundle,
    write_sdlc_artifact_manifest,
)
from agentic_sdlc.sdlc_artifact_index import (
    SDLCArtifactIndexError,
    SDLCArtifactIndexRow,
    load_sdlc_artifact_index,
)
from agentic_sdlc.sdlc_document_models import SDLC_PDF_FILENAMES
from tests.test_run_artifacts import _terminal_state


def _finalized_bundle(
    tmp_path: Path,
    *,
    run_id: str = "artifact-index",
    files: dict[str, bytes] | None = None,
) -> tuple[LiveRunArtifactBundle, Path]:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, run_id)
    bundle.artifact_dir.mkdir(parents=True)
    for name, contents in (files or {"summary.md": b"# Summary\n"}).items():
        (bundle.artifact_dir / name).write_bytes(contents)
    manifest_path = write_sdlc_artifact_manifest(
        _terminal_state(run_id),
        bundle,
    )
    return bundle, manifest_path


def _load(
    bundle: LiveRunArtifactBundle,
    manifest_path: Path,
) -> tuple[SDLCArtifactIndexRow, ...]:
    return load_sdlc_artifact_index(
        bundle=bundle,
        manifest_path=manifest_path,
        workflow_status="success",
    )


def test_manifest_artifacts_are_presented_in_sdlc_lifecycle_order(
    tmp_path: Path,
) -> None:
    files = {
        "summary.md": b"summary",
        "requirement_traceability.json": b"{}\n",
        "task_graph.json": b"{}\n",
        "human_governance_history.md": b"history",
        "engineering_artifacts.json": b"[]\n",
        "requirements.json": b"{}\n",
        "task_execution.json": b"{}\n",
        "requirement_analysis.md": b"analysis",
        "requirement_traceability.md": b"traceability",
        "workflow_diagram.png": b"\x89PNG\r\n\x1a\n",
        "workspace_execution.json": b"{}\n",
        "task_graph.md": b"plan",
        "approved_requirement_spec.json": b"{}\n",
    }
    bundle, manifest_path = _finalized_bundle(tmp_path, files=files)

    rows = _load(bundle, manifest_path)

    assert [row.artifact for row in rows] == [
        "requirements.json",
        "requirement_analysis.md",
        "approved_requirement_spec.json",
        "task_graph.md",
        "task_graph.json",
        "workflow_diagram.png",
        "task_execution.json",
        "workspace_execution.json",
        "engineering_artifacts.json",
        "requirement_traceability.md",
        "requirement_traceability.json",
        "human_governance_history.md",
        "summary.md",
        "manifest.json",
    ]
    assert rows[3].stage == rows[4].stage == "Planning / TaskGraph"
    assert rows[-2].stage == "Final Engineering Summary"
    assert rows[-1].stage == "Evidence Integrity"


def test_index_uses_manifest_records_not_directory_enumeration(tmp_path: Path) -> None:
    bundle, manifest_path = _finalized_bundle(
        tmp_path,
        files={
            "requirements.json": b'{"requirement": "retained"}\n',
            "summary.md": b"# Retained\n",
        },
    )
    (bundle.artifact_dir / "not-manifest-bound.env").write_text(
        "SECRET=not-for-presentation\n",
        encoding="utf-8",
    )
    bundle.run_events_path.write_text('{"event":"outside"}\n', encoding="utf-8")

    rows = _load(bundle, manifest_path)
    artifacts = {row.artifact for row in rows}

    assert artifacts == {"requirements.json", "summary.md", "manifest.json"}
    assert "not-manifest-bound.env" not in artifacts
    assert "run-events.jsonl" not in artifacts


def test_unknown_manifest_artifact_uses_deterministic_fallback(tmp_path: Path) -> None:
    bundle, manifest_path = _finalized_bundle(
        tmp_path,
        files={
            "summary.md": b"summary",
            "future_evidence.bin": b"future evidence",
        },
    )

    rows = _load(bundle, manifest_path)
    future = next(row for row in rows if row.artifact == "future_evidence.bin")

    assert future.stage == "Other Retained Evidence"
    assert future.description == "Additional manifest-bound retained evidence."
    assert future.mime_type == "application/octet-stream"
    assert [row.artifact for row in rows] == [
        "summary.md",
        "future_evidence.bin",
        "manifest.json",
    ]


def test_index_exposes_exact_retained_bytes_filename_and_mime_type(
    tmp_path: Path,
) -> None:
    expected = b"# Exact retained bytes\n"
    bundle, manifest_path = _finalized_bundle(
        tmp_path,
        files={"requirement_traceability.md": expected},
    )

    row = _load(bundle, manifest_path)[0]

    assert row.artifact == "requirement_traceability.md"
    assert row.mime_type == "text/markdown"
    assert row.contents == expected


def test_index_gives_pdfs_human_friendly_names_and_pdf_mime_type(
    tmp_path: Path,
) -> None:
    bundle, manifest_path = _finalized_bundle(
        tmp_path,
        files={name: b"%PDF-1.4\n" for name in SDLC_PDF_FILENAMES},
    )

    rows = _load(bundle, manifest_path)

    assert [row.artifact for row in rows[:-1]] == list(SDLC_PDF_FILENAMES)
    assert [row.display_name for row in rows[:-1]] == [
        "Requirements Specification",
        "Functional Specification",
        "Design Specification",
        "Test Plan and Validation Report",
    ]
    assert all(row.stage == "Governed SDLC Documents" for row in rows[:-1])
    assert all(row.mime_type == "application/pdf" for row in rows[:-1])


def test_index_rejects_traversal_in_manifest_record(tmp_path: Path) -> None:
    bundle, manifest_path = _finalized_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside-secret"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SDLCArtifactIndexError, match="could not be read safely"):
        _load(bundle, manifest_path)


def test_index_rejects_symlink_replacing_manifested_artifact(tmp_path: Path) -> None:
    bundle, manifest_path = _finalized_bundle(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    summary = bundle.artifact_dir / "summary.md"
    summary.unlink()
    summary.symlink_to(outside)

    with pytest.raises(SDLCArtifactIndexError, match="could not be read safely"):
        _load(bundle, manifest_path)


def test_index_rejects_manifest_path_outside_owned_bundle(tmp_path: Path) -> None:
    bundle, _ = _finalized_bundle(tmp_path)

    with pytest.raises(SDLCArtifactIndexError, match="does not belong"):
        _load(bundle, tmp_path / "manifest.json")
