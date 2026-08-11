"""Deterministic contracts for proposed target-workspace desired state.

This module is deliberately free of filesystem I/O.  It interprets validated
artifact materialization intents against an explicitly supplied logical snapshot;
the mutator separately enforces real-filesystem containment (including symlink
containment) immediately before applying any change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentic_sdlc.project_delivery import ProjectDeliverableRole
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    EngineeringArtifactType,
    TaskExecutionResult,
    TaskExecutionValidationResult,
)
from agentic_sdlc.task_graph import Task, TaskMaterializationPolicy


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_PROTECTED_DIRECTORY_NAMES = frozenset({".git", ".venv", "venv"})


class WorkspaceContractError(ValueError):
    """Raised when authoritative workspace contracts cannot be constructed."""


class ArtifactMaterializationIntent(BaseModel):
    """Proposal that one artifact is desired content for one regular-file path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str = Field(min_length=1)
    target_path: str

    @field_validator("target_path")
    @classmethod
    def validate_target_path(cls, value: str) -> str:
        return normalize_repository_path(value)


class ArtifactMaterializationIssueCode(StrEnum):
    """Stable deterministic categories for materialization validation."""

    TASK_VALIDATION = "TASK_VALIDATION"
    ARTIFACT_SET = "ARTIFACT_SET"
    ARTIFACT_REFERENCE = "ARTIFACT_REFERENCE"
    DUPLICATE_ARTIFACT = "DUPLICATE_ARTIFACT"
    DUPLICATE_PATH = "DUPLICATE_PATH"
    PATH_POLICY = "PATH_POLICY"
    LINEAGE = "LINEAGE"
    POLICY = "POLICY"
    DELIVERABLE_ROLE = "DELIVERABLE_ROLE"


class ArtifactMaterializationValidationIssue(BaseModel):
    """One machine-readable materialization-proposal validation issue."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ArtifactMaterializationIssueCode
    artifact_id: str | None
    path: str | None
    detail: str


class ArtifactMaterializationValidationResult(BaseModel):
    """Application judgment bound to exact artifacts, intents, and task policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    materialization_validation_id: str
    task_id: str
    request_id: str
    attempt_id: str
    policy: TaskMaterializationPolicy
    artifact_ids: tuple[str, ...]
    artifact_evidence_digests: tuple[str, ...]
    intents: tuple[ArtifactMaterializationIntent, ...]
    passed: bool
    issues: tuple[ArtifactMaterializationValidationIssue, ...]


class WorkspaceChangeOperation(StrEnum):
    """Application-derived complete-file desired-state operation."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    NO_CHANGE = "NO_CHANGE"


class WorkspaceChangeSetIssueCode(StrEnum):
    """Stable machine-readable categories for change-set validation issues."""

    WORKSPACE_ID = "WORKSPACE_ID"
    SNAPSHOT_ID = "SNAPSHOT_ID"
    CHANGE_SET_ID = "CHANGE_SET_ID"
    LINEAGE = "LINEAGE"
    MATERIALIZATION_EVIDENCE = "MATERIALIZATION_EVIDENCE"
    ARTIFACT_REFERENCE = "ARTIFACT_REFERENCE"
    PROVENANCE = "PROVENANCE"
    PATH_POLICY = "PATH_POLICY"
    DUPLICATE_PATH = "DUPLICATE_PATH"
    ORDERING = "ORDERING"
    OPERATION = "OPERATION"
    PREIMAGE_HASH = "PREIMAGE_HASH"
    DESIRED_CONTENT = "DESIRED_CONTENT"
    DESIRED_CONTENT_HASH = "DESIRED_CONTENT_HASH"
    STALE_PREIMAGE = "STALE_PREIMAGE"


class WorkspaceFileState(BaseModel):
    """One canonical repository-relative file and its complete-content hash."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    content_hash: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalize_repository_path(value, enforce_protected=False)

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        return _validated_sha256(value, "workspace file content_hash")


class WorkspaceSnapshot(BaseModel):
    """Immutable, canonically ordered logical view of a target workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    snapshot_id: str
    workspace_id: str = Field(min_length=1)
    files: tuple[WorkspaceFileState, ...]

    def file_state(self, path: str) -> WorkspaceFileState | None:
        """Look up a file by canonical repository-relative path without I/O."""

        normalized = _normalize_repository_path(path, enforce_protected=False)
        return next((item for item in self.files if item.path == normalized), None)


class WorkspaceFileChange(BaseModel):
    """Application-derived complete desired state for one canonical file."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str
    artifact_lineage_id: str
    path: str
    operation: WorkspaceChangeOperation
    expected_preimage_hash: str | None
    desired_content_hash: str
    desired_content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_repository_path(value)

    @field_validator("expected_preimage_hash")
    @classmethod
    def validate_expected_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validated_sha256(value, "expected_preimage_hash")

    @field_validator("desired_content_hash")
    @classmethod
    def validate_desired_hash(cls, value: str) -> str:
        return _validated_sha256(value, "desired_content_hash")


class WorkspaceChangeSet(BaseModel):
    """Authoritative materialized desired state for one governed task attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    change_set_id: str
    workspace_id: str = Field(min_length=1)
    base_snapshot_id: str
    requirement_spec_id: str
    graph_id: str
    task_id: str
    request_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    materialization_validation_id: str
    materialized_artifact_ids: tuple[str, ...] = Field(min_length=1)
    file_changes: tuple[WorkspaceFileChange, ...] = Field(min_length=1)


class WorkspaceChangeSetValidationIssue(BaseModel):
    """One deterministic, machine-readable change-set validation issue."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: WorkspaceChangeSetIssueCode
    path: str | None
    detail: str


class WorkspaceChangeSetValidationResult(BaseModel):
    """Application judgment of change-set consistency against supplied state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    change_set_id: str
    workspace_id: str
    snapshot_id: str
    passed: bool
    issues: tuple[WorkspaceChangeSetValidationIssue, ...]


class WorkspaceChangeConflictParticipant(BaseModel):
    """One independently produced task attempt participating in a conflict."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    change_set_id: str
    task_id: str
    request_id: str
    attempt_id: str
    operation: WorkspaceChangeOperation
    desired_content_hash: str


class WorkspaceChangeConflict(BaseModel):
    """Deterministic evidence of independently proposed same-path changes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    participants: tuple[WorkspaceChangeConflictParticipant, ...] = Field(
        min_length=2
    )


class WorkspaceChangeSetConflictAnalysis(BaseModel):
    """Conflict judgment for task change sets sharing one base snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: str
    base_snapshot_id: str
    has_conflicts: bool
    conflicts: tuple[WorkspaceChangeConflict, ...]


def workspace_file_content_hash(content: str) -> str:
    """Hash complete desired file contents using the project's SHA-256 convention."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_repository_path(path: str) -> str:
    """Validate one conservative canonical repository-relative POSIX path.

    Dangerous or ambiguous input is rejected rather than rewritten.  This is a
    logical policy only; a future filesystem mutator must additionally prevent
    symlink and other runtime containment escapes.
    """

    return _normalize_repository_path(path, enforce_protected=True)


def build_workspace_snapshot(
    workspace_id: str,
    files: tuple[WorkspaceFileState, ...] = (),
) -> WorkspaceSnapshot:
    """Build a deterministic in-memory snapshot with canonical file ordering."""

    if not workspace_id:
        raise WorkspaceContractError("workspace_id must be non-empty.")
    ordered = tuple(sorted(files, key=lambda item: item.path))
    paths = tuple(item.path for item in ordered)
    if len(paths) != len(set(paths)):
        raise WorkspaceContractError("Workspace snapshot paths must be unique.")
    snapshot_id = _snapshot_id(workspace_id, ordered)
    return WorkspaceSnapshot(
        snapshot_id=snapshot_id,
        workspace_id=workspace_id,
        files=ordered,
    )


def workspace_snapshot_identity_is_valid(snapshot: WorkspaceSnapshot) -> bool:
    """Return whether a snapshot ID still binds its canonical file manifest."""

    return _snapshot_identity_is_valid(snapshot)


def validate_artifact_materialization(
    task: Task,
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    intents: tuple[ArtifactMaterializationIntent, ...] = (),
) -> ArtifactMaterializationValidationResult:
    """Validate exact artifact-to-path proposals against approved task policy."""

    issues: list[ArtifactMaterializationValidationIssue] = []
    if not validation.passed:
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.TASK_VALIDATION,
            "Task-execution validation did not pass.",
        )

    artifact_counts = Counter(artifact.artifact_id for artifact in artifacts)
    duplicate_artifacts = {
        artifact_id for artifact_id, count in artifact_counts.items() if count > 1
    }
    for artifact_id in sorted(duplicate_artifacts):
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.ARTIFACT_SET,
            f"Duplicate canonical artifact ID is ambiguous: {artifact_id}.",
            artifact_id=artifact_id,
        )
    supplied_ids = tuple(artifact.artifact_id for artifact in artifacts)
    if (
        len(validation.artifact_ids) != len(set(validation.artifact_ids))
        or supplied_ids != validation.artifact_ids
    ):
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.ARTIFACT_SET,
            "Canonical artifacts do not exactly match validated artifact order.",
        )

    artifacts_by_id = {
        artifact.artifact_id: artifact
        for artifact in artifacts
        if artifact.artifact_id not in duplicate_artifacts
    }
    for artifact in sorted(
        artifacts_by_id.values(), key=lambda item: item.artifact_id
    ):
        if (
            artifact.task_id,
            artifact.request_id,
            artifact.attempt_id,
        ) != (task.task_id, validation.request_id, validation.attempt_id):
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.LINEAGE,
                "Canonical artifact lineage differs from task validation.",
                artifact_id=artifact.artifact_id,
            )
    if validation.task_id != task.task_id:
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.LINEAGE,
            "Task-execution validation belongs to a different approved task.",
        )

    ordered_intents = tuple(
        sorted(intents, key=lambda item: (item.target_path, item.artifact_id))
    )
    intent_artifact_counts = Counter(item.artifact_id for item in intents)
    for artifact_id, count in sorted(intent_artifact_counts.items()):
        if count > 1:
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.DUPLICATE_ARTIFACT,
                "One artifact may have at most one materialization intent.",
                artifact_id=artifact_id,
            )
    intent_path_counts = Counter(item.target_path for item in intents)
    for path, count in sorted(intent_path_counts.items()):
        if count > 1:
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.DUPLICATE_PATH,
                "One task attempt may target a repository path at most once.",
                path=path,
            )
    for intent in ordered_intents:
        try:
            canonical_path = normalize_repository_path(intent.target_path)
        except (TypeError, WorkspaceContractError):
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.PATH_POLICY,
                "Materialization target violates repository path policy.",
                artifact_id=intent.artifact_id,
                path=str(intent.target_path),
            )
        else:
            if canonical_path != intent.target_path:
                _add_materialization_issue(
                    issues,
                    ArtifactMaterializationIssueCode.PATH_POLICY,
                    "Materialization target is not canonical.",
                    artifact_id=intent.artifact_id,
                    path=intent.target_path,
                )
        if intent.artifact_id not in artifacts_by_id:
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.ARTIFACT_REFERENCE,
                "Materialization intent references no unambiguous canonical artifact.",
                artifact_id=intent.artifact_id,
                path=intent.target_path,
            )

    if task.materialization_policy is TaskMaterializationPolicy.FORBIDDEN and intents:
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.POLICY,
            "FORBIDDEN task policy requires zero materialization intents.",
        )
    if (
        task.materialization_policy is TaskMaterializationPolicy.REQUIRED
        and not intents
    ):
        _add_materialization_issue(
            issues,
            ArtifactMaterializationIssueCode.POLICY,
            "REQUIRED task policy requires at least one materialization intent.",
        )
    _validate_deliverable_role_intents(
        task,
        artifacts_by_id,
        ordered_intents,
        issues,
    )

    canonical_issues = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.code.value,
                item.path or "",
                item.artifact_id or "",
                item.detail,
            ),
        )
    )
    payload = _materialization_validation_payload(
        task_id=task.task_id,
        request_id=validation.request_id,
        attempt_id=validation.attempt_id,
        policy=task.materialization_policy,
        artifact_ids=validation.artifact_ids,
        artifact_evidence_digests=tuple(
            sorted(_artifact_evidence_digest(item) for item in artifacts)
        ),
        intents=ordered_intents,
        passed=not canonical_issues,
        issues=canonical_issues,
    )
    return ArtifactMaterializationValidationResult(
        materialization_validation_id=_materialization_validation_id(payload),
        **payload,
    )


def _validate_deliverable_role_intents(
    task: Task,
    artifacts_by_id: dict[str, EngineeringArtifact],
    intents: tuple[ArtifactMaterializationIntent, ...],
    issues: list[ArtifactMaterializationValidationIssue],
) -> None:
    """Bind structured role obligations to canonical artifact/path intents."""

    materialized = tuple(
        (artifacts_by_id.get(intent.artifact_id), intent)
        for intent in intents
        if intent.artifact_id in artifacts_by_id
    )
    for role in task.deliverable_roles:
        if role is ProjectDeliverableRole.RUNNABLE_ENTRYPOINT:
            passed = any(
                artifact is not None
                and artifact.artifact_type is EngineeringArtifactType.SOURCE
                for artifact, _ in materialized
            )
            requirement = "a materialized canonical SOURCE artifact"
        elif role is ProjectDeliverableRole.AUTOMATED_TESTS:
            passed = any(
                artifact is not None
                and artifact.artifact_type is EngineeringArtifactType.TEST
                for artifact, _ in materialized
            )
            requirement = "a materialized canonical TEST artifact"
        else:
            passed = any(
                artifact is not None
                and artifact.artifact_type is EngineeringArtifactType.DOCUMENTATION
                and intent.target_path == "README.md"
                for artifact, intent in materialized
            )
            requirement = (
                "a materialized canonical DOCUMENTATION artifact at root README.md"
            )
        if not passed:
            _add_materialization_issue(
                issues,
                ArtifactMaterializationIssueCode.DELIVERABLE_ROLE,
                f"{role.value} requires {requirement}.",
            )


def canonicalize_artifact_materialization_proposals(
    result: TaskExecutionResult,
    artifacts: tuple[EngineeringArtifact, ...],
) -> tuple[ArtifactMaterializationIntent, ...]:
    """Correlate untrusted output ordinals to canonical artifact identities."""

    artifacts_by_index = {artifact.output_index: artifact for artifact in artifacts}
    if len(artifacts_by_index) != len(artifacts):
        raise WorkspaceContractError(
            "Canonical artifact output indices must be unique."
        )
    intents: list[ArtifactMaterializationIntent] = []
    for proposal in result.materialization_proposals:
        artifact = artifacts_by_index.get(proposal.output_index)
        if artifact is None:
            raise WorkspaceContractError(
                "Materialization proposal references an unknown output index: "
                f"{proposal.output_index}."
            )
        try:
            intent = ArtifactMaterializationIntent(
                artifact_id=artifact.artifact_id,
                target_path=proposal.target_path,
            )
        except ValueError as exc:
            raise WorkspaceContractError(
                "Materialization proposal target violates repository path policy."
            ) from exc
        intents.append(intent)
    return tuple(
        sorted(intents, key=lambda item: (item.target_path, item.artifact_id))
    )


def artifact_materialization_validation_identity_is_valid(
    validation: ArtifactMaterializationValidationResult,
) -> bool:
    """Return whether materialization evidence still binds its exact contents."""

    return validation.materialization_validation_id == _materialization_validation_id(
        _materialization_validation_payload_from_result(validation)
    )


def build_workspace_change_set(
    snapshot: WorkspaceSnapshot,
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    materialization_validation: ArtifactMaterializationValidationResult,
) -> WorkspaceChangeSet:
    """Convert validated materialization intents into authoritative desired state."""

    _require_valid_snapshot(snapshot)
    _require_valid_materialization_evidence(
        validation, artifacts, materialization_validation
    )
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    by_path = tuple(
        (
            intent.target_path,
            artifacts_by_id[intent.artifact_id],
        )
        for intent in materialization_validation.intents
    )
    changes = tuple(
        _derive_file_change(snapshot, path, artifact)
        for path, artifact in by_path
    )
    first = by_path[0][1]
    materialized_ids = tuple(change.artifact_id for change in changes)
    payload = _change_set_payload(
        workspace_id=snapshot.workspace_id,
        base_snapshot_id=snapshot.snapshot_id,
        requirement_spec_id=first.requirement_spec_id,
        graph_id=first.graph_id,
        task_id=first.task_id,
        request_id=first.request_id,
        attempt_id=first.attempt_id,
        attempt_number=first.attempt_number,
        materialization_validation_id=(
            materialization_validation.materialization_validation_id
        ),
        materialized_artifact_ids=materialized_ids,
        file_changes=changes,
    )
    return WorkspaceChangeSet(
        change_set_id=_change_set_id(payload),
        **payload,
    )


def validate_workspace_change_set(
    change_set: WorkspaceChangeSet,
    snapshot: WorkspaceSnapshot,
    artifacts: tuple[EngineeringArtifact, ...],
    materialization_validation: ArtifactMaterializationValidationResult,
) -> WorkspaceChangeSetValidationResult:
    """Validate desired-state lineage against exact materialization evidence."""

    issues: list[WorkspaceChangeSetValidationIssue] = []
    _validate_snapshot_for_change_set(change_set, snapshot, issues)
    _validate_materialization_evidence_for_change_set(
        change_set, materialization_validation, issues
    )

    artifact_id_counts = Counter(artifact.artifact_id for artifact in artifacts)
    duplicate_artifact_ids = {
        artifact_id
        for artifact_id, count in artifact_id_counts.items()
        if count > 1
    }
    for artifact_id in sorted(duplicate_artifact_ids):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE,
            None,
            f"Duplicate canonical artifact ID is ambiguous: {artifact_id}.",
        )
    artifacts_by_id = {
        artifact.artifact_id: artifact
        for artifact in artifacts
        if artifact.artifact_id not in duplicate_artifact_ids
    }
    if tuple(artifact.artifact_id for artifact in artifacts) != (
        materialization_validation.artifact_ids
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE,
            None,
            "Canonical artifacts do not match materialization evidence.",
        )
    if tuple(sorted(_artifact_evidence_digest(item) for item in artifacts)) != (
        materialization_validation.artifact_evidence_digests
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE,
            None,
            "Canonical artifact contents differ from materialization evidence.",
        )

    changes = change_set.file_changes
    paths = tuple(change.path for change in changes)
    if paths != tuple(sorted(paths)):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ORDERING,
            None,
            "File changes are not in canonical path order.",
        )
    if len(paths) != len(set(paths)):
        for path in sorted(path for path in set(paths) if paths.count(path) > 1):
            _add_issue(
                issues,
                WorkspaceChangeSetIssueCode.DUPLICATE_PATH,
                path,
                "Canonical destination occurs more than once.",
            )
    if change_set.materialized_artifact_ids != tuple(
        change.artifact_id for change in changes
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ORDERING,
            None,
            "Materialized artifact IDs do not match canonical file-change order.",
        )

    intents_by_artifact = {
        intent.artifact_id: intent for intent in materialization_validation.intents
    }
    for change in sorted(changes, key=lambda item: (item.path, item.artifact_id)):
        _validate_file_change(
            change_set,
            change,
            snapshot,
            artifacts_by_id,
            intents_by_artifact,
            issues,
        )

    referenced = set(change_set.materialized_artifact_ids)
    expected = {intent.artifact_id for intent in materialization_validation.intents}
    supplied_artifact_ids = set(artifact_id_counts)
    for artifact_id in sorted(referenced - supplied_artifact_ids):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE,
            None,
            f"Referenced materialized artifact does not exist: {artifact_id}.",
        )
    if referenced != expected:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE,
            None,
            "Materialized artifacts do not exactly match validated intents.",
        )

    if not workspace_change_set_identity_is_valid(change_set):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.CHANGE_SET_ID,
            None,
            "Change-set identity does not match its canonical contents.",
        )
    return _validation_result(change_set, snapshot, issues)


def workspace_change_set_identity_is_valid(
    change_set: WorkspaceChangeSet,
) -> bool:
    """Return whether a change set ID still binds its canonical contents."""

    return change_set.change_set_id == _change_set_id(
        _change_set_payload_from_change_set(change_set)
    )


def validate_workspace_change_set_preimages(
    change_set: WorkspaceChangeSet,
    comparison_snapshot: WorkspaceSnapshot,
) -> WorkspaceChangeSetValidationResult:
    """Check optimistic preimages against a supplied logical current snapshot."""

    issues: list[WorkspaceChangeSetValidationIssue] = []
    if change_set.workspace_id != comparison_snapshot.workspace_id:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.WORKSPACE_ID,
            None,
            "Comparison snapshot belongs to a different workspace.",
        )
    if not _snapshot_identity_is_valid(comparison_snapshot):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.SNAPSHOT_ID,
            None,
            "Comparison snapshot canonical identity is invalid.",
        )
        return _validation_result(change_set, comparison_snapshot, issues)
    for change in sorted(change_set.file_changes, key=lambda item: item.path):
        current = comparison_snapshot.file_state(change.path)
        if change.operation is WorkspaceChangeOperation.CREATE:
            if current is not None:
                _add_issue(
                    issues,
                    WorkspaceChangeSetIssueCode.STALE_PREIMAGE,
                    change.path,
                    "CREATE destination now exists.",
                )
            continue
        if current is None:
            _add_issue(
                issues,
                WorkspaceChangeSetIssueCode.STALE_PREIMAGE,
                change.path,
                "Expected preimage is now absent.",
            )
        elif current.content_hash != change.expected_preimage_hash:
            _add_issue(
                issues,
                WorkspaceChangeSetIssueCode.STALE_PREIMAGE,
                change.path,
                "Current content hash differs from the expected preimage hash.",
            )
    return _validation_result(change_set, comparison_snapshot, issues)


def analyze_workspace_change_set_conflicts(
    change_sets: tuple[WorkspaceChangeSet, ...],
) -> WorkspaceChangeSetConflictAnalysis:
    """Conservatively identify same-path proposals from parallel task attempts.

    Two NO_CHANGE observations are compatible.  Any same-path combination that
    contains a mutation is a conflict, including mutation plus NO_CHANGE and
    identical mutations, because independently produced task change sets are not
    implicitly merged.
    """

    if len(change_sets) < 2:
        raise WorkspaceContractError(
            "Conflict analysis requires at least two task change sets."
        )
    workspace_ids = {item.workspace_id for item in change_sets}
    snapshot_ids = {item.base_snapshot_id for item in change_sets}
    if len(workspace_ids) != 1 or len(snapshot_ids) != 1:
        raise WorkspaceContractError(
            "Parallel conflict analysis requires one workspace and base snapshot."
        )
    change_set_ids = [item.change_set_id for item in change_sets]
    task_ids = [item.task_id for item in change_sets]
    if len(change_set_ids) != len(set(change_set_ids)):
        raise WorkspaceContractError("Change-set IDs must be unique.")
    if len(task_ids) != len(set(task_ids)):
        raise WorkspaceContractError(
            "Parallel conflict analysis requires independently tasked change sets."
        )

    by_path: dict[
        str, list[tuple[WorkspaceChangeSet, WorkspaceFileChange]]
    ] = defaultdict(list)
    for change_set in sorted(change_sets, key=lambda item: item.change_set_id):
        seen: set[str] = set()
        for change in change_set.file_changes:
            if change.path in seen:
                raise WorkspaceContractError(
                    f"Change set {change_set.change_set_id} has duplicate path "
                    f"{change.path}."
                )
            seen.add(change.path)
            by_path[change.path].append((change_set, change))

    conflicts: list[WorkspaceChangeConflict] = []
    for path in sorted(by_path):
        proposed = by_path[path]
        if len(proposed) < 2 or all(
            change.operation is WorkspaceChangeOperation.NO_CHANGE
            for _, change in proposed
        ):
            continue
        participants = tuple(
            WorkspaceChangeConflictParticipant(
                change_set_id=change_set.change_set_id,
                task_id=change_set.task_id,
                request_id=change_set.request_id,
                attempt_id=change_set.attempt_id,
                operation=change.operation,
                desired_content_hash=change.desired_content_hash,
            )
            for change_set, change in sorted(
                proposed,
                key=lambda item: (item[0].task_id, item[0].change_set_id),
            )
        )
        conflicts.append(
            WorkspaceChangeConflict(path=path, participants=participants)
        )

    return WorkspaceChangeSetConflictAnalysis(
        workspace_id=next(iter(workspace_ids)),
        base_snapshot_id=next(iter(snapshot_ids)),
        has_conflicts=bool(conflicts),
        conflicts=tuple(conflicts),
    )


def _normalize_repository_path(path: str, *, enforce_protected: bool) -> str:
    if not path:
        raise WorkspaceContractError("Repository-relative path must be non-empty.")
    if "\x00" in path:
        raise WorkspaceContractError("Repository-relative path contains NUL.")
    if "\\" in path:
        raise WorkspaceContractError(
            "Backslashes are not allowed in canonical repository paths."
        )
    if path.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(path):
        raise WorkspaceContractError("Absolute or drive-qualified paths are forbidden.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceContractError(
            "Repository path contains empty, dot, or traversal segments."
        )
    normalized = "/".join(parts)
    if enforce_protected:
        first = parts[0].casefold()
        if first in _PROTECTED_DIRECTORY_NAMES or normalized.casefold() == ".env":
            raise WorkspaceContractError(
                f"Protected repository path is not mutable: {normalized}."
            )
    return normalized


def _validated_sha256(value: str, label: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase hexadecimal SHA-256 hash")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_payload(
    workspace_id: str, files: tuple[WorkspaceFileState, ...]
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "files": [item.model_dump(mode="json") for item in files],
    }


def _snapshot_id(
    workspace_id: str, files: tuple[WorkspaceFileState, ...]
) -> str:
    digest = _content_hash(_snapshot_payload(workspace_id, files))
    return f"WORKSPACE-SNAPSHOT-{digest[:12].upper()}"


def _require_valid_snapshot(snapshot: WorkspaceSnapshot) -> None:
    if not _snapshot_identity_is_valid(snapshot):
        raise WorkspaceContractError("Workspace snapshot identity is invalid.")


def _snapshot_identity_is_valid(snapshot: WorkspaceSnapshot) -> bool:
    paths = tuple(item.path for item in snapshot.files)
    return (
        paths == tuple(sorted(paths))
        and len(paths) == len(set(paths))
        and snapshot.snapshot_id == _snapshot_id(snapshot.workspace_id, snapshot.files)
    )


def _require_artifact_lineage(
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
) -> None:
    first = artifacts[0]
    expected = (
        first.requirement_spec_id,
        first.graph_id,
        first.task_id,
        first.request_id,
        first.attempt_id,
        first.attempt_number,
    )
    for artifact in artifacts:
        actual = (
            artifact.requirement_spec_id,
            artifact.graph_id,
            artifact.task_id,
            artifact.request_id,
            artifact.attempt_id,
            artifact.attempt_number,
        )
        if actual != expected:
            raise WorkspaceContractError(
                "Canonical artifacts must share one task-attempt lineage."
            )
    if (
        validation.task_id,
        validation.request_id,
        validation.attempt_id,
    ) != (first.task_id, first.request_id, first.attempt_id):
        raise WorkspaceContractError(
            "Task validation does not match canonical artifact lineage."
        )


def _require_valid_materialization_evidence(
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    materialization: ArtifactMaterializationValidationResult,
) -> None:
    if not materialization.passed or materialization.issues:
        raise WorkspaceContractError(
            "A workspace change set requires passed materialization validation."
        )
    if not artifact_materialization_validation_identity_is_valid(materialization):
        raise WorkspaceContractError(
            "Materialization validation identity is not canonical."
        )
    artifact_ids = tuple(artifact.artifact_id for artifact in artifacts)
    if artifact_ids != validation.artifact_ids or artifact_ids != (
        materialization.artifact_ids
    ):
        raise WorkspaceContractError(
            "Artifacts must exactly match task and materialization validation."
        )
    if tuple(sorted(_artifact_evidence_digest(item) for item in artifacts)) != (
        materialization.artifact_evidence_digests
    ):
        raise WorkspaceContractError(
            "Artifacts differ from materialization validation evidence."
        )
    if (
        materialization.task_id,
        materialization.request_id,
        materialization.attempt_id,
    ) != (validation.task_id, validation.request_id, validation.attempt_id):
        raise WorkspaceContractError(
            "Materialization validation does not match task validation lineage."
        )
    if not materialization.intents:
        raise WorkspaceContractError(
            "A workspace change set requires at least one validated intent."
        )
    _require_artifact_lineage(validation, artifacts)


def _materialization_validation_payload(
    *,
    task_id: str,
    request_id: str,
    attempt_id: str,
    policy: TaskMaterializationPolicy,
    artifact_ids: tuple[str, ...],
    artifact_evidence_digests: tuple[str, ...],
    intents: tuple[ArtifactMaterializationIntent, ...],
    passed: bool,
    issues: tuple[ArtifactMaterializationValidationIssue, ...],
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "request_id": request_id,
        "attempt_id": attempt_id,
        "policy": policy,
        "artifact_ids": artifact_ids,
        "artifact_evidence_digests": artifact_evidence_digests,
        "intents": intents,
        "passed": passed,
        "issues": issues,
    }


def _materialization_validation_payload_from_result(
    result: ArtifactMaterializationValidationResult,
) -> dict[str, object]:
    return _materialization_validation_payload(
        task_id=result.task_id,
        request_id=result.request_id,
        attempt_id=result.attempt_id,
        policy=result.policy,
        artifact_ids=result.artifact_ids,
        artifact_evidence_digests=result.artifact_evidence_digests,
        intents=result.intents,
        passed=result.passed,
        issues=result.issues,
    )


def _materialization_validation_id(payload: dict[str, object]) -> str:
    serializable = {
        key: (
            [item.model_dump(mode="json") for item in value]
            if key in {"intents", "issues"}
            else value
        )
        for key, value in payload.items()
    }
    digest = _content_hash(serializable)
    return f"MATERIALIZATION-VALIDATION-{digest[:12].upper()}"


def _artifact_evidence_digest(artifact: EngineeringArtifact) -> str:
    return _content_hash(artifact.model_dump(mode="json"))


def _add_materialization_issue(
    issues: list[ArtifactMaterializationValidationIssue],
    code: ArtifactMaterializationIssueCode,
    detail: str,
    *,
    artifact_id: str | None = None,
    path: str | None = None,
) -> None:
    issues.append(
        ArtifactMaterializationValidationIssue(
            code=code,
            artifact_id=artifact_id,
            path=path,
            detail=detail,
        )
    )


def _derive_file_change(
    snapshot: WorkspaceSnapshot,
    path: str,
    artifact: EngineeringArtifact,
) -> WorkspaceFileChange:
    desired_hash = workspace_file_content_hash(artifact.content)
    current = snapshot.file_state(path)
    if current is None:
        operation = WorkspaceChangeOperation.CREATE
        expected_hash = None
    elif current.content_hash == desired_hash:
        operation = WorkspaceChangeOperation.NO_CHANGE
        expected_hash = current.content_hash
    else:
        operation = WorkspaceChangeOperation.MODIFY
        expected_hash = current.content_hash
    return WorkspaceFileChange(
        artifact_id=artifact.artifact_id,
        artifact_lineage_id=artifact.lineage_id,
        path=path,
        operation=operation,
        expected_preimage_hash=expected_hash,
        desired_content_hash=desired_hash,
        desired_content=artifact.content,
    )


def _change_set_payload(
    *,
    workspace_id: str,
    base_snapshot_id: str,
    requirement_spec_id: str,
    graph_id: str,
    task_id: str,
    request_id: str,
    attempt_id: str,
    attempt_number: int,
    materialization_validation_id: str,
    materialized_artifact_ids: tuple[str, ...],
    file_changes: tuple[WorkspaceFileChange, ...],
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "base_snapshot_id": base_snapshot_id,
        "requirement_spec_id": requirement_spec_id,
        "graph_id": graph_id,
        "task_id": task_id,
        "request_id": request_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "materialization_validation_id": materialization_validation_id,
        "materialized_artifact_ids": materialized_artifact_ids,
        "file_changes": file_changes,
    }


def _change_set_payload_from_change_set(
    change_set: WorkspaceChangeSet,
) -> dict[str, object]:
    return _change_set_payload(
        workspace_id=change_set.workspace_id,
        base_snapshot_id=change_set.base_snapshot_id,
        requirement_spec_id=change_set.requirement_spec_id,
        graph_id=change_set.graph_id,
        task_id=change_set.task_id,
        request_id=change_set.request_id,
        attempt_id=change_set.attempt_id,
        attempt_number=change_set.attempt_number,
        materialization_validation_id=change_set.materialization_validation_id,
        materialized_artifact_ids=change_set.materialized_artifact_ids,
        file_changes=change_set.file_changes,
    )


def _change_set_id(payload: dict[str, object]) -> str:
    serializable = {
        key: (
            [item.model_dump(mode="json", warnings="none") for item in value]
            if key == "file_changes"
            else value
        )
        for key, value in payload.items()
    }
    digest = _content_hash(serializable)
    return f"WORKSPACE-CHANGESET-{digest[:12].upper()}"


def _validate_snapshot_for_change_set(
    change_set: WorkspaceChangeSet,
    snapshot: WorkspaceSnapshot,
    issues: list[WorkspaceChangeSetValidationIssue],
) -> None:
    if change_set.workspace_id != snapshot.workspace_id:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.WORKSPACE_ID,
            None,
            "Change set and snapshot workspace identities differ.",
        )
    if not _snapshot_identity_is_valid(snapshot):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.SNAPSHOT_ID,
            None,
            "Supplied snapshot canonical identity is invalid.",
        )
    if change_set.base_snapshot_id != snapshot.snapshot_id:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.SNAPSHOT_ID,
            None,
            "Change set is bound to a different base snapshot.",
        )


def _validate_materialization_evidence_for_change_set(
    change_set: WorkspaceChangeSet,
    validation: ArtifactMaterializationValidationResult,
    issues: list[WorkspaceChangeSetValidationIssue],
) -> None:
    if (
        not validation.passed
        or validation.issues
        or not artifact_materialization_validation_identity_is_valid(validation)
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.MATERIALIZATION_EVIDENCE,
            None,
            "Materialization validation is not passed canonical evidence.",
        )
    if change_set.materialization_validation_id != (
        validation.materialization_validation_id
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.MATERIALIZATION_EVIDENCE,
            None,
            "Change set references different materialization validation evidence.",
        )
    if (
        change_set.task_id,
        change_set.request_id,
        change_set.attempt_id,
    ) != (validation.task_id, validation.request_id, validation.attempt_id):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.MATERIALIZATION_EVIDENCE,
            None,
            "Materialization validation lineage differs from its change set.",
        )


def _validate_file_change(
    change_set: WorkspaceChangeSet,
    change: WorkspaceFileChange,
    snapshot: WorkspaceSnapshot,
    artifacts_by_id: dict[str, EngineeringArtifact],
    intents_by_artifact: dict[str, ArtifactMaterializationIntent],
    issues: list[WorkspaceChangeSetValidationIssue],
) -> None:
    try:
        canonical_path = normalize_repository_path(change.path)
    except (TypeError, WorkspaceContractError):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.PATH_POLICY,
            str(change.path),
            "Destination path is not canonical or policy-allowed.",
        )
        canonical_path = None
    else:
        if canonical_path != change.path:
            _add_issue(
                issues,
                WorkspaceChangeSetIssueCode.PATH_POLICY,
                change.path,
                "Destination path is not canonical.",
            )

    artifact = artifacts_by_id.get(change.artifact_id)
    if artifact is None:
        return
    lineage = (
        artifact.requirement_spec_id,
        artifact.graph_id,
        artifact.task_id,
        artifact.request_id,
        artifact.attempt_id,
        artifact.attempt_number,
    )
    if lineage != (
        change_set.requirement_spec_id,
        change_set.graph_id,
        change_set.task_id,
        change_set.request_id,
        change_set.attempt_id,
        change_set.attempt_number,
    ):
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.LINEAGE,
            change.path,
            "Artifact task-attempt lineage differs from its change set.",
        )
    if change.artifact_lineage_id != artifact.lineage_id:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.PROVENANCE,
            change.path,
            "File-change artifact lineage does not match its canonical artifact.",
        )
    intent = intents_by_artifact.get(change.artifact_id)
    if intent is None:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.PROVENANCE,
            change.path,
            "File change has no matching validated materialization intent.",
        )
    elif canonical_path is not None and intent.target_path != canonical_path:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.PROVENANCE,
            change.path,
            "Destination path does not match its validated materialization intent.",
        )
    if change.desired_content != artifact.content:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.DESIRED_CONTENT,
            change.path,
            "Desired contents do not match the canonical artifact.",
        )
    desired_hash = workspace_file_content_hash(artifact.content)
    if change.desired_content_hash != desired_hash:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.DESIRED_CONTENT_HASH,
            change.path,
            "Desired content hash does not match the canonical artifact.",
        )

    if canonical_path is None:
        return
    current = snapshot.file_state(canonical_path)
    if current is None:
        expected_operation = WorkspaceChangeOperation.CREATE
        expected_preimage = None
    elif current.content_hash == desired_hash:
        expected_operation = WorkspaceChangeOperation.NO_CHANGE
        expected_preimage = current.content_hash
    else:
        expected_operation = WorkspaceChangeOperation.MODIFY
        expected_preimage = current.content_hash
    if change.operation != expected_operation:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.OPERATION,
            change.path,
            f"Operation must be application-derived as {expected_operation.value}.",
        )
    if change.expected_preimage_hash != expected_preimage:
        _add_issue(
            issues,
            WorkspaceChangeSetIssueCode.PREIMAGE_HASH,
            change.path,
            "Expected preimage hash does not match the base snapshot.",
        )


def _validation_result(
    change_set: WorkspaceChangeSet,
    snapshot: WorkspaceSnapshot,
    issues: list[WorkspaceChangeSetValidationIssue],
) -> WorkspaceChangeSetValidationResult:
    return WorkspaceChangeSetValidationResult(
        change_set_id=change_set.change_set_id,
        workspace_id=snapshot.workspace_id,
        snapshot_id=snapshot.snapshot_id,
        passed=not issues,
        issues=tuple(issues),
    )


def _add_issue(
    issues: list[WorkspaceChangeSetValidationIssue],
    code: WorkspaceChangeSetIssueCode,
    path: str | None,
    detail: str,
) -> None:
    issues.append(
        WorkspaceChangeSetValidationIssue(code=code, path=path, detail=detail)
    )
