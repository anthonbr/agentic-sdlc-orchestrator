"""Application-owned selection and lineage for published project baselines."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentic_sdlc.run_artifacts import (
    SDLC_ARTIFACT_DIRECTORY_NAME,
    SDLC_ARTIFACT_MANIFEST_FILENAME,
    SDLCArtifactManifest,
)
from agentic_sdlc.workspace_contracts import (
    WorkspaceFileState,
    WorkspaceSnapshot,
)
from agentic_sdlc.workspace_integration_contracts import (
    GovernedWorkspaceSession,
    WorkspaceIntegrityStatus,
)
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedResult,
    WorkspaceSeedingError,
    verify_approved_source_files,
)


BROWNFIELD_BASELINE_SCHEMA_VERSION = "brownfield-baseline-v1"


class BrownfieldBaselineProvenanceData(TypedDict):
    """Checkpoint-safe JSON representation of immutable baseline lineage."""

    schema_version: Literal["brownfield-baseline-v1"]
    baseline_id: str
    selected_project_name: str
    originating_run_id: str
    workflow_project_name: str | None
    publication_bundle_sha256: str
    source_workspace_id: str
    source_snapshot_id: str
    engineering_files: list[dict[str, str]]
    seed_result: dict[str, object]
    governed_baseline_snapshot_id: str


class PublishedProjectBaselineIssueCode(StrEnum):
    """Stable failures for application-owned published baseline selection."""

    INVALID_PROJECT_NAME = "INVALID_PROJECT_NAME"
    PROJECTS_ROOT = "PROJECTS_ROOT"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    PROJECT_ROOT_SYMLINK = "PROJECT_ROOT_SYMLINK"
    UNSUPPORTED_ENTRY = "UNSUPPORTED_ENTRY"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    ENGINEERING_DRIFT = "ENGINEERING_DRIFT"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"


class PublishedProjectBaselineError(RuntimeError):
    """Bounded selection failure that exposes no caller-chosen filesystem path."""

    def __init__(
        self,
        code: PublishedProjectBaselineIssueCode,
        message: str,
        *,
        project_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.project_name = project_name


@dataclass(frozen=True, slots=True)
class PublishedProjectBaseline:
    """Verified application capability for one published engineering projection."""

    project_name: str
    project_root: Path
    root_device: int
    root_inode: int
    originating_run_id: str
    workflow_project_name: str | None
    publication_bundle_sha256: str
    source_snapshot: WorkspaceSnapshot

    @property
    def engineering_files(self) -> tuple[WorkspaceFileState, ...]:
        """Return the exact published file allowlist, excluding SDLC evidence."""

        return self.source_snapshot.files


class BrownfieldBaselineProvenance(BaseModel):
    """Immutable lineage from a published project to a seeded run baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["brownfield-baseline-v1"]
    baseline_id: str = Field(min_length=1)
    selected_project_name: str = Field(min_length=1)
    originating_run_id: str = Field(min_length=1)
    workflow_project_name: str | None
    publication_bundle_sha256: str
    source_workspace_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    engineering_files: tuple[WorkspaceFileState, ...]
    seed_result: WorkspaceSeedResult
    governed_baseline_snapshot_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if _canonical_project_name(self.selected_project_name) != (
            self.selected_project_name
        ):
            raise ValueError("Brownfield baseline project name must be canonical.")
        if not _is_sha256(self.publication_bundle_sha256):
            raise ValueError(
                "Brownfield publication bundle identity must be lowercase SHA-256."
            )
        paths = tuple(item.path for item in self.engineering_files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError(
                "Brownfield engineering files must be uniquely canonical."
            )
        source_hashes = {
            item.path: item.content_hash for item in self.engineering_files
        }
        seeded_source_hashes = {
            item.path: item.source_content_hash for item in self.seed_result.files
        }
        seeded_destination_hashes = {
            item.path: item.seeded_content_hash for item in self.seed_result.files
        }
        if (
            not self.seed_result.verified
            or source_hashes != seeded_source_hashes
            or source_hashes != seeded_destination_hashes
            or self.seed_result.baseline_snapshot_id
            != self.governed_baseline_snapshot_id
        ):
            raise ValueError(
                "Brownfield seed evidence does not match the published projection."
            )
        if self.baseline_id != _brownfield_baseline_id(self):
            raise ValueError("Brownfield baseline identity is not canonical.")
        return self


class PublishedProjectCatalog:
    """Enumerate successful publications under the managed ``projects/`` root.

    The catalog accepts logical names only.  Successful publication evidence
    determines the engineering file allowlist; ungoverned files added later are
    not silently promoted into a brownfield run.
    """

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = Path(repository_root).resolve()
        self._projects_root = self._repository_root / "projects"

    @property
    def projects_root(self) -> Path:
        """Return the application-owned publication root."""

        return self._projects_root

    def eligible_projects(self) -> tuple[PublishedProjectBaseline, ...]:
        """Return direct, evidence-valid published project baselines."""

        try:
            projects_root, _ = _validated_real_directory(
                self._projects_root,
                code=PublishedProjectBaselineIssueCode.PROJECTS_ROOT,
                message="Managed projects root is unavailable or unsafe.",
            )
        except PublishedProjectBaselineError as error:
            try:
                self._projects_root.lstat()
            except FileNotFoundError:
                return ()
            except OSError:
                pass
            raise error
        try:
            with os.scandir(projects_root) as directory_entries:
                entries = tuple(directory_entries)
        except OSError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECTS_ROOT,
                "Managed projects root could not be enumerated.",
            ) from error

        eligible: list[PublishedProjectBaseline] = []
        for entry in sorted(entries, key=lambda item: item.name):
            try:
                baseline = self.select(entry.name)
            except PublishedProjectBaselineError:
                continue
            eligible.append(baseline)
        return tuple(eligible)

    def select(self, project_name: str) -> PublishedProjectBaseline:
        """Resolve and verify one direct published child by logical name."""

        canonical_name = _canonical_project_name(project_name)
        projects_root, _ = _validated_real_directory(
            self._projects_root,
            code=PublishedProjectBaselineIssueCode.PROJECTS_ROOT,
            message="Managed projects root is unavailable or unsafe.",
        )
        project_root = projects_root / canonical_name
        try:
            supplied_status = project_root.lstat()
        except FileNotFoundError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECT_NOT_FOUND,
                "Selected published project does not exist.",
                project_name=canonical_name,
            ) from error
        except OSError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECT_NOT_FOUND,
                "Selected published project could not be inspected.",
                project_name=canonical_name,
            ) from error
        if stat.S_ISLNK(supplied_status.st_mode):
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECT_ROOT_SYMLINK,
                "Selected published project root must not be a symlink.",
                project_name=canonical_name,
            )
        if not stat.S_ISDIR(supplied_status.st_mode):
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.UNSUPPORTED_ENTRY,
                "Selected published project root must be a directory.",
                project_name=canonical_name,
            )
        try:
            resolved_root = project_root.resolve(strict=True)
        except OSError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECT_NOT_FOUND,
                "Selected published project root became unavailable.",
                project_name=canonical_name,
            ) from error
        if resolved_root != project_root or resolved_root.parent != projects_root:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.PROJECT_ROOT_SYMLINK,
                "Selected project is not a direct canonical projects child.",
                project_name=canonical_name,
            )
        root_identity = supplied_status.st_dev, supplied_status.st_ino

        manifest, source_snapshot = _published_evidence(
            resolved_root,
            project_name=canonical_name,
        )
        expected_files = source_snapshot.files
        if any(
            item.path.split("/", 1)[0].casefold()
            == SDLC_ARTIFACT_DIRECTORY_NAME.casefold()
            for item in expected_files
        ):
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                "Published engineering evidence includes the reserved SDLC namespace.",
                project_name=canonical_name,
            )
        try:
            observed_files = verify_approved_source_files(
                resolved_root,
                relative_paths=tuple(item.path for item in expected_files),
            )
        except WorkspaceSeedingError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT,
                "Published engineering projection could not be verified.",
                project_name=canonical_name,
            ) from error
        if observed_files != expected_files:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT,
                "Published engineering content differs from its authoritative "
                "snapshot.",
                project_name=canonical_name,
            )
        _require_root_identity(resolved_root, root_identity, canonical_name)
        return PublishedProjectBaseline(
            project_name=canonical_name,
            project_root=resolved_root,
            root_device=root_identity[0],
            root_inode=root_identity[1],
            originating_run_id=manifest.run_id,
            workflow_project_name=manifest.project_name,
            publication_bundle_sha256=manifest.bundle_sha256,
            source_snapshot=source_snapshot,
        )

    def require_available_output(
        self,
        project_name: str,
        *,
        baseline_project_name: str,
    ) -> str:
        """Validate an explicit, non-overwriting brownfield destination name."""

        output_name = _canonical_project_name(project_name)
        baseline_name = _canonical_project_name(baseline_project_name)
        if output_name == baseline_name:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.DESTINATION_EXISTS,
                "Brownfield output must not overwrite its selected baseline.",
                project_name=output_name,
            )
        projects_root, _ = _validated_real_directory(
            self._projects_root,
            code=PublishedProjectBaselineIssueCode.PROJECTS_ROOT,
            message="Managed projects root is unavailable or unsafe.",
        )
        destination = projects_root / output_name
        try:
            destination.lstat()
        except FileNotFoundError:
            return output_name
        except OSError as error:
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.DESTINATION_EXISTS,
                "Brownfield output destination could not be inspected.",
                project_name=output_name,
            ) from error
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.DESTINATION_EXISTS,
            "Brownfield output destination already exists.",
            project_name=output_name,
        )

    def require_current_identity(self, baseline: PublishedProjectBaseline) -> None:
        """Fail if a selected root is replaced between verification and seeding."""

        expected = baseline.root_device, baseline.root_inode
        _require_root_identity(
            baseline.project_root,
            expected,
            baseline.project_name,
        )


def build_brownfield_baseline_provenance(
    baseline: PublishedProjectBaseline,
    seed_result: WorkspaceSeedResult,
    seeded_snapshot: WorkspaceSnapshot,
) -> BrownfieldBaselineProvenance:
    """Bind publication evidence to the exact new governed baseline snapshot."""

    if seeded_snapshot.workspace_id != seed_result.workspace_id:
        raise ValueError("Brownfield seed workspace identities differ.")
    if seeded_snapshot.snapshot_id != seed_result.baseline_snapshot_id:
        raise ValueError("Brownfield seed snapshot identities differ.")
    if seeded_snapshot.files != baseline.engineering_files:
        raise ValueError(
            "Brownfield seeded files differ from the published projection."
        )
    values = {
        "schema_version": BROWNFIELD_BASELINE_SCHEMA_VERSION,
        "baseline_id": "pending",
        "selected_project_name": baseline.project_name,
        "originating_run_id": baseline.originating_run_id,
        "workflow_project_name": baseline.workflow_project_name,
        "publication_bundle_sha256": baseline.publication_bundle_sha256,
        "source_workspace_id": baseline.source_snapshot.workspace_id,
        "source_snapshot_id": baseline.source_snapshot.snapshot_id,
        "engineering_files": baseline.engineering_files,
        "seed_result": seed_result,
        "governed_baseline_snapshot_id": seeded_snapshot.snapshot_id,
    }
    provisional = BrownfieldBaselineProvenance.model_construct(**values)
    values["baseline_id"] = _brownfield_baseline_id(provisional)
    return BrownfieldBaselineProvenance.model_validate(values)


def brownfield_baseline_from_value(
    value: BrownfieldBaselineProvenance | Mapping[str, object],
) -> BrownfieldBaselineProvenance:
    """Restore and revalidate canonical lineage from checkpoint-safe state."""

    if isinstance(value, BrownfieldBaselineProvenance):
        return BrownfieldBaselineProvenance.model_validate_json(
            value.model_dump_json()
        )
    return BrownfieldBaselineProvenance.model_validate_json(
        json.dumps(_plain_json_value(value))
    )


def _published_evidence(
    project_root: Path,
    *,
    project_name: str,
) -> tuple[SDLCArtifactManifest, WorkspaceSnapshot]:
    artifact_root = project_root / SDLC_ARTIFACT_DIRECTORY_NAME
    try:
        contents = _read_flat_regular_file_directory(artifact_root)
    except PublishedProjectBaselineError as error:
        raise PublishedProjectBaselineError(
            error.code,
            str(error),
            project_name=project_name,
        ) from error
    manifest_contents = contents.get(SDLC_ARTIFACT_MANIFEST_FILENAME)
    if manifest_contents is None:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published project evidence is missing manifest.json.",
            project_name=project_name,
        )
    try:
        manifest = SDLCArtifactManifest.model_validate_json(manifest_contents)
    except ValidationError as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published project manifest is malformed or noncanonical.",
            project_name=project_name,
        ) from error
    if manifest.workflow_status != "success" or manifest.exit_gate_passed is not True:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published baseline requires successful exit-gated evidence.",
            project_name=project_name,
        )
    manifested_names = tuple(item.path for item in manifest.files)
    expected_names = tuple(
        sorted((*manifested_names, SDLC_ARTIFACT_MANIFEST_FILENAME))
    )
    if tuple(sorted(contents)) != expected_names:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published SDLC files do not exactly match manifest.json.",
            project_name=project_name,
        )
    for record in manifest.files:
        value = contents[record.path]
        if len(value) != record.size_bytes or hashlib.sha256(value).hexdigest() != (
            record.sha256
        ):
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                "Published SDLC evidence content differs from manifest.json.",
                project_name=project_name,
            )

    workspace_contents = contents.get("workspace_execution.json")
    if workspace_contents is None:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published baseline lacks workspace lineage evidence.",
            project_name=project_name,
        )
    try:
        value = json.loads(workspace_contents.decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("workspace evidence must be an object")
        session = GovernedWorkspaceSession.model_validate_json(
            json.dumps(value["session"])
        )
        snapshots = tuple(
            WorkspaceSnapshot.model_validate_json(json.dumps(item))
            for item in value["snapshots"]
        )
    except (
        KeyError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published workspace lineage is malformed.",
            project_name=project_name,
        ) from error
    authoritative = tuple(
        item
        for item in snapshots
        if item.snapshot_id == session.authoritative_snapshot_id
    )
    if (
        session.run_id != manifest.run_id
        or session.integrity_status is not WorkspaceIntegrityStatus.VERIFIED
        or len(authoritative) != 1
        or authoritative[0].workspace_id != session.workspace_id
    ):
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published workspace lineage is not authoritative and verified.",
            project_name=project_name,
        )
    return manifest, authoritative[0]


def _read_flat_regular_file_directory(root: Path) -> dict[str, bytes]:
    try:
        status = root.lstat()
    except OSError as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published SDLC evidence directory is unavailable.",
        ) from error
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published SDLC evidence root must be a real directory.",
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published SDLC evidence directory could not be opened safely.",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise PublishedProjectBaselineError(
                PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                "Published SDLC evidence directory changed identity.",
            )
        names = tuple(sorted(os.listdir(descriptor)))
        contents: dict[str, bytes] = {}
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise PublishedProjectBaselineError(
                    PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                    "Published SDLC evidence contains an unsafe entry name.",
                )
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                raise PublishedProjectBaselineError(
                    PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                    "Published SDLC evidence contains a non-regular entry.",
                )
            file_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                file_flags |= os.O_NOFOLLOW
            file_descriptor = os.open(name, file_flags, dir_fd=descriptor)
            try:
                opened_entry = os.fstat(file_descriptor)
                if (opened_entry.st_dev, opened_entry.st_ino) != (
                    entry.st_dev,
                    entry.st_ino,
                ):
                    raise PublishedProjectBaselineError(
                        PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
                        "Published SDLC evidence file changed identity.",
                    )
                chunks: list[bytes] = []
                while chunk := os.read(file_descriptor, 1024 * 1024):
                    chunks.append(chunk)
                contents[name] = b"".join(chunks)
            finally:
                os.close(file_descriptor)
        return contents
    except PublishedProjectBaselineError:
        raise
    except OSError as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.EVIDENCE_INVALID,
            "Published SDLC evidence could not be verified.",
        ) from error
    finally:
        os.close(descriptor)


def _validated_real_directory(
    path: Path,
    *,
    code: PublishedProjectBaselineIssueCode,
    message: str,
) -> tuple[Path, tuple[int, int]]:
    try:
        supplied = path.lstat()
        resolved = path.resolve(strict=True)
        observed = resolved.lstat()
    except OSError as error:
        raise PublishedProjectBaselineError(code, message) from error
    if (
        stat.S_ISLNK(supplied.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or (supplied.st_dev, supplied.st_ino) != (observed.st_dev, observed.st_ino)
    ):
        raise PublishedProjectBaselineError(code, message)
    return resolved, (observed.st_dev, observed.st_ino)


def _require_root_identity(
    root: Path,
    expected: tuple[int, int],
    project_name: str,
) -> None:
    try:
        status = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT,
            "Published project root became unavailable.",
            project_name=project_name,
        ) from error
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or resolved != root
        or (status.st_dev, status.st_ino) != expected
    ):
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.ENGINEERING_DRIFT,
            "Published project root changed identity during selection.",
            project_name=project_name,
        )


def _canonical_project_name(project_name: str) -> str:
    from agentic_sdlc.project_export import ProjectNameError, normalize_project_name

    try:
        return normalize_project_name(project_name)
    except (TypeError, ProjectNameError) as error:
        raise PublishedProjectBaselineError(
            PublishedProjectBaselineIssueCode.INVALID_PROJECT_NAME,
            "Published project selection requires one safe logical name.",
        ) from error


def _brownfield_baseline_id(value: BrownfieldBaselineProvenance) -> str:
    payload = {
        "schema_version": value.schema_version,
        "selected_project_name": value.selected_project_name,
        "originating_run_id": value.originating_run_id,
        "workflow_project_name": value.workflow_project_name,
        "publication_bundle_sha256": value.publication_bundle_sha256,
        "source_workspace_id": value.source_workspace_id,
        "source_snapshot_id": value.source_snapshot_id,
        "engineering_files": [
            item.model_dump(mode="json") for item in value.engineering_files
        ],
        "seed_result": value.seed_result.model_dump(mode="json"),
        "governed_baseline_snapshot_id": value.governed_baseline_snapshot_id,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"BROWNFIELD-BASELINE-{digest[:20].upper()}"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _plain_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value
