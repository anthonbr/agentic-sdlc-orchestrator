"""Read-only presentation index for finalized SDLC evidence bundles."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from agentic_sdlc.run_artifacts import (
    RUN_EVENTS_FILENAME,
    RUNS_DIRECTORY_NAME,
    SDLC_ARTIFACT_DIRECTORY_NAME,
    SDLC_ARTIFACT_MANIFEST_FILENAME,
    LiveRunArtifactBundle,
    SDLCArtifactManifest,
)
from agentic_sdlc.sdlc_document_models import (
    DESIGN_SPECIFICATION_PDF,
    FUNCTIONAL_SPECIFICATION_PDF,
    REQUIREMENTS_SPECIFICATION_PDF,
    TEST_PLAN_VALIDATION_REPORT_PDF,
)


class SDLCArtifactIndexError(ValueError):
    """Raised when finalized evidence cannot be exposed safely."""


@dataclass(frozen=True, slots=True)
class SDLCArtifactIndexRow:
    """One presentation-only retained-artifact download row."""

    lifecycle_rank: int
    lifecycle_subrank: int
    stage: str
    artifact: str
    display_name: str
    description: str
    mime_type: str
    contents: bytes


@dataclass(frozen=True, slots=True)
class _ArtifactPresentation:
    lifecycle_rank: int
    lifecycle_subrank: int
    stage: str
    description: str
    display_name: str | None = None


_PRESENTATION_BY_ARTIFACT = {
    "requirements.json": _ArtifactPresentation(
        10,
        0,
        "Requirement Input",
        "Original and normalized requirement submission.",
    ),
    "requirement_analysis.md": _ArtifactPresentation(
        20,
        0,
        "Requirement Analysis",
        (
            "Requirement interpretation, ambiguity, risks, acceptance criteria, "
            "and review history."
        ),
    ),
    "approved_requirement_spec.json": _ArtifactPresentation(
        30,
        0,
        "Approved Specification",
        "Human-approved authoritative requirement specification.",
    ),
    "task_graph.md": _ArtifactPresentation(
        40,
        0,
        "Planning / TaskGraph",
        "Human-readable implementation plan and task dependencies.",
    ),
    "task_graph.json": _ArtifactPresentation(
        40,
        1,
        "Planning / TaskGraph",
        "Structured approved TaskGraph.",
    ),
    "workflow_diagram.png": _ArtifactPresentation(
        50,
        0,
        "Orchestration",
        "Governed orchestration and control-flow diagram.",
    ),
    "task_execution.json": _ArtifactPresentation(
        60,
        0,
        "Task Execution",
        "Task execution attempts, outcomes, retries, and evidence.",
    ),
    "workspace_execution.json": _ArtifactPresentation(
        70,
        0,
        "Workspace / Validation",
        "Governed workspace mutation and validation evidence.",
    ),
    "engineering_artifacts.json": _ArtifactPresentation(
        80,
        0,
        "Engineering Output Inventory",
        "Generated and modified engineering artifact inventory.",
    ),
    "requirement_traceability.md": _ArtifactPresentation(
        90,
        0,
        "Requirement-to-Code Traceability",
        "Human-readable requirement-to-code traceability.",
    ),
    "requirement_traceability.json": _ArtifactPresentation(
        90,
        1,
        "Requirement-to-Code Traceability",
        "Structured requirement-to-code traceability projection.",
    ),
    REQUIREMENTS_SPECIFICATION_PDF: _ArtifactPresentation(
        95,
        0,
        "Governed SDLC Documents",
        "Human-readable projection of approved requirements and traceability.",
        "Requirements Specification",
    ),
    FUNCTIONAL_SPECIFICATION_PDF: _ArtifactPresentation(
        95,
        1,
        "Governed SDLC Documents",
        "Human-readable projection of approved functional behavior and mappings.",
        "Functional Specification",
    ),
    DESIGN_SPECIFICATION_PDF: _ArtifactPresentation(
        95,
        2,
        "Governed SDLC Documents",
        "Human-readable projection of approved design and engineering evidence.",
        "Design Specification",
    ),
    TEST_PLAN_VALIDATION_REPORT_PDF: _ArtifactPresentation(
        95,
        3,
        "Governed SDLC Documents",
        "Human-readable projection of actual governed validation evidence.",
        "Test Plan and Validation Report",
    ),
    "human_governance_history.md": _ArtifactPresentation(
        100,
        0,
        "Human Governance",
        "Human decisions, feedback, AI assistance, and governance consequences.",
    ),
    "summary.md": _ArtifactPresentation(
        110,
        0,
        "Final Engineering Summary",
        "Final engineering outcome, validation, risks, and limitations.",
    ),
    SDLC_ARTIFACT_MANIFEST_FILENAME: _ArtifactPresentation(
        120,
        0,
        "Evidence Integrity",
        "Integrity-bound inventory of retained evidence.",
    ),
}
_OTHER_RETAINED_EVIDENCE = _ArtifactPresentation(
    115,
    0,
    "Other Retained Evidence",
    "Additional manifest-bound retained evidence.",
)


def load_sdlc_artifact_index(
    *,
    bundle: LiveRunArtifactBundle,
    manifest_path: Path,
    workflow_status: str,
) -> tuple[SDLCArtifactIndexRow, ...]:
    """Load exact manifest-bound bytes in deterministic lifecycle order.

    The manifest is the inclusion authority for bundle files. Its own bytes are
    appended as the final integrity row because a manifest cannot list itself.
    No filesystem enumeration contributes an artifact row.
    """

    try:
        canonical_bundle = LiveRunArtifactBundle.under_repository(
            bundle.repository_root,
            bundle.run_id,
        )
    except (OSError, ValueError) as error:
        raise SDLCArtifactIndexError(
            f"The run artifact bundle is not canonical: {error}"
        ) from error
    if bundle != canonical_bundle:
        raise SDLCArtifactIndexError("The run artifact bundle path is not canonical.")
    expected_manifest_path = bundle.artifact_dir / SDLC_ARTIFACT_MANIFEST_FILENAME
    if manifest_path != expected_manifest_path:
        raise SDLCArtifactIndexError(
            "The terminal manifest path does not belong to the run artifact bundle."
        )

    try:
        with _open_artifact_directory(bundle) as artifact_descriptor:
            manifest_contents = _read_regular_file_at(
                artifact_descriptor,
                SDLC_ARTIFACT_MANIFEST_FILENAME,
            )
            manifest = SDLCArtifactManifest.model_validate_json(manifest_contents)
            if manifest.run_id != bundle.run_id:
                raise SDLCArtifactIndexError(
                    "The terminal manifest belongs to a different governed run."
                )
            if manifest.workflow_status != workflow_status:
                raise SDLCArtifactIndexError(
                    "The terminal manifest status differs from the governed run."
                )

            rows: list[SDLCArtifactIndexRow] = []
            for record in manifest.files:
                if record.path == RUN_EVENTS_FILENAME:
                    raise SDLCArtifactIndexError(
                        "The live run-event stream must not be manifest-bound."
                    )
                contents = _read_regular_file_at(artifact_descriptor, record.path)
                if (
                    len(contents) != record.size_bytes
                    or hashlib.sha256(contents).hexdigest() != record.sha256
                ):
                    raise SDLCArtifactIndexError(
                        "Retained artifact content differs from manifest.json: "
                        f"{record.path}."
                    )
                rows.append(_row_for(record.path, contents))
            rows.append(
                _row_for(SDLC_ARTIFACT_MANIFEST_FILENAME, manifest_contents)
            )
    except SDLCArtifactIndexError:
        raise
    except (OSError, ValidationError, ValueError) as error:
        raise SDLCArtifactIndexError(
            f"Finalized SDLC evidence could not be read safely: {error}"
        ) from error

    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.lifecycle_rank,
                row.lifecycle_subrank,
                row.artifact,
            ),
        )
    )


def _row_for(artifact: str, contents: bytes) -> SDLCArtifactIndexRow:
    presentation = _PRESENTATION_BY_ARTIFACT.get(
        artifact,
        _OTHER_RETAINED_EVIDENCE,
    )
    return SDLCArtifactIndexRow(
        lifecycle_rank=presentation.lifecycle_rank,
        lifecycle_subrank=presentation.lifecycle_subrank,
        stage=presentation.stage,
        artifact=artifact,
        display_name=presentation.display_name or artifact,
        description=presentation.description,
        mime_type=_mime_type(artifact),
        contents=contents,
    )


def _mime_type(artifact: str) -> str:
    suffix = Path(artifact).suffix.casefold()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    if suffix == ".png":
        return "image/png"
    if suffix == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


@contextmanager
def _open_artifact_directory(
    bundle: LiveRunArtifactBundle,
) -> Iterator[int]:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise SDLCArtifactIndexError(
            "Safe artifact access requires POSIX no-follow filesystem operations."
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC

    descriptors: list[int] = []
    try:
        descriptor = os.open(bundle.repository_root, directory_flags)
        descriptors.append(descriptor)
        _require_directory(descriptor, "repository root")
        for component in (
            RUNS_DIRECTORY_NAME,
            bundle.run_id,
            SDLC_ARTIFACT_DIRECTORY_NAME,
        ):
            descriptor = os.open(
                component,
                directory_flags,
                dir_fd=descriptor,
            )
            descriptors.append(descriptor)
            _require_directory(descriptor, component)
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_directory(descriptor: int, label: str) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise SDLCArtifactIndexError(f"Artifact path is not a directory: {label}.")


def _read_regular_file_at(parent_descriptor: int, name: str) -> bytes:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise SDLCArtifactIndexError("Manifest artifact path is unsafe.")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        initial = os.fstat(descriptor)
        identity = initial.st_dev, initial.st_ino
        if not stat.S_ISREG(initial.st_mode):
            raise SDLCArtifactIndexError(
                f"Manifest artifact is not a regular file: {name}."
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino) != identity
            or not stat.S_ISREG(final.st_mode)
        ):
            raise SDLCArtifactIndexError(
                f"Manifest artifact changed identity while being read: {name}."
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)
