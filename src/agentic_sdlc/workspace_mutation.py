"""Process-level transactional mutation of factory-created isolated workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentic_sdlc.workspace_contracts import (
    WorkspaceChangeOperation,
    WorkspaceChangeSet,
    WorkspaceChangeSetValidationResult,
    WorkspaceContractError,
    WorkspaceFileChange,
    WorkspaceSnapshot,
    normalize_repository_path,
    validate_workspace_change_set_preimages,
    workspace_change_set_identity_is_valid,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    WorkspaceRuntimeIssueCode,
    _CreatedDirectory,
    _RuntimeFileState,
    _create_missing_parent_directories,
    _inspect_workspace_target,
    _read_regular_file,
    _validated_workspace_root,
    snapshot_isolated_workspace,
)


class WorkspaceMutationStatus(StrEnum):
    """Terminal process-level outcome for one workspace change set."""

    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class WorkspaceMutationIssueCode(StrEnum):
    """Stable machine-readable runtime mutation failure categories."""

    VALIDATION_EVIDENCE = "VALIDATION_EVIDENCE"
    INVALID_WORKSPACE_CAPABILITY = "INVALID_WORKSPACE_CAPABILITY"
    WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
    RUNTIME_PATH_POLICY = "RUNTIME_PATH_POLICY"
    PATH_CONTAINMENT = "PATH_CONTAINMENT"
    SYMLINK_DETECTED = "SYMLINK_DETECTED"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    STALE_PRECONDITION = "STALE_PRECONDITION"
    CREATE_FAILURE = "CREATE_FAILURE"
    MODIFY_FAILURE = "MODIFY_FAILURE"
    POSTIMAGE_MISMATCH = "POSTIMAGE_MISMATCH"
    ROLLBACK_FAILURE = "ROLLBACK_FAILURE"
    ROLLBACK_VERIFICATION_FAILURE = "ROLLBACK_VERIFICATION_FAILURE"


class WorkspaceMutationIssue(BaseModel):
    """One deterministic runtime mutation or rollback issue."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: WorkspaceMutationIssueCode
    path: str | None
    detail: str


class WorkspaceFileMutationEvidence(BaseModel):
    """Canonical audit evidence for one desired file operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    operation: WorkspaceChangeOperation
    expected_preimage_hash: str | None
    observed_preimage_hash: str | None
    desired_postimage_hash: str
    observed_postimage_hash: str | None
    write_performed: bool
    created_parent_paths: tuple[str, ...]
    rollback_attempted: bool
    rollback_verified: bool


class WorkspaceMutationResult(BaseModel):
    """Immutable application-owned evidence for one mutation transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mutation_id: str
    workspace_id: str
    change_set_id: str
    base_snapshot_id: str
    task_id: str
    request_id: str
    attempt_id: str
    pre_mutation_snapshot_id: str | None
    post_mutation_snapshot_id: str | None
    rollback_snapshot_id: str | None
    status: WorkspaceMutationStatus
    file_evidence: tuple[WorkspaceFileMutationEvidence, ...]
    issues: tuple[WorkspaceMutationIssue, ...]


@dataclass(slots=True)
class _EvidenceState:
    change: WorkspaceFileChange
    observed_preimage_hash: str | None = None
    observed_postimage_hash: str | None = None
    write_performed: bool = False
    created_parent_paths: tuple[str, ...] = ()
    rollback_attempted: bool = False
    rollback_verified: bool = False


@dataclass(slots=True)
class _StagingFileRecord:
    logical_path: str
    absolute_path: Path
    device: int | None = None
    inode: int | None = None
    content_hash: str | None = None
    mode: int | None = None
    cleanup_attempted: bool = False
    cleanup_verified: bool = False


@dataclass(slots=True)
class _AppliedRecord:
    change: WorkspaceFileChange
    created_directories: tuple[_CreatedDirectory, ...] = ()
    target_created: bool = False
    written_device: int | None = None
    written_inode: int | None = None
    prior_contents: bytes | None = None
    prior_mode: int | None = None
    observed_postimage_hash: str | None = None
    rollback_ownership_hash: str | None = None
    rollback_ownership_mode: int | None = None
    staging_file: _StagingFileRecord | None = None

    @property
    def has_effects(self) -> bool:
        return bool(
            self.created_directories
            or self.target_created
            or self.written_device is not None
            or (
                self.staging_file is not None
                and not self.staging_file.cleanup_verified
            )
        )


class _MutationFailure(RuntimeError):
    def __init__(
        self,
        code: WorkspaceMutationIssueCode,
        detail: str,
        *,
        path: str | None = None,
        record: _AppliedRecord | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.path = path
        self.record = record


class _AtomicReplaceFailure(OSError):
    def __init__(self, staging_file: _StagingFileRecord | None) -> None:
        super().__init__("Atomic replacement staging failed.")
        self.staging_file = staging_file


def apply_workspace_change_set(
    workspace: IsolatedWorkspace,
    change_set: WorkspaceChangeSet,
    validation: WorkspaceChangeSetValidationResult,
) -> WorkspaceMutationResult:
    """Validate, apply, verify, and if necessary roll back one change set."""

    evidence = _initial_evidence(change_set)
    issues = _validation_evidence_issues(change_set, validation)
    if issues:
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            issues,
        )

    try:
        _validated_workspace_root(workspace)
    except WorkspaceRuntimeError as exc:
        return _rejected_runtime_result(change_set, evidence, exc)
    if workspace.workspace_id != change_set.workspace_id:
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            (
                _issue(
                    WorkspaceMutationIssueCode.VALIDATION_EVIDENCE,
                    None,
                    "Isolated workspace ID does not match the change set.",
                ),
            ),
        )

    invalid_operation = next(
        (
            change
            for change in change_set.file_changes
            if not isinstance(change.operation, WorkspaceChangeOperation)
        ),
        None,
    )
    if invalid_operation is not None:
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            (
                _issue(
                    WorkspaceMutationIssueCode.UNSUPPORTED_OPERATION,
                    invalid_operation.path,
                    "Workspace change operation is unsupported.",
                ),
            ),
        )

    try:
        pre_snapshot = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as exc:
        return _rejected_runtime_result(change_set, evidence, exc)

    try:
        for change in _ordered_changes(change_set):
            normalize_repository_path(change.path)
            file_state = pre_snapshot.file_state(change.path)
            evidence[change.path].observed_preimage_hash = (
                file_state.content_hash if file_state is not None else None
            )
    except (TypeError, WorkspaceContractError) as exc:
        issue = _issue(
            WorkspaceMutationIssueCode.RUNTIME_PATH_POLICY,
            getattr(change, "path", None),
            "Workspace destination violates runtime path policy.",
        )
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            (issue,),
            pre_snapshot_id=pre_snapshot.snapshot_id,
        )

    preimage_validation = validate_workspace_change_set_preimages(
        change_set, pre_snapshot
    )
    if not preimage_validation.passed:
        stale_issues = tuple(
            _issue(
                WorkspaceMutationIssueCode.STALE_PRECONDITION,
                item.path,
                item.detail,
            )
            for item in preimage_validation.issues
        )
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            stale_issues,
            pre_snapshot_id=pre_snapshot.snapshot_id,
        )

    try:
        _preflight_all(workspace, change_set)
    except WorkspaceRuntimeError as exc:
        return _rejected_runtime_result(
            change_set,
            evidence,
            exc,
            pre_snapshot_id=pre_snapshot.snapshot_id,
        )
    except _MutationFailure as exc:
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            (_issue(exc.code, exc.path, exc.detail),),
            pre_snapshot_id=pre_snapshot.snapshot_id,
        )

    applied: list[_AppliedRecord] = []
    original_issue: WorkspaceMutationIssue | None = None
    try:
        for change in _ordered_changes(change_set):
            current_evidence = evidence[change.path]
            if change.operation is WorkspaceChangeOperation.CREATE:
                try:
                    record = _apply_create(workspace, change)
                except _MutationFailure as exc:
                    if exc.record is not None and exc.record.has_effects:
                        applied.append(exc.record)
                        _record_effects(current_evidence, exc.record)
                    raise
                applied.append(record)
                _record_effects(current_evidence, record)
            elif change.operation is WorkspaceChangeOperation.MODIFY:
                record = _apply_modify(workspace, change)
                applied.append(record)
                _record_effects(current_evidence, record)
            else:
                state = _inspect_workspace_target(workspace, change.path)
                if (
                    not state.exists
                    or state.content_hash != change.expected_preimage_hash
                    or state.content_hash != change.desired_content_hash
                ):
                    raise _MutationFailure(
                        WorkspaceMutationIssueCode.STALE_PRECONDITION,
                        "NO_CHANGE target no longer matches its desired preimage.",
                        path=change.path,
                    )
                current_evidence.observed_postimage_hash = state.content_hash

        post_snapshot = snapshot_isolated_workspace(workspace)
        _verify_postimages(change_set, post_snapshot, evidence)
    except WorkspaceRuntimeError as exc:
        original_issue = _runtime_issue(exc)
    except _MutationFailure as exc:
        original_issue = _issue(exc.code, exc.path, exc.detail)
        if exc.record is not None and exc.record.has_effects:
            if exc.record not in applied:
                applied.append(exc.record)
                _record_effects(evidence[exc.record.change.path], exc.record)
    except OSError:
        original_issue = _issue(
            WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE,
            None,
            "Unexpected filesystem failure during workspace mutation.",
        )
    else:
        return _result(
            change_set,
            WorkspaceMutationStatus.APPLIED,
            evidence,
            (),
            pre_snapshot_id=pre_snapshot.snapshot_id,
            post_snapshot_id=post_snapshot.snapshot_id,
        )

    if not any(record.has_effects for record in applied):
        return _result(
            change_set,
            WorkspaceMutationStatus.REJECTED,
            evidence,
            (original_issue,),
            pre_snapshot_id=pre_snapshot.snapshot_id,
        )

    rollback_issues = _rollback(workspace, applied, evidence)
    rollback_snapshot_id: str | None = None
    try:
        rollback_snapshot_id = snapshot_isolated_workspace(workspace).snapshot_id
    except WorkspaceRuntimeError:
        pass
    status = (
        WorkspaceMutationStatus.ROLLED_BACK
        if not rollback_issues
        else WorkspaceMutationStatus.ROLLBACK_FAILED
    )
    return _result(
        change_set,
        status,
        evidence,
        (original_issue, *rollback_issues),
        pre_snapshot_id=pre_snapshot.snapshot_id,
        rollback_snapshot_id=rollback_snapshot_id,
    )


def _validation_evidence_issues(
    change_set: WorkspaceChangeSet,
    validation: WorkspaceChangeSetValidationResult,
) -> tuple[WorkspaceMutationIssue, ...]:
    failures: list[str] = []
    if not validation.passed:
        failures.append("validation did not pass")
    if validation.issues:
        failures.append("validation contains issues")
    if validation.change_set_id != change_set.change_set_id:
        failures.append("change-set identity differs")
    if validation.workspace_id != change_set.workspace_id:
        failures.append("workspace identity differs")
    if validation.snapshot_id != change_set.base_snapshot_id:
        failures.append("base snapshot identity differs")
    if not workspace_change_set_identity_is_valid(change_set):
        failures.append("current change-set contents do not match canonical identity")
    if not failures:
        return ()
    return (
        _issue(
            WorkspaceMutationIssueCode.VALIDATION_EVIDENCE,
            None,
            "Mutation validation evidence rejected: " + ", ".join(failures) + ".",
        ),
    )


def _preflight_all(
    workspace: IsolatedWorkspace,
    change_set: WorkspaceChangeSet,
) -> None:
    for change in _ordered_changes(change_set):
        state = _inspect_workspace_target(workspace, change.path)
        if change.operation is WorkspaceChangeOperation.CREATE:
            if state.exists:
                raise _MutationFailure(
                    WorkspaceMutationIssueCode.STALE_PRECONDITION,
                    "CREATE destination already exists.",
                    path=change.path,
                )
        elif change.operation in {
            WorkspaceChangeOperation.MODIFY,
            WorkspaceChangeOperation.NO_CHANGE,
        }:
            if not state.exists:
                raise _MutationFailure(
                    WorkspaceMutationIssueCode.STALE_PRECONDITION,
                    "Existing workspace target is absent.",
                    path=change.path,
                )
            if state.content_hash != change.expected_preimage_hash:
                raise _MutationFailure(
                    WorkspaceMutationIssueCode.STALE_PRECONDITION,
                    "Workspace target no longer matches its expected preimage.",
                    path=change.path,
                )


def _apply_create(
    workspace: IsolatedWorkspace,
    change: WorkspaceFileChange,
) -> _AppliedRecord:
    before = _inspect_workspace_target(workspace, change.path)
    if before.exists:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.STALE_PRECONDITION,
            "CREATE destination appeared after transaction preflight.",
            path=change.path,
        )
    created_accumulator: list[_CreatedDirectory] = []
    try:
        created_directories = _create_missing_parent_directories(
            workspace,
            change.path,
            _created=created_accumulator,
        )
    except WorkspaceRuntimeError as exc:
        record = _AppliedRecord(
            change=change,
            created_directories=tuple(created_accumulator),
        )
        raise _MutationFailure(
            _runtime_mutation_code(exc.code),
            str(exc),
            path=exc.path or change.path,
            record=record,
        ) from exc
    record = _AppliedRecord(
        change=change,
        created_directories=created_directories,
    )
    try:
        after_parents = _inspect_workspace_target(workspace, change.path)
    except WorkspaceRuntimeError as exc:
        raise _MutationFailure(
            _runtime_mutation_code(exc.code),
            str(exc),
            path=exc.path or change.path,
            record=record,
        ) from exc
    except OSError as exc:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE,
            "CREATE parent-state inspection failed.",
            path=change.path,
            record=record,
        ) from exc
    if after_parents.exists:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.STALE_PRECONDITION,
            "CREATE destination appeared before exclusive creation.",
            path=change.path,
            record=record,
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    primary_error: OSError | None = None
    try:
        descriptor = os.open(after_parents.absolute_path, flags, 0o600)
        record.target_created = True
        created_stat = os.fstat(descriptor)
        record.written_device = created_stat.st_dev
        record.written_inode = created_stat.st_ino
        _write_all(descriptor, change.desired_content.encode("utf-8"))
        os.fsync(descriptor)
    except OSError as exc:
        primary_error = exc
    close_error: OSError | None = None
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
    if primary_error is not None or close_error is not None:
        if record.target_created:
            _capture_create_rollback_ownership(workspace, record)
        raise _MutationFailure(
            WorkspaceMutationIssueCode.CREATE_FAILURE,
            "Exclusive CREATE operation failed.",
            path=change.path,
            record=record,
        ) from (primary_error if primary_error is not None else close_error)
    try:
        observed = _inspect_workspace_target(workspace, change.path)
    except WorkspaceRuntimeError as exc:
        raise _MutationFailure(
            _runtime_mutation_code(exc.code),
            str(exc),
            path=exc.path or change.path,
            record=record,
        ) from exc
    except OSError as exc:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE,
            "CREATE post-write inspection failed.",
            path=change.path,
            record=record,
        ) from exc
    if (
        not observed.exists
        or (observed.device, observed.inode)
        != (record.written_device, record.written_inode)
        or observed.content_hash != change.desired_content_hash
    ):
        raise _MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "CREATE postimage could not be verified.",
            path=change.path,
            record=record,
        )
    record.observed_postimage_hash = observed.content_hash
    record.rollback_ownership_hash = observed.content_hash
    record.rollback_ownership_mode = observed.mode
    return record


def _apply_modify(
    workspace: IsolatedWorkspace,
    change: WorkspaceFileChange,
) -> _AppliedRecord:
    before = _inspect_workspace_target(workspace, change.path)
    if not before.exists or before.content_hash != change.expected_preimage_hash:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.STALE_PRECONDITION,
            "MODIFY target changed after transaction preflight.",
            path=change.path,
        )
    prior_contents, prior_stat = _read_regular_file(
        before.absolute_path, change.path
    )
    if (
        hashlib.sha256(prior_contents).hexdigest() != change.expected_preimage_hash
        or (prior_stat.st_dev, prior_stat.st_ino) != (before.device, before.inode)
    ):
        raise _MutationFailure(
            WorkspaceMutationIssueCode.STALE_PRECONDITION,
            "MODIFY preimage changed immediately before replacement.",
            path=change.path,
        )
    record = _AppliedRecord(
        change=change,
        prior_contents=prior_contents,
        prior_mode=stat.S_IMODE(prior_stat.st_mode),
    )
    try:
        written_device, written_inode = _atomic_replace_file(
            before.absolute_path,
            change.desired_content.encode("utf-8"),
            record.prior_mode,
            change.path,
        )
    except _AtomicReplaceFailure as exc:
        record.staging_file = exc.staging_file
        raise _MutationFailure(
            WorkspaceMutationIssueCode.MODIFY_FAILURE,
            "Atomic MODIFY operation failed.",
            path=change.path,
            record=record if exc.staging_file is not None else None,
        ) from exc
    except OSError as exc:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.MODIFY_FAILURE,
            "Atomic MODIFY operation failed.",
            path=change.path,
        ) from exc
    record.written_device = written_device
    record.written_inode = written_inode
    try:
        observed = _inspect_workspace_target(workspace, change.path)
    except WorkspaceRuntimeError as exc:
        raise _MutationFailure(
            _runtime_mutation_code(exc.code),
            str(exc),
            path=exc.path or change.path,
            record=record,
        ) from exc
    except OSError as exc:
        raise _MutationFailure(
            WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE,
            "MODIFY post-replacement inspection failed.",
            path=change.path,
            record=record,
        ) from exc
    if (
        not observed.exists
        or (observed.device, observed.inode) != (written_device, written_inode)
        or observed.content_hash != change.desired_content_hash
        or observed.mode != record.prior_mode
    ):
        raise _MutationFailure(
            WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
            "MODIFY postimage or preserved mode could not be verified.",
            path=change.path,
            record=record,
        )
    record.observed_postimage_hash = observed.content_hash
    return record


def _verify_postimages(
    change_set: WorkspaceChangeSet,
    post_snapshot: WorkspaceSnapshot,
    evidence: dict[str, _EvidenceState],
) -> None:
    for change in _ordered_changes(change_set):
        state = post_snapshot.file_state(change.path)
        observed_hash = state.content_hash if state is not None else None
        evidence[change.path].observed_postimage_hash = observed_hash
        if observed_hash != change.desired_content_hash:
            raise _MutationFailure(
                WorkspaceMutationIssueCode.POSTIMAGE_MISMATCH,
                "Verified workspace postimage differs from desired contents.",
                path=change.path,
            )


def _rollback(
    workspace: IsolatedWorkspace,
    records: list[_AppliedRecord],
    evidence: dict[str, _EvidenceState],
) -> tuple[WorkspaceMutationIssue, ...]:
    issues: list[WorkspaceMutationIssue] = []
    for record in reversed(records):
        current_evidence = evidence[record.change.path]
        current_evidence.rollback_attempted = True
        if record.change.operation is WorkspaceChangeOperation.CREATE:
            if record.target_created:
                try:
                    _rollback_created_file(workspace, record)
                except (OSError, WorkspaceRuntimeError):
                    issues.append(
                        _issue(
                            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
                            record.change.path,
                            "Transaction-created file could not be safely removed.",
                        )
                    )
        elif record.change.operation is WorkspaceChangeOperation.MODIFY:
            if (
                record.staging_file is not None
                and not record.staging_file.cleanup_verified
            ):
                if not _cleanup_staging_file(record.staging_file):
                    issues.append(
                        _issue(
                            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
                            record.change.path,
                            "MODIFY staging file could not be safely removed.",
                        )
                    )
            if record.written_device is not None:
                try:
                    _rollback_modified_file(workspace, record)
                except (OSError, WorkspaceRuntimeError):
                    issues.append(
                        _issue(
                            WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
                            record.change.path,
                            "Modified file could not be safely restored.",
                        )
                    )

    directories = sorted(
        {
            (item.path, item.absolute_path, item.device, item.inode): item
            for record in records
            for item in record.created_directories
        }.values(),
        key=lambda item: (-item.path.count("/"), item.path),
    )
    for directory in directories:
        try:
            _rollback_created_directory(directory)
        except OSError:
            issues.append(
                _issue(
                    WorkspaceMutationIssueCode.ROLLBACK_FAILURE,
                    directory.path,
                    "Transaction-created directory could not be safely removed.",
                )
            )

    for record in records:
        try:
            verified = _verify_rollback_record(workspace, record)
        except WorkspaceRuntimeError:
            verified = False
        evidence[record.change.path].rollback_verified = verified
        if not verified:
            issues.append(
                _issue(
                    WorkspaceMutationIssueCode.ROLLBACK_VERIFICATION_FAILURE,
                    record.change.path,
                    "Transaction-owned effects were not fully restored.",
                )
            )
    return _canonical_issues(issues)


def _rollback_created_file(
    workspace: IsolatedWorkspace,
    record: _AppliedRecord,
) -> None:
    current = _inspect_workspace_target(workspace, record.change.path)
    if not current.exists:
        return
    if (
        (current.device, current.inode)
        != (record.written_device, record.written_inode)
        or record.rollback_ownership_hash is None
        or current.content_hash != record.rollback_ownership_hash
        or record.rollback_ownership_mode is None
        or current.mode != record.rollback_ownership_mode
    ):
        raise OSError("transaction-created file ownership state changed")
    _remove_created_file(current.absolute_path)


def _rollback_modified_file(
    workspace: IsolatedWorkspace,
    record: _AppliedRecord,
) -> None:
    current = _inspect_workspace_target(workspace, record.change.path)
    if (
        not current.exists
        or (current.device, current.inode)
        != (record.written_device, record.written_inode)
        or current.content_hash != record.change.desired_content_hash
        or current.mode != record.prior_mode
        or record.prior_contents is None
        or record.prior_mode is None
    ):
        raise OSError(
            "transaction-modified file identity, contents, or mode changed"
        )
    try:
        _atomic_replace_file(
            current.absolute_path,
            record.prior_contents,
            record.prior_mode,
            record.change.path,
        )
    except _AtomicReplaceFailure as exc:
        if exc.staging_file is not None:
            record.staging_file = exc.staging_file
        raise


def _rollback_created_directory(directory: _CreatedDirectory) -> None:
    try:
        directory_stat = directory.absolute_path.lstat()
    except FileNotFoundError:
        return
    if (
        directory.device is None
        or directory.inode is None
        or stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or (directory_stat.st_dev, directory_stat.st_ino)
        != (directory.device, directory.inode)
    ):
        raise OSError("transaction-created directory identity changed")
    _remove_created_directory(directory.absolute_path)


def _verify_rollback_record(
    workspace: IsolatedWorkspace,
    record: _AppliedRecord,
) -> bool:
    if record.change.operation is WorkspaceChangeOperation.CREATE:
        try:
            target = _validated_workspace_root(workspace).joinpath(
                *record.change.path.split("/")
            )
            target.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        else:
            return False
        for directory in record.created_directories:
            try:
                directory.absolute_path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            return False
        return True
    if record.staging_file is not None and not _verify_staging_file_absent(
        record.staging_file
    ):
        return False
    current = _inspect_workspace_target(workspace, record.change.path)
    return bool(
        current.exists
        and current.content_hash == record.change.expected_preimage_hash
        and current.mode == record.prior_mode
    )


def _atomic_replace_file(
    target: Path,
    contents: bytes,
    mode: int,
    logical_path: str,
) -> tuple[int, int]:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".agentic-sdlc-mutation-",
        dir=target.parent,
    )
    staging = _StagingFileRecord(
        logical_path=logical_path,
        absolute_path=Path(temporary_name),
    )
    failure: OSError | None = None
    try:
        initial_stat = os.fstat(descriptor)
        staging.device = initial_stat.st_dev
        staging.inode = initial_stat.st_ino
        os.fchmod(descriptor, mode)
        _write_all(descriptor, contents)
        os.fsync(descriptor)
    except OSError as exc:
        failure = exc

    try:
        os.close(descriptor)
    except OSError as exc:
        if failure is None:
            failure = exc

    if failure is None and (staging.device is None or staging.inode is None):
        failure = OSError("Staging file identity was unavailable.")
    if failure is None:
        try:
            os.replace(staging.absolute_path, target)
        except OSError as exc:
            failure = exc
        else:
            assert staging.device is not None
            assert staging.inode is not None
            return staging.device, staging.inode

    _capture_staging_file_ownership(staging)
    _cleanup_staging_file(staging)
    raise _AtomicReplaceFailure(
        staging if not staging.cleanup_verified else None
    ) from failure


def _capture_staging_file_ownership(staging: _StagingFileRecord) -> None:
    try:
        entry_stat = staging.absolute_path.lstat()
    except OSError:
        return
    if (
        stat.S_ISLNK(entry_stat.st_mode)
        or not stat.S_ISREG(entry_stat.st_mode)
        or (entry_stat.st_dev, entry_stat.st_ino)
        != (staging.device, staging.inode)
    ):
        return
    try:
        contents, opened_stat = _read_regular_file(
            staging.absolute_path, staging.logical_path
        )
    except (OSError, WorkspaceRuntimeError):
        return
    if (opened_stat.st_dev, opened_stat.st_ino) != (
        staging.device,
        staging.inode,
    ):
        return
    staging.content_hash = hashlib.sha256(contents).hexdigest()
    staging.mode = stat.S_IMODE(opened_stat.st_mode)


def _cleanup_staging_file(staging: _StagingFileRecord) -> bool:
    staging.cleanup_attempted = True
    try:
        staging.absolute_path.lstat()
    except FileNotFoundError:
        staging.cleanup_verified = True
        return True
    except OSError:
        return False
    if not _staging_file_matches_ownership(staging):
        return False
    try:
        _remove_staging_file(staging.absolute_path)
    except OSError:
        pass
    return _verify_staging_file_absent(staging)


def _staging_file_matches_ownership(staging: _StagingFileRecord) -> bool:
    if (
        staging.device is None
        or staging.inode is None
        or staging.content_hash is None
        or staging.mode is None
    ):
        return False
    try:
        entry_stat = staging.absolute_path.lstat()
        contents, opened_stat = _read_regular_file(
            staging.absolute_path, staging.logical_path
        )
    except (OSError, WorkspaceRuntimeError):
        return False
    return bool(
        not stat.S_ISLNK(entry_stat.st_mode)
        and stat.S_ISREG(entry_stat.st_mode)
        and (entry_stat.st_dev, entry_stat.st_ino)
        == (staging.device, staging.inode)
        and (opened_stat.st_dev, opened_stat.st_ino)
        == (staging.device, staging.inode)
        and hashlib.sha256(contents).hexdigest() == staging.content_hash
        and stat.S_IMODE(opened_stat.st_mode) == staging.mode
    )


def _verify_staging_file_absent(staging: _StagingFileRecord) -> bool:
    try:
        staging.absolute_path.lstat()
    except FileNotFoundError:
        staging.cleanup_verified = True
        return True
    except OSError:
        return False
    return False


def _capture_create_rollback_ownership(
    workspace: IsolatedWorkspace,
    record: _AppliedRecord,
) -> None:
    """Capture the actual partial CREATE state only while identity still matches."""

    if (
        not record.target_created
        or record.written_device is None
        or record.written_inode is None
    ):
        return
    try:
        current = _inspect_workspace_target(workspace, record.change.path)
    except (OSError, WorkspaceRuntimeError):
        return
    if (
        current.exists
        and (current.device, current.inode)
        == (record.written_device, record.written_inode)
    ):
        record.rollback_ownership_hash = current.content_hash
        record.rollback_ownership_mode = current.mode


def _write_all(descriptor: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("filesystem write made no progress")
        view = view[written:]


def _remove_created_file(path: Path) -> None:
    os.unlink(path)


def _remove_staging_file(path: Path) -> None:
    os.unlink(path)


def _remove_created_directory(path: Path) -> None:
    os.rmdir(path)


def _record_effects(evidence: _EvidenceState, record: _AppliedRecord) -> None:
    evidence.write_performed = (
        record.target_created
        or record.written_device is not None
        or record.staging_file is not None
        or bool(record.created_directories)
    )
    evidence.created_parent_paths = tuple(
        item.path for item in record.created_directories
    )
    evidence.observed_postimage_hash = record.observed_postimage_hash


def _initial_evidence(
    change_set: WorkspaceChangeSet,
) -> dict[str, _EvidenceState]:
    return {
        change.path: _EvidenceState(change=change)
        for change in _ordered_changes(change_set)
        if isinstance(change.operation, WorkspaceChangeOperation)
    }


def _ordered_changes(
    change_set: WorkspaceChangeSet,
) -> tuple[WorkspaceFileChange, ...]:
    return tuple(
        sorted(change_set.file_changes, key=lambda item: (item.path, item.artifact_id))
    )


def _rejected_runtime_result(
    change_set: WorkspaceChangeSet,
    evidence: dict[str, _EvidenceState],
    error: WorkspaceRuntimeError,
    *,
    pre_snapshot_id: str | None = None,
) -> WorkspaceMutationResult:
    return _result(
        change_set,
        WorkspaceMutationStatus.REJECTED,
        evidence,
        (_runtime_issue(error),),
        pre_snapshot_id=pre_snapshot_id,
    )


def _runtime_issue(error: WorkspaceRuntimeError) -> WorkspaceMutationIssue:
    return _issue(_runtime_mutation_code(error.code), error.path, str(error))


def _runtime_mutation_code(
    code: WorkspaceRuntimeIssueCode,
) -> WorkspaceMutationIssueCode:
    mapping = {
        WorkspaceRuntimeIssueCode.INVALID_CAPABILITY: (
            WorkspaceMutationIssueCode.INVALID_WORKSPACE_CAPABILITY
        ),
        WorkspaceRuntimeIssueCode.WORKSPACE_UNAVAILABLE: (
            WorkspaceMutationIssueCode.WORKSPACE_UNAVAILABLE
        ),
        WorkspaceRuntimeIssueCode.PATH_POLICY: (
            WorkspaceMutationIssueCode.RUNTIME_PATH_POLICY
        ),
        WorkspaceRuntimeIssueCode.PATH_CONTAINMENT: (
            WorkspaceMutationIssueCode.PATH_CONTAINMENT
        ),
        WorkspaceRuntimeIssueCode.SYMLINK_DETECTED: (
            WorkspaceMutationIssueCode.SYMLINK_DETECTED
        ),
        WorkspaceRuntimeIssueCode.UNSUPPORTED_FILE_TYPE: (
            WorkspaceMutationIssueCode.UNSUPPORTED_FILE_TYPE
        ),
    }
    return mapping[code]


def _issue(
    code: WorkspaceMutationIssueCode,
    path: str | None,
    detail: str,
) -> WorkspaceMutationIssue:
    return WorkspaceMutationIssue(code=code, path=path, detail=detail)


def _canonical_issues(
    issues: list[WorkspaceMutationIssue] | tuple[WorkspaceMutationIssue, ...],
) -> tuple[WorkspaceMutationIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda item: (item.path or "", item.code.value, item.detail),
        )
    )


def _result(
    change_set: WorkspaceChangeSet,
    status: WorkspaceMutationStatus,
    evidence: dict[str, _EvidenceState],
    issues: tuple[WorkspaceMutationIssue, ...],
    *,
    pre_snapshot_id: str | None = None,
    post_snapshot_id: str | None = None,
    rollback_snapshot_id: str | None = None,
) -> WorkspaceMutationResult:
    file_evidence = tuple(
        WorkspaceFileMutationEvidence(
            path=item.change.path,
            operation=item.change.operation,
            expected_preimage_hash=item.change.expected_preimage_hash,
            observed_preimage_hash=item.observed_preimage_hash,
            desired_postimage_hash=item.change.desired_content_hash,
            observed_postimage_hash=item.observed_postimage_hash,
            write_performed=item.write_performed,
            created_parent_paths=item.created_parent_paths,
            rollback_attempted=item.rollback_attempted,
            rollback_verified=item.rollback_verified,
        )
        for item in sorted(evidence.values(), key=lambda value: value.change.path)
    )
    canonical_issues = _canonical_issues(issues)
    payload = {
        "workspace_id": change_set.workspace_id,
        "change_set_id": change_set.change_set_id,
        "base_snapshot_id": change_set.base_snapshot_id,
        "task_id": change_set.task_id,
        "request_id": change_set.request_id,
        "attempt_id": change_set.attempt_id,
        "pre_mutation_snapshot_id": pre_snapshot_id,
        "post_mutation_snapshot_id": post_snapshot_id,
        "rollback_snapshot_id": rollback_snapshot_id,
        "status": status,
        "file_evidence": file_evidence,
        "issues": canonical_issues,
    }
    serializable = {
        key: (
            [item.model_dump(mode="json") for item in value]
            if key in {"file_evidence", "issues"}
            else value
        )
        for key, value in payload.items()
    }
    digest = hashlib.sha256(
        json.dumps(
            serializable,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return WorkspaceMutationResult(
        mutation_id=f"WORKSPACE-MUTATION-{digest[:12].upper()}",
        **payload,
    )
