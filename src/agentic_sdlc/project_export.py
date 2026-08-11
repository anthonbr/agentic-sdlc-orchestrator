"""Governed promotion of a verified isolated workspace into a durable project."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from agentic_sdlc.state import WorkflowState, WorkflowStatus
from agentic_sdlc.workspace_contracts import (
    WorkspaceSnapshot,
    build_workspace_snapshot,
)
from agentic_sdlc.workspace_integration_contracts import (
    GovernedWorkspaceSession,
    WorkspaceIntegrityStatus,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    read_isolated_workspace_file,
    snapshot_directory_tree,
    snapshot_isolated_workspace,
)


_PROJECT_NAME_COMPONENT = re.compile(r"[a-z0-9]+")
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_MAX_PROJECT_NAME_LENGTH = 80
_STAGING_NAME_ATTEMPTS = 100


@dataclass(frozen=True, slots=True)
class _DirectoryCapability:
    """Opened directory identity used for descriptor-relative filesystem effects."""

    descriptor: int
    identity: tuple[int, int]


class ProjectExportStatus(StrEnum):
    """Finite outcomes for one durable project promotion attempt."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProjectExportIssueCode(StrEnum):
    """Stable failure categories for project export."""

    INELIGIBLE_WORKFLOW = "INELIGIBLE_WORKFLOW"
    INVALID_AUTHORITY = "INVALID_AUTHORITY"
    INVALID_PROJECT_NAME = "INVALID_PROJECT_NAME"
    SOURCE_INTEGRITY = "SOURCE_INTEGRITY"
    EXPORT_ROOT = "EXPORT_ROOT"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    COPY_FAILED = "COPY_FAILED"
    POST_EXPORT_INTEGRITY = "POST_EXPORT_INTEGRITY"


class ProjectExportContractError(ValueError):
    """Raised when a workflow cannot supply an authoritative export request."""


class ProjectNameError(ValueError):
    """Raised when an explicitly requested project name is unsafe or meaningless."""


@dataclass(frozen=True, slots=True)
class ProjectExportValidation:
    """Integrity evidence recorded across the source and destination boundary."""

    authoritative_snapshot_id: str
    pre_export_snapshot_id: str | None = None
    staged_snapshot_id: str | None = None
    post_export_snapshot_id: str | None = None
    source_matches_authority: bool = False
    export_matches_authority: bool = False


@dataclass(frozen=True, slots=True)
class ProjectExportRequest:
    """Complete authority and naming input for one project promotion attempt."""

    run_id: str
    workspace: IsolatedWorkspace
    session: GovernedWorkspaceSession
    authoritative_snapshot: WorkspaceSnapshot
    workflow_status: WorkflowStatus
    exit_gate_passed: bool
    requested_project_name: str | None
    workflow_project_name: str | None
    export_root: Path


@dataclass(frozen=True, slots=True)
class ProjectExportResult:
    """Typed result for a durable project promotion attempt."""

    status: ProjectExportStatus
    requested_project_name: str | None
    project_name: str | None
    export_root: Path
    destination_directory: Path | None
    exported_file_count: int
    validation: ProjectExportValidation
    issue_code: ProjectExportIssueCode | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether the verified durable promotion completed."""

        return self.status is ProjectExportStatus.SUCCEEDED


def normalize_project_name(project_name: str) -> str:
    """Return a conservative lowercase slug for one explicit folder name."""

    if not isinstance(project_name, str):
        raise ProjectNameError("Project name must be text.")
    stripped = project_name.strip()
    if not stripped:
        raise ProjectNameError("Project name must not be empty.")
    if (
        stripped in {".", ".."}
        or ".." in stripped
        or "/" in stripped
        or "\\" in stripped
        or Path(stripped).is_absolute()
        or _WINDOWS_DRIVE_PREFIX.match(stripped)
    ):
        raise ProjectNameError("Project name must be a single safe folder name.")

    ascii_name = (
        unicodedata.normalize("NFKD", stripped)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .lower()
    )
    slug = "-".join(_PROJECT_NAME_COMPONENT.findall(ascii_name))
    slug = slug[:_MAX_PROJECT_NAME_LENGTH].rstrip("-")
    if not slug or slug in {".", ".."}:
        raise ProjectNameError(
            "Project name must contain at least one letter or number."
        )
    return slug


def project_export_request_from_state(
    state: WorkflowState,
    *,
    workspace: IsolatedWorkspace,
    export_root: Path,
    requested_project_name: str | None = None,
) -> ProjectExportRequest:
    """Bind terminal workflow evidence to the exact retained live capability."""

    run_id = state.get("run_id", "")
    session_value = state.get("governed_workspace_session")
    if not run_id or session_value is None:
        raise ProjectExportContractError(
            "Completed workflow has no governed workspace session."
        )
    try:
        session = (
            session_value
            if isinstance(session_value, GovernedWorkspaceSession)
            else GovernedWorkspaceSession.model_validate(session_value)
        )
        snapshots = tuple(
            snapshot
            if isinstance(snapshot, WorkspaceSnapshot)
            else WorkspaceSnapshot.model_validate(snapshot)
            for snapshot in state.get("workspace_snapshots", [])
        )
    except ValidationError as exc:
        raise ProjectExportContractError(
            "Workflow workspace evidence is not a valid export contract."
        ) from exc
    authoritative = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.snapshot_id == session.authoritative_snapshot_id
    )
    if len(authoritative) != 1:
        raise ProjectExportContractError(
            "Completed workflow must retain exactly one authoritative snapshot."
        )
    return ProjectExportRequest(
        run_id=run_id,
        workspace=workspace,
        session=session,
        authoritative_snapshot=authoritative[0],
        workflow_status=state.get("workflow_status", "pending"),
        exit_gate_passed=state.get("exit_gate_passed") is True,
        requested_project_name=requested_project_name,
        workflow_project_name=state.get("project_name"),
        export_root=Path(export_root),
    )


class ProjectExporter:
    """Promote an eligible workspace through verified, non-overwriting copy."""

    def export(self, request: ProjectExportRequest) -> ProjectExportResult:
        """Export only a successful run's still-authoritative workspace state."""

        validation = ProjectExportValidation(
            authoritative_snapshot_id=request.authoritative_snapshot.snapshot_id
        )
        eligibility_failure = _eligibility_failure(request)
        if eligibility_failure is not None:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.INELIGIBLE_WORKFLOW,
                eligibility_failure,
            )

        authority_failure = _authority_failure(request)
        if authority_failure is not None:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.INVALID_AUTHORITY,
                authority_failure,
            )

        try:
            project_name, explicit_name = _select_project_name(request)
        except ProjectNameError as exc:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.INVALID_PROJECT_NAME,
                str(exc),
            )

        try:
            observed_source = snapshot_isolated_workspace(request.workspace)
        except WorkspaceRuntimeError as exc:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.SOURCE_INTEGRITY,
                _runtime_failure("Source workspace could not be verified", exc),
                project_name=project_name,
            )
        validation = ProjectExportValidation(
            authoritative_snapshot_id=request.authoritative_snapshot.snapshot_id,
            pre_export_snapshot_id=observed_source.snapshot_id,
            source_matches_authority=observed_source == request.authoritative_snapshot,
        )
        if not validation.source_matches_authority:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.SOURCE_INTEGRITY,
                "Source workspace drifted after authoritative validation.",
                project_name=project_name,
            )

        try:
            _require_descriptor_relative_support()
            export_root, export_root_identity = _prepare_export_root(
                request.export_root
            )
        except _ProjectExportFailure as exc:
            return _failure_result(
                request,
                validation,
                exc.code,
                str(exc),
                project_name=project_name,
            )

        try:
            project_name, destination = _select_destination(
                export_root,
                project_name,
                run_id=request.run_id,
                explicit_name=explicit_name,
            )
        except _ProjectExportFailure as exc:
            return _failure_result(
                request,
                validation,
                exc.code,
                str(exc),
                project_name=project_name,
                export_root=export_root,
                destination=export_root / project_name,
            )
        except OSError as exc:
            return _failure_result(
                request,
                validation,
                ProjectExportIssueCode.EXPORT_ROOT,
                f"Project destination could not be inspected: {exc}.",
                project_name=project_name,
                export_root=export_root,
            )

        try:
            with _open_directory_path(
                export_root,
                expected_identity=export_root_identity,
                issue_code=ProjectExportIssueCode.EXPORT_ROOT,
            ) as export_root_capability:
                return _export_with_root_capability(
                    request,
                    project_name=project_name,
                    export_root=export_root,
                    destination=destination,
                    export_root_capability=export_root_capability,
                    observed_source=observed_source,
                    validation=validation,
                )
        except _ProjectExportFailure as exc:
            return _failure_result(
                request,
                validation,
                exc.code,
                str(exc),
                project_name=project_name,
                export_root=export_root,
                destination=destination,
            )


def _export_with_root_capability(
    request: ProjectExportRequest,
    *,
    project_name: str,
    export_root: Path,
    destination: Path,
    export_root_capability: _DirectoryCapability,
    observed_source: WorkspaceSnapshot,
    validation: ProjectExportValidation,
) -> ProjectExportResult:
    """Run all export effects relative to one pinned export-root descriptor."""

    staging_name: str | None = None
    staging_identity: tuple[int, int] | None = None
    destination_identity: tuple[int, int] | None = None
    try:
        staging_name, staging_identity = _create_staging_directory_at(
            export_root_capability.descriptor,
            project_name,
        )
        staging_path = export_root / staging_name
        with _open_directory_at(
            export_root_capability.descriptor,
            staging_name,
            expected_identity=staging_identity,
            issue_code=ProjectExportIssueCode.COPY_FAILED,
        ) as staging_capability:
            staging_identity = staging_capability.identity
            _copy_authoritative_files(request, staging_capability)

            try:
                observed_after_copy = snapshot_isolated_workspace(request.workspace)
            except WorkspaceRuntimeError as exc:
                validation = ProjectExportValidation(
                    authoritative_snapshot_id=(
                        request.authoritative_snapshot.snapshot_id
                    ),
                    pre_export_snapshot_id=observed_source.snapshot_id,
                    source_matches_authority=False,
                )
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.SOURCE_INTEGRITY,
                    _runtime_failure(
                        "Source workspace could not be reverified after copying",
                        exc,
                    ),
                ) from exc
            if observed_after_copy != request.authoritative_snapshot:
                validation = ProjectExportValidation(
                    authoritative_snapshot_id=(
                        request.authoritative_snapshot.snapshot_id
                    ),
                    pre_export_snapshot_id=observed_source.snapshot_id,
                    source_matches_authority=False,
                )
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.SOURCE_INTEGRITY,
                    "Source workspace drifted while export contents were read.",
                )

            try:
                staged_snapshot = snapshot_directory_tree(
                    staging_path,
                    workspace_id=request.session.workspace_id,
                )
            except WorkspaceRuntimeError as exc:
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.COPY_FAILED,
                    _runtime_failure("Staged project could not be verified", exc),
                ) from exc
            validation = ProjectExportValidation(
                authoritative_snapshot_id=(
                    request.authoritative_snapshot.snapshot_id
                ),
                pre_export_snapshot_id=observed_source.snapshot_id,
                staged_snapshot_id=staged_snapshot.snapshot_id,
                source_matches_authority=True,
                export_matches_authority=(
                    staged_snapshot == request.authoritative_snapshot
                ),
            )
            if not validation.export_matches_authority:
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.COPY_FAILED,
                    "Staged project differs from the authoritative workspace.",
                )
            if not _directory_entry_matches(
                export_root_capability.descriptor,
                staging_name,
                staging_capability.identity,
            ) or not _path_directory_identity_matches(
                export_root,
                export_root_capability.identity,
            ):
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.COPY_FAILED,
                    "Staging or export-root identity changed after verification.",
                )

            destination_identity = _create_destination_directory_at(
                export_root_capability.descriptor,
                destination.name,
            )
            with _open_directory_at(
                export_root_capability.descriptor,
                destination.name,
                expected_identity=destination_identity,
                issue_code=ProjectExportIssueCode.COPY_FAILED,
            ) as destination_capability:
                destination_identity = destination_capability.identity
                _promote_staged_entries(
                    staging_capability,
                    destination_capability,
                )
                if not _remove_empty_owned_directory_at(
                    export_root_capability.descriptor,
                    staging_name,
                    staging_capability.identity,
                ):
                    raise _ProjectExportFailure(
                        ProjectExportIssueCode.COPY_FAILED,
                        "Staging directory cleanup identity could not be verified.",
                    )
                staging_name = None

                try:
                    exported_snapshot = snapshot_directory_tree(
                        destination,
                        workspace_id=request.session.workspace_id,
                    )
                except WorkspaceRuntimeError as exc:
                    raise _ProjectExportFailure(
                        ProjectExportIssueCode.POST_EXPORT_INTEGRITY,
                        _runtime_failure(
                            "Durable project could not be verified",
                            exc,
                        ),
                    ) from exc
                validation = ProjectExportValidation(
                    authoritative_snapshot_id=(
                        request.authoritative_snapshot.snapshot_id
                    ),
                    pre_export_snapshot_id=observed_source.snapshot_id,
                    staged_snapshot_id=staged_snapshot.snapshot_id,
                    post_export_snapshot_id=exported_snapshot.snapshot_id,
                    source_matches_authority=True,
                    export_matches_authority=(
                        exported_snapshot == request.authoritative_snapshot
                    ),
                )
                if not validation.export_matches_authority:
                    raise _ProjectExportFailure(
                        ProjectExportIssueCode.POST_EXPORT_INTEGRITY,
                        "Durable project differs from the authoritative workspace.",
                    )
                if not _directory_entry_matches(
                    export_root_capability.descriptor,
                    destination.name,
                    destination_capability.identity,
                ) or not _path_directory_identity_matches(
                    export_root,
                    export_root_capability.identity,
                ):
                    raise _ProjectExportFailure(
                        ProjectExportIssueCode.POST_EXPORT_INTEGRITY,
                        "Durable project path identity changed after verification.",
                    )
    except OSError as exc:
        failure = _ProjectExportFailure(
            ProjectExportIssueCode.COPY_FAILED,
            f"Project export filesystem operation failed: {exc}.",
        )
    except _ProjectExportFailure as exc:
        failure = exc
    else:
        return ProjectExportResult(
            status=ProjectExportStatus.SUCCEEDED,
            requested_project_name=request.requested_project_name,
            project_name=project_name,
            export_root=export_root,
            destination_directory=destination,
            exported_file_count=len(request.authoritative_snapshot.files),
            validation=validation,
        )

    cleanup_failures: list[str] = []
    if (
        staging_name is not None
        and staging_identity is not None
        and not _remove_owned_directory_at(
            export_root_capability.descriptor,
            staging_name,
            staging_identity,
        )
    ):
        cleanup_failures.append("staging directory cleanup was not provable")
    if destination_identity is not None and not _remove_owned_directory_at(
        export_root_capability.descriptor,
        destination.name,
        destination_identity,
    ):
        cleanup_failures.append("destination cleanup was not provable")
    reason = str(failure)
    if cleanup_failures:
        reason += " Cleanup warning: " + "; ".join(cleanup_failures) + "."
    return _failure_result(
        request,
        validation,
        failure.code,
        reason,
        project_name=project_name,
        export_root=export_root,
        destination=destination,
    )


class _ProjectExportFailure(RuntimeError):
    def __init__(self, code: ProjectExportIssueCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _eligibility_failure(request: ProjectExportRequest) -> str | None:
    if request.workflow_status != "success" or not request.exit_gate_passed:
        return "Only a workflow that passed its exit gate may be exported."
    if request.session.integrity_status is not WorkspaceIntegrityStatus.VERIFIED:
        return "Workspace integrity must be VERIFIED before export."
    return None


def _authority_failure(request: ProjectExportRequest) -> str | None:
    if not request.run_id or request.session.run_id != request.run_id:
        return "Export run identity differs from the governed session."
    if request.workspace.workspace_id != request.session.workspace_id:
        return "Live workspace capability differs from the governed session."
    if request.authoritative_snapshot.workspace_id != request.session.workspace_id:
        return "Authoritative snapshot belongs to a different workspace."
    if (
        request.authoritative_snapshot.snapshot_id
        != request.session.authoritative_snapshot_id
    ):
        return "Supplied snapshot is not authoritative for the governed session."
    canonical_snapshot = build_workspace_snapshot(
        request.authoritative_snapshot.workspace_id,
        request.authoritative_snapshot.files,
    )
    if canonical_snapshot != request.authoritative_snapshot:
        return "Authoritative snapshot identity is not canonical."
    return None


def _select_project_name(request: ProjectExportRequest) -> tuple[str, bool]:
    if request.requested_project_name is not None:
        return normalize_project_name(request.requested_project_name), True
    if request.workflow_project_name:
        try:
            return normalize_project_name(request.workflow_project_name), False
        except ProjectNameError:
            pass
    return f"project-{_run_suffix(request.run_id)}", False


def _require_descriptor_relative_support() -> None:
    required_dir_fd_functions = (
        os.open,
        os.mkdir,
        os.stat,
        os.rename,
        os.unlink,
        os.rmdir,
    )
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or any(
            function not in os.supports_dir_fd
            for function in required_dir_fd_functions
        )
        or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
    ):
        raise _ProjectExportFailure(
            ProjectExportIssueCode.EXPORT_ROOT,
            "Safe project export requires POSIX descriptor-relative no-follow "
            "filesystem operations.",
        )


def _prepare_export_root(
    export_root: Path,
) -> tuple[Path, tuple[int, int]]:
    candidate = Path(export_root)
    try:
        if _path_exists(candidate):
            candidate_stat = candidate.lstat()
            if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(
                candidate_stat.st_mode
            ):
                raise _ProjectExportFailure(
                    ProjectExportIssueCode.EXPORT_ROOT,
                    "Project export root must be a real directory.",
                )
        else:
            candidate.mkdir(parents=True, mode=0o700)
        resolved = candidate.resolve(strict=True)
        resolved_stat = resolved.lstat()
    except OSError as exc:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.EXPORT_ROOT,
            f"Project export root is unavailable: {exc}.",
        ) from exc
    if stat.S_ISLNK(resolved_stat.st_mode) or not stat.S_ISDIR(resolved_stat.st_mode):
        raise _ProjectExportFailure(
            ProjectExportIssueCode.EXPORT_ROOT,
            "Resolved project export root must be a real directory.",
        )
    return resolved, (resolved_stat.st_dev, resolved_stat.st_ino)


def _select_destination(
    export_root: Path,
    project_name: str,
    *,
    run_id: str,
    explicit_name: bool,
) -> tuple[str, Path]:
    first = _contained_destination(export_root, project_name)
    if not _path_exists(first):
        return project_name, first
    if explicit_name:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.DESTINATION_EXISTS,
            f"Project destination already exists: {first}.",
        )

    suffix = _run_suffix(run_id)
    candidates = [f"{project_name}-{suffix}"]
    candidates.extend(f"{project_name}-{suffix}-{index}" for index in range(2, 1001))
    for candidate_name in candidates:
        candidate = _contained_destination(export_root, candidate_name)
        if not _path_exists(candidate):
            return candidate_name, candidate
    raise _ProjectExportFailure(
        ProjectExportIssueCode.DESTINATION_EXISTS,
        "No available deterministic project destination could be selected.",
    )


def _contained_destination(export_root: Path, project_name: str) -> Path:
    destination = export_root / project_name
    try:
        destination.relative_to(export_root)
    except ValueError as exc:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.INVALID_PROJECT_NAME,
            "Project destination escapes the export root.",
        ) from exc
    if destination.parent != export_root:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.INVALID_PROJECT_NAME,
            "Project destination must be a direct child of the export root.",
        )
    return destination


def _copy_authoritative_files(
    request: ProjectExportRequest,
    staging: _DirectoryCapability,
) -> None:
    for expected in request.authoritative_snapshot.files:
        try:
            contents = read_isolated_workspace_file(request.workspace, expected.path)
        except WorkspaceRuntimeError as exc:
            raise _ProjectExportFailure(
                ProjectExportIssueCode.SOURCE_INTEGRITY,
                _runtime_failure(
                    f"Authoritative source file could not be read: {expected.path}",
                    exc,
                ),
            ) from exc
        if (
            contents is None
            or hashlib.sha256(contents).hexdigest() != expected.content_hash
        ):
            raise _ProjectExportFailure(
                ProjectExportIssueCode.SOURCE_INTEGRITY,
                f"Authoritative source file drifted: {expected.path}.",
            )
        components = _validated_export_path_components(expected.path)
        with _open_export_parent(
            staging.descriptor,
            components[:-1],
        ) as parent_descriptor:
            _write_new_file_at(parent_descriptor, components[-1], contents)


def _write_new_file_at(
    parent_descriptor: int,
    name: str,
    contents: bytes,
) -> None:
    _validate_child_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(
        name,
        flags,
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise _ProjectExportFailure(
                ProjectExportIssueCode.COPY_FAILED,
                "Created export entry is not a regular file.",
            )
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("regular-file write made no progress")
            view = view[written:]
    finally:
        os.close(descriptor)


def _create_staging_directory_at(
    export_root_descriptor: int,
    project_name: str,
) -> tuple[str, tuple[int, int]]:
    for _ in range(_STAGING_NAME_ATTEMPTS):
        name = f".{project_name}.staging-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=export_root_descriptor)
        except FileExistsError:
            continue
        try:
            return name, _directory_entry_identity(
                export_root_descriptor,
                name,
            )
        except _ProjectExportFailure:
            try:
                os.rmdir(name, dir_fd=export_root_descriptor)
            except OSError:
                pass
            raise
    raise _ProjectExportFailure(
        ProjectExportIssueCode.COPY_FAILED,
        "Unable to allocate a unique staging directory.",
    )


def _create_destination_directory_at(
    export_root_descriptor: int,
    name: str,
) -> tuple[int, int]:
    _validate_child_name(name)
    try:
        os.mkdir(name, mode=0o700, dir_fd=export_root_descriptor)
    except FileExistsError as exc:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.DESTINATION_EXISTS,
            "Project destination already exists.",
        ) from exc
    return _directory_entry_identity(export_root_descriptor, name)


def _promote_staged_entries(
    staging: _DirectoryCapability,
    destination: _DirectoryCapability,
) -> None:
    for name in sorted(os.listdir(staging.descriptor)):
        _validate_child_name(name)
        os.rename(
            name,
            name,
            src_dir_fd=staging.descriptor,
            dst_dir_fd=destination.descriptor,
        )


@contextmanager
def _open_directory_path(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    issue_code: ProjectExportIssueCode,
) -> Iterator[_DirectoryCapability]:
    descriptor = _open_directory_descriptor(
        path,
        expected_identity=expected_identity,
        issue_code=issue_code,
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor.descriptor)


@contextmanager
def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
    issue_code: ProjectExportIssueCode,
) -> Iterator[_DirectoryCapability]:
    descriptor = _open_directory_descriptor_at(
        parent_descriptor,
        name,
        expected_identity=expected_identity,
        issue_code=issue_code,
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor.descriptor)


def _open_directory_descriptor(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    issue_code: ProjectExportIssueCode,
) -> _DirectoryCapability:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _ProjectExportFailure(
            issue_code,
            f"Trusted export directory could not be opened safely: {exc}.",
        ) from exc
    return _validated_open_directory(
        descriptor,
        expected_identity=expected_identity,
        issue_code=issue_code,
    )


def _open_directory_descriptor_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    issue_code: ProjectExportIssueCode,
) -> _DirectoryCapability:
    _validate_child_name(name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise _ProjectExportFailure(
            issue_code,
            f"Export directory component could not be opened safely: {exc}.",
        ) from exc
    return _validated_open_directory(
        descriptor,
        expected_identity=expected_identity,
        issue_code=issue_code,
    )


def _validated_open_directory(
    descriptor: int,
    *,
    expected_identity: tuple[int, int] | None,
    issue_code: ProjectExportIssueCode,
) -> _DirectoryCapability:
    try:
        opened_stat = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    identity = opened_stat.st_dev, opened_stat.st_ino
    if not stat.S_ISDIR(opened_stat.st_mode) or (
        expected_identity is not None and identity != expected_identity
    ):
        os.close(descriptor)
        raise _ProjectExportFailure(
            issue_code,
            "Opened export directory identity is not the trusted directory.",
        )
    return _DirectoryCapability(descriptor=descriptor, identity=identity)


@contextmanager
def _open_export_parent(
    root_descriptor: int,
    components: tuple[str, ...],
) -> Iterator[int]:
    current = os.dup(root_descriptor)
    try:
        for component in components:
            _validate_child_name(component)
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = _open_directory_descriptor_at(
                current,
                component,
                issue_code=ProjectExportIssueCode.COPY_FAILED,
            )
            previous = current
            current = child.descriptor
            os.close(previous)
        yield current
    finally:
        os.close(current)


def _directory_entry_identity(
    parent_descriptor: int,
    name: str,
) -> tuple[int, int]:
    _validate_child_name(name)
    try:
        entry_stat = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.COPY_FAILED,
            f"Created export directory identity is unavailable: {exc}.",
        ) from exc
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise _ProjectExportFailure(
            ProjectExportIssueCode.COPY_FAILED,
            "Created export entry is not a real directory.",
        )
    return entry_stat.st_dev, entry_stat.st_ino


def _directory_entry_matches(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        return _directory_entry_identity(parent_descriptor, name) == expected_identity
    except _ProjectExportFailure:
        return False


def _path_directory_identity_matches(
    path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(path_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and (path_stat.st_dev, path_stat.st_ino) == expected_identity
    )


def _remove_owned_directory_at(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        with _open_directory_at(
            parent_descriptor,
            name,
            expected_identity=expected_identity,
            issue_code=ProjectExportIssueCode.COPY_FAILED,
        ) as owned:
            if not _clear_owned_directory(owned.descriptor):
                return False
    except (OSError, _ProjectExportFailure):
        return False
    return _remove_empty_owned_directory_at(
        parent_descriptor,
        name,
        expected_identity,
    )


def _clear_owned_directory(descriptor: int) -> bool:
    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError:
        return False
    for name in names:
        try:
            _validate_child_name(name)
            entry_stat = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except (OSError, _ProjectExportFailure):
            return False
        if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
            identity = entry_stat.st_dev, entry_stat.st_ino
            try:
                with _open_directory_at(
                    descriptor,
                    name,
                    expected_identity=identity,
                    issue_code=ProjectExportIssueCode.COPY_FAILED,
                ) as child:
                    if not _clear_owned_directory(child.descriptor):
                        return False
            except (OSError, _ProjectExportFailure):
                return False
            if not _remove_empty_owned_directory_at(descriptor, name, identity):
                return False
            continue
        try:
            os.unlink(name, dir_fd=descriptor)
        except OSError:
            return False
    return True


def _remove_empty_owned_directory_at(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> bool:
    if not _directory_entry_matches(parent_descriptor, name, expected_identity):
        return False
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    except OSError:
        return False
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _validated_export_path_components(path: str) -> tuple[str, ...]:
    components = tuple(path.split("/"))
    if not components:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.COPY_FAILED,
            "Authoritative export path has no components.",
        )
    for component in components:
        _validate_child_name(component)
    return components


def _validate_child_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise _ProjectExportFailure(
            ProjectExportIssueCode.COPY_FAILED,
            "Export path contains an unsafe child component.",
        )


def _failure_result(
    request: ProjectExportRequest,
    validation: ProjectExportValidation,
    issue_code: ProjectExportIssueCode,
    failure_reason: str,
    *,
    project_name: str | None = None,
    export_root: Path | None = None,
    destination: Path | None = None,
) -> ProjectExportResult:
    return ProjectExportResult(
        status=ProjectExportStatus.FAILED,
        requested_project_name=request.requested_project_name,
        project_name=project_name,
        export_root=Path(export_root or request.export_root),
        destination_directory=destination,
        exported_file_count=0,
        validation=validation,
        issue_code=issue_code,
        failure_reason=failure_reason,
    )


def _runtime_failure(prefix: str, error: WorkspaceRuntimeError) -> str:
    path = f" ({error.path})" if error.path else ""
    return f"{prefix}{path}: {error}."


def _run_suffix(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
