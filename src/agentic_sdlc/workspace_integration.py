"""Read-only governed workspace-session and repository-context application logic."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import uuid5

from agentic_sdlc.requirement_spec import LINEAGE_NAMESPACE
from agentic_sdlc.task_graph import Task

from agentic_sdlc.workspace_contracts import (
    WorkspaceContractError,
    WorkspaceSnapshot,
    normalize_repository_path,
    workspace_file_content_hash,
)
from agentic_sdlc.workspace_integration_contracts import (
    GovernedWorkspaceSession,
    RepositoryContext,
    RepositoryPathObservation,
    WorkspaceBinding,
    WorkspaceIntegrityStatus,
    build_repository_context,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    create_isolated_workspace,
    read_isolated_workspace_file,
    snapshot_isolated_workspace,
)


class WorkspaceIntegrationIssueCode(StrEnum):
    """Stable failure categories for governed read-only workspace integration."""

    WORKSPACE_ID = "WORKSPACE_ID"
    SNAPSHOT_ID = "SNAPSHOT_ID"
    INTEGRITY = "INTEGRITY"
    PATH_POLICY = "PATH_POLICY"
    DUPLICATE_PATH = "DUPLICATE_PATH"
    WORKSPACE_DRIFT = "WORKSPACE_DRIFT"
    NON_TEXT_FILE = "NON_TEXT_FILE"
    RUNTIME = "RUNTIME"


class WorkspaceIntegrationError(RuntimeError):
    """Bounded application failure without leaking filesystem capabilities."""

    def __init__(
        self,
        code: WorkspaceIntegrationIssueCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class RepositoryContextPathProvider(Protocol):
    """Application-owned bounded path selection for one task attempt."""

    def paths_for_attempt(
        self,
        task: Task,
        *,
        dependency_paths: tuple[str, ...],
        retry_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Return explicit repository paths; no filesystem discovery is allowed."""


class DeterministicRepositoryContextPathProvider:
    """Canonical union of configured, dependency, and retry-relevant paths."""

    def __init__(self, baseline_paths: tuple[str, ...] = ()) -> None:
        normalized = tuple(normalize_repository_path(path) for path in baseline_paths)
        if len(normalized) != len(set(normalized)):
            raise WorkspaceContractError(
                "Baseline repository context paths must be unique."
            )
        self._baseline_paths = tuple(sorted(normalized))

    def paths_for_attempt(
        self,
        task: Task,
        *,
        dependency_paths: tuple[str, ...],
        retry_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        del task
        return tuple(
            sorted(
                {
                    *self._baseline_paths,
                    *(normalize_repository_path(path) for path in dependency_paths),
                    *(normalize_repository_path(path) for path in retry_paths),
                }
            )
        )


class GovernedWorkspaceRuntime:
    """Per-workflow owner of live capabilities kept outside checkpointed state."""

    def __init__(self, *, parent_directory: Path | None = None) -> None:
        self._parent_directory = parent_directory
        self._workspaces: dict[str, IsolatedWorkspace] = {}
        self._lock = Lock()

    def bind_workspace(self, run_id: str, workspace: IsolatedWorkspace) -> None:
        """Bind one caller-created capability to one run before execution."""

        if not run_id:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_ID,
                "Governed workspace run ID must be non-empty.",
            )
        snapshot = _snapshot_for_context(workspace)
        if snapshot.workspace_id != workspace.workspace_id:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_ID,
                "Supplied workspace capability identity is inconsistent.",
            )
        with self._lock:
            existing = self._workspaces.get(run_id)
            if existing is not None and existing != workspace:
                raise WorkspaceIntegrationError(
                    WorkspaceIntegrationIssueCode.WORKSPACE_ID,
                    "Governed run already owns a different isolated workspace.",
                )
            self._workspaces[run_id] = workspace

    def establish_workspace_for_run(self, run_id: str) -> IsolatedWorkspace:
        """Create or return the run's sole capability before execution starts."""

        if not run_id:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_ID,
                "Governed workspace run ID must be non-empty.",
            )
        with self._lock:
            workspace = self._workspaces.get(run_id)
            if workspace is None:
                workspace_id = "WORKSPACE-" + str(
                    uuid5(LINEAGE_NAMESPACE, f"governed-workspace:{run_id}")
                )
                workspace = create_isolated_workspace(
                    workspace_id,
                    parent_directory=self._parent_directory,
                )
                self._workspaces[run_id] = workspace
            return workspace

    def workspace_for_run(self, run_id: str) -> IsolatedWorkspace:
        """Resolve an existing run capability without silently replacing it."""

        if not run_id:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_ID,
                "Governed workspace run ID must be non-empty.",
            )
        with self._lock:
            workspace = self._workspaces.get(run_id)
        if workspace is None:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.RUNTIME,
                "Governed run's isolated workspace capability is unavailable.",
            )
        return workspace


def advance_governed_workspace_session(
    session: GovernedWorkspaceSession,
    snapshot: WorkspaceSnapshot,
) -> GovernedWorkspaceSession:
    """Advance only the authoritative snapshot of a verified session."""

    if session.integrity_status is not WorkspaceIntegrityStatus.VERIFIED:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.INTEGRITY,
            "An unprovable workspace session cannot advance.",
        )
    if snapshot.workspace_id != session.workspace_id:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.WORKSPACE_ID,
            "Authoritative snapshot belongs to a different workspace.",
        )
    return session.model_copy(
        update={"authoritative_snapshot_id": snapshot.snapshot_id}
    )


def mark_workspace_integrity_unprovable(
    session: GovernedWorkspaceSession,
) -> GovernedWorkspaceSession:
    """Retain the last verified snapshot as history while freezing authority."""

    return session.model_copy(
        update={"integrity_status": WorkspaceIntegrityStatus.UNPROVABLE}
    )


def establish_governed_workspace_session(
    workspace: IsolatedWorkspace,
    *,
    run_id: str,
) -> tuple[GovernedWorkspaceSession, WorkspaceSnapshot]:
    """Bind one run to the factory-created workspace's real initial snapshot."""

    try:
        snapshot = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as exc:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.RUNTIME,
            "Unable to establish a governed workspace session.",
            path=exc.path,
        ) from exc
    session = GovernedWorkspaceSession(
        run_id=run_id,
        workspace_id=snapshot.workspace_id,
        baseline_snapshot_id=snapshot.snapshot_id,
        authoritative_snapshot_id=snapshot.snapshot_id,
        integrity_status=WorkspaceIntegrityStatus.VERIFIED,
    )
    return session, snapshot


def provide_repository_context(
    workspace: IsolatedWorkspace,
    session: GovernedWorkspaceSession,
    authoritative_snapshot: WorkspaceSnapshot,
    requested_paths: tuple[str, ...],
) -> RepositoryContext:
    """Read an explicit bounded text projection from one authoritative snapshot."""

    if session.integrity_status is not WorkspaceIntegrityStatus.VERIFIED:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.INTEGRITY,
            "Repository context requires verified workspace integrity.",
        )
    if workspace.workspace_id != session.workspace_id:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.WORKSPACE_ID,
            "Isolated workspace and governed session identities differ.",
        )
    if authoritative_snapshot.workspace_id != session.workspace_id:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.WORKSPACE_ID,
            "Authoritative snapshot belongs to a different workspace.",
        )
    if authoritative_snapshot.snapshot_id != session.authoritative_snapshot_id:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.SNAPSHOT_ID,
            "Supplied snapshot is not the session's authoritative snapshot.",
        )

    normalized_paths: list[str] = []
    for path in requested_paths:
        try:
            normalized_paths.append(normalize_repository_path(path))
        except (TypeError, WorkspaceContractError) as exc:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.PATH_POLICY,
                "Requested repository context path violates path policy.",
                path=str(path),
            ) from exc
    if len(normalized_paths) != len(set(normalized_paths)):
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.DUPLICATE_PATH,
            "Requested repository context paths must be unique.",
        )
    ordered_paths = tuple(sorted(normalized_paths))

    observed_before = _snapshot_for_context(workspace)
    if observed_before != authoritative_snapshot:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
            "Real workspace state differs from the authoritative snapshot.",
        )

    observations: list[RepositoryPathObservation] = []
    for path in ordered_paths:
        contents = _read_context_file(workspace, path)
        expected = authoritative_snapshot.file_state(path)
        if contents is None:
            if expected is not None:
                raise WorkspaceIntegrationError(
                    WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
                    "Expected repository context file is now absent.",
                    path=path,
                )
            observations.append(
                RepositoryPathObservation(
                    path=path,
                    exists=False,
                    content=None,
                    content_hash=None,
                )
            )
            continue
        if expected is None:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
                "Repository context path now exists outside the bound snapshot.",
                path=path,
            )
        try:
            text = contents.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.NON_TEXT_FILE,
                "Requested repository context file is not valid UTF-8 text.",
                path=path,
            ) from exc
        content_hash = workspace_file_content_hash(text)
        if content_hash != expected.content_hash:
            raise WorkspaceIntegrationError(
                WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
                "Repository context content differs from the bound snapshot.",
                path=path,
            )
        observations.append(
            RepositoryPathObservation(
                path=path,
                exists=True,
                content=text,
                content_hash=content_hash,
            )
        )

    observed_after = _snapshot_for_context(workspace)
    if observed_after != authoritative_snapshot:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.WORKSPACE_DRIFT,
            "Workspace drifted while repository context was being read.",
        )
    binding = WorkspaceBinding(
        workspace_id=session.workspace_id,
        snapshot_id=session.authoritative_snapshot_id,
    )
    return build_repository_context(binding, tuple(observations))


def _snapshot_for_context(workspace: IsolatedWorkspace) -> WorkspaceSnapshot:
    try:
        return snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as exc:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.RUNTIME,
            "Unable to verify real workspace state for repository context.",
            path=exc.path,
        ) from exc


def _read_context_file(workspace: IsolatedWorkspace, path: str) -> bytes | None:
    try:
        return read_isolated_workspace_file(workspace, path)
    except WorkspaceRuntimeError as exc:
        raise WorkspaceIntegrationError(
            WorkspaceIntegrationIssueCode.RUNTIME,
            "Unable to read requested repository context path.",
            path=path,
        ) from exc
