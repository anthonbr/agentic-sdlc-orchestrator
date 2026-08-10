"""Concrete authority for application-created isolated local workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from agentic_sdlc.workspace_contracts import (
    WorkspaceContractError,
    WorkspaceFileState,
    WorkspaceSnapshot,
    build_workspace_snapshot,
    normalize_repository_path,
)


_WORKSPACE_FACTORY_AUTHORITY = object()


class WorkspaceRuntimeIssueCode(StrEnum):
    """Stable failure categories for isolated-workspace inspection."""

    INVALID_CAPABILITY = "INVALID_CAPABILITY"
    WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
    PATH_POLICY = "PATH_POLICY"
    PATH_CONTAINMENT = "PATH_CONTAINMENT"
    SYMLINK_DETECTED = "SYMLINK_DETECTED"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"


class WorkspaceRuntimeError(RuntimeError):
    """Bounded runtime failure carrying canonical relative-path evidence."""

    def __init__(
        self,
        code: WorkspaceRuntimeIssueCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True, init=False)
class IsolatedWorkspace:
    """Factory-created capability for one unique isolated filesystem root.

    The private authority marker is an application API boundary rather than a
    cryptographic sandbox.  Root device/inode binding detects disappearance or
    replacement before authority is exercised.
    """

    workspace_id: str
    root: Path
    _root_device: int = field(repr=False)
    _root_inode: int = field(repr=False)
    _authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        workspace_id: str,
        root: Path,
        *,
        _authority: object | None = None,
        _root_device: int | None = None,
        _root_inode: int | None = None,
    ) -> None:
        if _authority is not _WORKSPACE_FACTORY_AUTHORITY:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
                "IsolatedWorkspace must be created by the application factory.",
            )
        if not workspace_id:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
                "workspace_id must be non-empty.",
            )
        if _root_device is None or _root_inode is None:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
                "Factory root identity is required.",
            )
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "_root_device", _root_device)
        object.__setattr__(self, "_root_inode", _root_inode)
        object.__setattr__(self, "_authority", _authority)


@dataclass(frozen=True, slots=True)
class _RuntimeFileState:
    path: str
    absolute_path: Path
    exists: bool
    content_hash: str | None
    mode: int | None
    device: int | None
    inode: int | None


@dataclass(frozen=True, slots=True)
class _CreatedDirectory:
    path: str
    absolute_path: Path
    device: int | None
    inode: int | None


def create_isolated_workspace(
    workspace_id: str,
    *,
    parent_directory: Path | None = None,
) -> IsolatedWorkspace:
    """Create and bind a unique empty directory as an isolated capability."""

    if not workspace_id:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "workspace_id must be non-empty.",
        )
    parent: str | None = None
    if parent_directory is not None:
        supplied_parent = Path(parent_directory)
        try:
            parent_stat = supplied_parent.lstat()
            canonical_parent = supplied_parent.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                "Requested workspace parent is unavailable.",
            ) from exc
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                "Requested workspace parent must be a real directory.",
            )
        parent = str(canonical_parent)
    try:
        created = Path(
            tempfile.mkdtemp(prefix="agentic-sdlc-workspace-", dir=parent)
        ).resolve(strict=True)
        root_stat = created.lstat()
    except OSError as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Unable to create isolated workspace.",
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
            "Created workspace root is not a real directory.",
        )
    return IsolatedWorkspace(
        workspace_id,
        created,
        _authority=_WORKSPACE_FACTORY_AUTHORITY,
        _root_device=root_stat.st_dev,
        _root_inode=root_stat.st_ino,
    )


def snapshot_isolated_workspace(workspace: IsolatedWorkspace) -> WorkspaceSnapshot:
    """Snapshot regular files without following symlinks or mutating state."""

    root = _validated_workspace_root(workspace)
    file_states: list[WorkspaceFileState] = []
    directories = [root]
    while directories:
        directory = directories.pop()
        relative_directory = _relative_path(root, directory)
        _require_real_directory(directory, relative_directory)
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                "Workspace directory became unavailable during snapshot.",
                path=relative_directory or None,
            ) from exc
        for entry in sorted(entries, key=lambda item: item.name):
            candidate = Path(entry.path)
            relative = _relative_path(root, candidate)
            try:
                candidate_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                    "Workspace entry became unavailable during snapshot.",
                    path=relative,
                ) from exc
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.SYMLINK_DETECTED,
                    "Symlinks are not permitted in isolated workspace snapshots.",
                    path=relative,
                )
            if stat.S_ISDIR(candidate_stat.st_mode):
                directories.append(candidate)
                continue
            if not stat.S_ISREG(candidate_stat.st_mode):
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                    "Unsupported filesystem entry in isolated workspace.",
                    path=relative,
                )
            contents, opened_stat = _read_regular_file(candidate, relative)
            if _identity(opened_stat) != _identity(candidate_stat):
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                    "Workspace file changed identity during snapshot.",
                    path=relative,
                )
            file_states.append(
                WorkspaceFileState(
                    path=relative,
                    content_hash=hashlib.sha256(contents).hexdigest(),
                )
            )
    _validated_workspace_root(workspace)
    return build_workspace_snapshot(workspace.workspace_id, tuple(file_states))


def _validated_workspace_root(workspace: object) -> Path:
    if not isinstance(workspace, IsolatedWorkspace):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "Mutation requires a factory-created IsolatedWorkspace capability.",
        )
    if getattr(workspace, "_authority", None) is not _WORKSPACE_FACTORY_AUTHORITY:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "IsolatedWorkspace authority marker is invalid.",
        )
    root = workspace.root
    if not root.is_absolute():
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "IsolatedWorkspace root is not canonical and absolute.",
        )
    try:
        root_stat = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Isolated workspace root is unavailable.",
        ) from exc
    if resolved != root:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "Isolated workspace root no longer resolves to its canonical path.",
        )
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Isolated workspace root is no longer a real directory.",
        )
    if _identity(root_stat) != (workspace._root_device, workspace._root_inode):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.INVALID_CAPABILITY,
            "Isolated workspace root identity changed.",
        )
    return root


def _inspect_workspace_target(
    workspace: IsolatedWorkspace,
    path: str,
) -> _RuntimeFileState:
    root = _validated_workspace_root(workspace)
    try:
        normalized = normalize_repository_path(path)
    except (TypeError, WorkspaceContractError) as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.PATH_POLICY,
            "Workspace destination violates repository path policy.",
            path=str(path),
        ) from exc
    parts = normalized.split("/")
    target = root.joinpath(*parts)
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.PATH_CONTAINMENT,
            "Workspace destination is outside the isolated root.",
            path=normalized,
        )

    current = root
    for index, part in enumerate(parts):
        relative = "/".join(parts[: index + 1])
        candidate = current / part
        try:
            names = {entry.name for entry in os.scandir(current)}
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            return _RuntimeFileState(
                path=normalized,
                absolute_path=target,
                exists=False,
                content_hash=None,
                mode=None,
                device=None,
                inode=None,
            )
        except OSError as exc:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                "Workspace path became unavailable during inspection.",
                path=relative,
            ) from exc
        if part not in names:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.PATH_CONTAINMENT,
                "Logical path aliases a differently named filesystem entry.",
                path=relative,
            )
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.SYMLINK_DETECTED,
                "Workspace path contains a symlink.",
                path=relative,
            )
        is_target = index == len(parts) - 1
        if not is_target:
            if not stat.S_ISDIR(candidate_stat.st_mode):
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                    "Workspace path parent is not a directory.",
                    path=relative,
                )
            current = candidate
            continue
        if not stat.S_ISREG(candidate_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                "Workspace mutation target is not a regular file.",
                path=normalized,
            )
        contents, opened_stat = _read_regular_file(candidate, normalized)
        if _identity(opened_stat) != _identity(candidate_stat):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                "Workspace target changed identity during inspection.",
                path=normalized,
            )
        return _RuntimeFileState(
            path=normalized,
            absolute_path=target,
            exists=True,
            content_hash=hashlib.sha256(contents).hexdigest(),
            mode=stat.S_IMODE(opened_stat.st_mode),
            device=opened_stat.st_dev,
            inode=opened_stat.st_ino,
        )
    raise AssertionError("repository path must contain at least one segment")


def _create_missing_parent_directories(
    workspace: IsolatedWorkspace,
    path: str,
    *,
    _created: list[_CreatedDirectory] | None = None,
) -> tuple[_CreatedDirectory, ...]:
    root = _validated_workspace_root(workspace)
    normalized = normalize_repository_path(path)
    parts = normalized.split("/")[:-1]
    created = [] if _created is None else _created
    current = root
    for index, part in enumerate(parts):
        relative = "/".join(parts[: index + 1])
        candidate = current / part
        try:
            names = {entry.name for entry in os.scandir(current)}
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(candidate, mode=0o700)
            except OSError as exc:
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                    "Unable to create transaction parent directory.",
                    path=relative,
                ) from exc
            created.append(
                _CreatedDirectory(
                    path=relative,
                    absolute_path=candidate,
                    device=None,
                    inode=None,
                )
            )
            try:
                candidate_stat = _lstat_created_directory(candidate)
            except OSError as exc:
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                    "Created transaction parent identity is unavailable.",
                    path=relative,
                ) from exc
            if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(
                candidate_stat.st_mode
            ):
                raise WorkspaceRuntimeError(
                    WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                    "Created parent is not a real directory.",
                    path=relative,
                )
            created[-1] = _CreatedDirectory(
                path=relative,
                absolute_path=candidate,
                device=candidate_stat.st_dev,
                inode=candidate_stat.st_ino,
            )
            current = candidate
            continue
        except OSError as exc:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
                "Workspace parent became unavailable.",
                path=relative,
            ) from exc
        if part not in names:
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.PATH_CONTAINMENT,
                "Logical parent aliases a differently named filesystem entry.",
                path=relative,
            )
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.SYMLINK_DETECTED,
                "Workspace parent is a symlink.",
                path=relative,
            )
        if not stat.S_ISDIR(candidate_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                "Workspace parent is not a directory.",
                path=relative,
            )
        current = candidate
    return tuple(created)


def _read_regular_file(path: Path, relative_path: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Unable to open regular workspace file without following symlinks.",
            path=relative_path,
        ) from exc
    result: tuple[bytes, os.stat_result] | None = None
    primary_error: WorkspaceRuntimeError | None = None
    primary_cause: OSError | None = None
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise WorkspaceRuntimeError(
                WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
                "Opened workspace entry is not a regular file.",
                path=relative_path,
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        result = b"".join(chunks), opened_stat
    except WorkspaceRuntimeError as exc:
        primary_error = exc
    except OSError as exc:
        primary_error = WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Unable to read regular workspace file.",
            path=relative_path,
        )
        primary_cause = exc
    close_error: OSError | None = None
    try:
        os.close(descriptor)
    except OSError as exc:
        close_error = exc
    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    if close_error is not None:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Unable to close regular workspace file after inspection.",
            path=relative_path,
        ) from close_error
    if result is None:
        raise AssertionError("successful regular-file read must produce a result")
    return result


def _lstat_created_directory(path: Path) -> os.stat_result:
    return path.lstat()


def _require_real_directory(path: Path, relative_path: str) -> None:
    try:
        directory_stat = path.lstat()
    except OSError as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE,
            "Workspace directory is unavailable.",
            path=relative_path or None,
        ) from exc
    if stat.S_ISLNK(directory_stat.st_mode):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.SYMLINK_DETECTED,
            "Symlink directory is forbidden in isolated workspace.",
            path=relative_path or None,
        )
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE,
            "Workspace path expected to be a directory.",
            path=relative_path or None,
        )


def _relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkspaceRuntimeError(
            WorkspaceRuntimeIssueCode.PATH_CONTAINMENT,
            "Filesystem entry escaped the isolated workspace root.",
        ) from exc
    return "" if relative == "." else relative


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino
