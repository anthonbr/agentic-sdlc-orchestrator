"""Application-owned paths and manifest for one live workflow run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agentic_sdlc.project_delivery import (
    ProjectDeliveryMode,
    project_delivery_policy_from_value,
)
from agentic_sdlc.state import WorkflowState


RUNS_DIRECTORY_NAME = "runs"
SDLC_ARTIFACT_DIRECTORY_NAME = "sdlc-artifacts"
WORKFLOW_DIAGRAM_FILENAME = "workflow_diagram.png"
SDLC_ARTIFACT_MANIFEST_FILENAME = "manifest.json"
SDLC_ARTIFACT_MANIFEST_SCHEMA_VERSION = "sdlc-artifact-manifest-v1"


@dataclass(frozen=True, slots=True)
class LiveRunArtifactBundle:
    """Filesystem locations owned by the orchestrator for one existing run ID."""

    run_id: str
    run_root: Path
    artifact_dir: Path

    @classmethod
    def under_repository(
        cls,
        repository_root: Path,
        run_id: str,
    ) -> LiveRunArtifactBundle:
        """Derive one run bundle without creating another run identity."""

        _validate_run_id_component(run_id)
        run_root = repository_root.resolve() / RUNS_DIRECTORY_NAME / run_id
        return cls(
            run_id=run_id,
            run_root=run_root,
            artifact_dir=run_root / SDLC_ARTIFACT_DIRECTORY_NAME,
        )

    @property
    def workflow_diagram_path(self) -> Path:
        """Return the diagram location inside this run's evidence bundle."""

        return self.artifact_dir / WORKFLOW_DIAGRAM_FILENAME


class SDLCArtifactFileRecord(BaseModel):
    """Content identity for one bundle-relative live SDLC evidence file."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: str
    size_bytes: int


class SDLCArtifactManifest(BaseModel):
    """Deterministic ownership and content index for a terminal live run bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["sdlc-artifact-manifest-v1"]
    run_id: str
    project_name: str | None
    workflow_status: Literal["success", "safe_stopped"]
    project_delivery_policy: ProjectDeliveryMode | None
    exit_gate_passed: bool | None
    bundle_sha256: str
    files: tuple[SDLCArtifactFileRecord, ...]


def build_sdlc_artifact_manifest(
    state: WorkflowState,
    bundle: LiveRunArtifactBundle,
) -> SDLCArtifactManifest:
    """Build a deterministic manifest from actual files in one terminal bundle."""

    run_id = state.get("run_id", "")
    if not run_id or run_id != bundle.run_id:
        raise ValueError("The terminal workflow state does not own this run bundle.")
    status = state.get("workflow_status")
    if status not in {"success", "safe_stopped"}:
        raise ValueError("A manifest requires a successful or safely stopped workflow.")

    file_records = _bundle_file_records(bundle.artifact_dir)
    project_name = state.get("project_name", "").strip() or None
    policy_value = state.get("project_delivery_policy")
    delivery_mode = (
        project_delivery_policy_from_value(policy_value).mode
        if policy_value is not None
        else None
    )
    exit_gate_passed = state.get("exit_gate_passed")
    binding = {
        "schema_version": SDLC_ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "project_name": project_name,
        "workflow_status": status,
        "project_delivery_policy": (
            delivery_mode.value if delivery_mode is not None else None
        ),
        "exit_gate_passed": exit_gate_passed,
        "files": [record.model_dump(mode="json") for record in file_records],
    }
    bundle_sha256 = hashlib.sha256(_canonical_json_bytes(binding)).hexdigest()
    return SDLCArtifactManifest(
        schema_version=SDLC_ARTIFACT_MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        project_name=project_name,
        workflow_status=status,
        project_delivery_policy=delivery_mode,
        exit_gate_passed=exit_gate_passed,
        bundle_sha256=bundle_sha256,
        files=file_records,
    )


def write_sdlc_artifact_manifest(
    state: WorkflowState,
    bundle: LiveRunArtifactBundle,
) -> Path:
    """Write the terminal manifest after all available live evidence exists."""

    manifest = build_sdlc_artifact_manifest(state, bundle)
    path = bundle.artifact_dir / SDLC_ARTIFACT_MANIFEST_FILENAME
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _bundle_file_records(bundle_dir: Path) -> tuple[SDLCArtifactFileRecord, ...]:
    if not bundle_dir.is_dir():
        raise ValueError("The live SDLC artifact directory does not exist.")

    records: list[SDLCArtifactFileRecord] = []
    for path in sorted(bundle_dir.iterdir(), key=lambda item: item.name):
        if path.name == SDLC_ARTIFACT_MANIFEST_FILENAME:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"Unsupported entry in live SDLC artifact bundle: {path.name}"
            )
        contents = path.read_bytes()
        records.append(
            SDLCArtifactFileRecord(
                path=path.relative_to(bundle_dir).as_posix(),
                sha256=hashlib.sha256(contents).hexdigest(),
                size_bytes=len(contents),
            )
        )
    return tuple(records)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_run_id_component(run_id: str) -> None:
    if (
        not run_id.strip()
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
        or Path(run_id).is_absolute()
    ):
        raise ValueError("The run ID must be one safe filesystem path component.")
