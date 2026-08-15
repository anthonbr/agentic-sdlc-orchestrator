"""Trusted baseline population for an empty factory-created workspace.

Seeding is scenario/bootstrap preparation performed before a governed workspace
session exists. It is intentionally separate from agent-authored change sets and
transactional task mutation.
"""

from __future__ import annotations

import hashlib
import os
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentic_sdlc.workspace_contracts import (
    WorkspaceContractError,
    WorkspaceFileState,
    WorkspaceSnapshot,
    normalize_repository_path,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    snapshot_isolated_workspace,
)


class WorkspaceSeedingIssueCode(StrEnum):
    """Stable failures for approved baseline population."""

    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_PATH = "SOURCE_PATH"
    DUPLICATE_PATH = "DUPLICATE_PATH"
    SOURCE_SYMLINK = "SOURCE_SYMLINK"
    UNSUPPORTED_SOURCE_TYPE = "UNSUPPORTED_SOURCE_TYPE"
    WORKSPACE_NOT_EMPTY = "WORKSPACE_NOT_EMPTY"
    DESTINATION_FAILURE = "DESTINATION_FAILURE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class WorkspaceSeedingError(RuntimeError):
    """Bounded baseline-population failure with no absolute-path evidence."""

    def __init__(
        self,
        code: WorkspaceSeedingIssueCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class WorkspaceSeedFileEvidence(BaseModel):
    """Proof that one approved source file exactly matches its seeded copy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    source_content_hash: str
    seeded_content_hash: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_repository_path(value)

    @field_validator("source_content_hash", "seeded_content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("Workspace seed hashes must be lowercase SHA-256 values.")
        return value


class WorkspaceSeedResult(BaseModel):
    """Immutable verified evidence for one pre-session baseline population."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_root: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    baseline_snapshot_id: str = Field(min_length=1)
    files: tuple[WorkspaceSeedFileEvidence, ...]
    verified: bool

    @field_validator("source_root")
    @classmethod
    def validate_source_root(cls, value: str) -> str:
        return normalize_repository_path(value)


def verify_approved_source_files(
    source_root: Path,
    *,
    relative_paths: tuple[str, ...],
) -> tuple[WorkspaceFileState, ...]:
    """Verify one explicit regular-file projection without following links.

    This is the read-only companion to baseline seeding.  Callers select the
    paths through application-owned evidence; this boundary proves that those
    exact source files still exist with stable content before they are granted
    baseline authority.
    """

    try:
        paths = tuple(
            sorted(normalize_repository_path(path) for path in relative_paths)
        )
    except (TypeError, WorkspaceContractError) as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.SOURCE_PATH,
            "Approved baseline path violates repository path policy.",
        ) from error
    if len(paths) != len(set(paths)):
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.DUPLICATE_PATH,
            "Approved baseline paths must be unique.",
        )

    canonical_source = _validated_source_root(source_root)
    first = tuple(
        WorkspaceFileState(
            path=path,
            content_hash=hashlib.sha256(
                _read_source_regular_file(canonical_source, path)
            ).hexdigest(),
        )
        for path in paths
    )
    second = tuple(
        WorkspaceFileState(
            path=path,
            content_hash=hashlib.sha256(
                _read_source_regular_file(canonical_source, path)
            ).hexdigest(),
        )
        for path in paths
    )
    if first != second:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.VERIFICATION_FAILED,
            "Approved source changed during projection verification.",
        )
    return first


def seed_isolated_workspace_from_approved_files(
    workspace: IsolatedWorkspace,
    *,
    source_root: Path,
    source_root_label: str,
    relative_paths: tuple[str, ...],
) -> tuple[WorkspaceSeedResult, WorkspaceSnapshot]:
    """Populate one empty capability from an explicit approved regular-file set."""

    try:
        source_label = normalize_repository_path(source_root_label)
        paths = tuple(
            sorted(normalize_repository_path(path) for path in relative_paths)
        )
    except (TypeError, WorkspaceContractError) as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.SOURCE_PATH,
            "Approved baseline path violates repository path policy.",
        ) from error
    if len(paths) != len(set(paths)):
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.DUPLICATE_PATH,
            "Approved baseline paths must be unique.",
        )
    try:
        initial_snapshot = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.DESTINATION_FAILURE,
            "Isolated workspace is unavailable for baseline population.",
        ) from error
    if initial_snapshot.files:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.WORKSPACE_NOT_EMPTY,
            "Approved baseline population requires an empty isolated workspace.",
        )

    canonical_source = _validated_source_root(source_root)
    source_contents = tuple(
        (path, _read_source_regular_file(canonical_source, path)) for path in paths
    )
    for path, contents in source_contents:
        _write_seed_file(workspace.root, path, contents)

    try:
        baseline_snapshot = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.VERIFICATION_FAILED,
            "Seeded workspace could not be snapshotted for verification.",
        ) from error

    expected_hashes = {
        path: hashlib.sha256(contents).hexdigest()
        for path, contents in source_contents
    }
    actual_hashes = {
        item.path: item.content_hash for item in baseline_snapshot.files
    }
    if actual_hashes != expected_hashes:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.VERIFICATION_FAILED,
            "Seeded workspace does not exactly match the approved source files.",
        )

    evidence: list[WorkspaceSeedFileEvidence] = []
    for path in paths:
        current_source_hash = hashlib.sha256(
            _read_source_regular_file(canonical_source, path)
        ).hexdigest()
        if current_source_hash != expected_hashes[path]:
            raise WorkspaceSeedingError(
                WorkspaceSeedingIssueCode.VERIFICATION_FAILED,
                "Approved source changed during baseline population.",
                path=path,
            )
        evidence.append(
            WorkspaceSeedFileEvidence(
                path=path,
                source_content_hash=current_source_hash,
                seeded_content_hash=actual_hashes[path],
            )
        )

    return (
        WorkspaceSeedResult(
            source_root=source_label,
            workspace_id=workspace.workspace_id,
            baseline_snapshot_id=baseline_snapshot.snapshot_id,
            files=tuple(evidence),
            verified=True,
        ),
        baseline_snapshot,
    )


def _validated_source_root(source_root: Path) -> Path:
    supplied = Path(source_root)
    try:
        supplied_status = supplied.lstat()
        canonical = supplied.resolve(strict=True)
        canonical_status = canonical.lstat()
    except OSError as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.SOURCE_UNAVAILABLE,
            "Approved baseline source root is unavailable.",
        ) from error
    if stat.S_ISLNK(supplied_status.st_mode):
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.SOURCE_SYMLINK,
            "Approved baseline source root must not be a symlink.",
        )
    if not stat.S_ISDIR(canonical_status.st_mode):
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.UNSUPPORTED_SOURCE_TYPE,
            "Approved baseline source root must be a directory.",
        )
    return canonical


def _read_source_regular_file(source_root: Path, path: str) -> bytes:
    candidate = source_root
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            candidate_status = candidate.lstat()
        except OSError as error:
            raise WorkspaceSeedingError(
                WorkspaceSeedingIssueCode.SOURCE_UNAVAILABLE,
                "Approved baseline source file is unavailable.",
                path=path,
            ) from error
        if stat.S_ISLNK(candidate_status.st_mode):
            raise WorkspaceSeedingError(
                WorkspaceSeedingIssueCode.SOURCE_SYMLINK,
                "Approved baseline source path must not contain symlinks.",
                path=path,
            )
        is_target = index == len(parts) - 1
        expected_type = (
            stat.S_ISREG(candidate_status.st_mode)
            if is_target
            else stat.S_ISDIR(candidate_status.st_mode)
        )
        if not expected_type:
            raise WorkspaceSeedingError(
                WorkspaceSeedingIssueCode.UNSUPPORTED_SOURCE_TYPE,
                "Approved baseline source path has an unsupported file type.",
                path=path,
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
        try:
            descriptor_status = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_status.st_mode):
                raise WorkspaceSeedingError(
                    WorkspaceSeedingIssueCode.UNSUPPORTED_SOURCE_TYPE,
                    "Approved baseline source target is not a regular file.",
                    path=path,
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except WorkspaceSeedingError:
        raise
    except OSError as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.SOURCE_UNAVAILABLE,
            "Approved baseline source file could not be read.",
            path=path,
        ) from error


def _write_seed_file(workspace_root: Path, path: str, contents: bytes) -> None:
    target = workspace_root
    parts = PurePosixPath(path).parts
    try:
        for part in parts[:-1]:
            target = target / part
            try:
                target.mkdir()
            except FileExistsError:
                target_status = target.lstat()
                if stat.S_ISLNK(target_status.st_mode) or not stat.S_ISDIR(
                    target_status.st_mode
                ):
                    raise WorkspaceSeedingError(
                        WorkspaceSeedingIssueCode.DESTINATION_FAILURE,
                        "Seed destination parent is not a real directory.",
                        path=path,
                    )
        destination = target / parts[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o666)
        try:
            view = memoryview(contents)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise OSError("Seed write made no progress.")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except WorkspaceSeedingError:
        raise
    except OSError as error:
        raise WorkspaceSeedingError(
            WorkspaceSeedingIssueCode.DESTINATION_FAILURE,
            "Approved baseline file could not be created in the workspace.",
            path=path,
        ) from error
