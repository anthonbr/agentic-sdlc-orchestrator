"""Deterministic final evidence for application-owned project delivery policy."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agentic_sdlc.project_delivery import (
    ProjectDeliverableRole,
    ProjectDeliveryMode,
    ProjectDeliveryPolicy,
)
from agentic_sdlc.task_execution import (
    TaskExecutionStatus,
    TaskGraphExecutionState,
)
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    EngineeringArtifactType,
    TaskExecutionRequest,
    TaskExecutionValidationResult,
)
from agentic_sdlc.task_graph import Task, TaskGraph, TaskMaterializationPolicy
from agentic_sdlc.validation_execution_contracts import (
    RequiredValidationExecutionStatus,
    TaskValidationExecutionEvidence,
    TaskValidationProvisioningEvidence,
    final_workspace_validation_execution_status,
    required_validation_execution_status,
)
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationValidationResult,
    WorkspaceChangeSet,
    WorkspaceChangeSetValidationResult,
    WorkspaceSnapshot,
    artifact_materialization_validation_identity_is_valid,
    workspace_change_set_identity_is_valid,
    workspace_file_content_hash,
    workspace_snapshot_identity_is_valid,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationResult,
    WorkspaceMutationStatus,
)
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDecision,
    WorkspaceBoundTaskExecutionRequest,
)


class ProjectReadinessIssueCode(StrEnum):
    """Stable categories for final delivery-readiness evidence defects."""

    POLICY_BINDING = "POLICY_BINDING"
    TASK_GRAPH_ROLE = "TASK_GRAPH_ROLE"
    ROLE_EVIDENCE = "ROLE_EVIDENCE"
    FINAL_SNAPSHOT = "FINAL_SNAPSHOT"
    RUNTIME_VALIDATION = "RUNTIME_VALIDATION"


class ProjectReadinessIssue(BaseModel):
    """One deterministic reason the final project delivery contract is incomplete."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ProjectReadinessIssueCode
    role: ProjectDeliverableRole | None
    detail: str


class ProjectReadinessRoleEvidence(BaseModel):
    """Complete authoritative chain for one materially satisfied role."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: ProjectDeliverableRole
    task_id: str
    request_id: str
    attempt_id: str
    artifact_id: str
    target_path: str
    artifact_content_hash: str
    materialized_content_hash: str
    final_snapshot_id: str


class ProjectReadinessValidation(BaseModel):
    """Application judgment over final canonical execution/workspace evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    readiness_validation_id: str
    policy: ProjectDeliveryPolicy
    passed: bool
    required_roles: tuple[ProjectDeliverableRole, ...]
    role_evidence: tuple[ProjectReadinessRoleEvidence, ...]
    issues: tuple[ProjectReadinessIssue, ...]
    runtime_validation_required: bool = False
    runtime_validation_required_count: int = Field(default=0, ge=0)
    runtime_validation_verified_count: int = Field(default=0, ge=0)
    runtime_execution_verified: bool = False
    python_compile_verified_count: int = Field(default=0, ge=0)
    python_pytest_verified_count: int = Field(default=0, ge=0)
    dependency_provisioning_verified_count: int = Field(default=0, ge=0)
    final_workspace_validation_required: bool = False
    final_workspace_validation_required_count: int = Field(default=0, ge=0)
    final_workspace_validation_verified_count: int = Field(default=0, ge=0)
    final_workspace_validation_verified: bool = False
    final_workspace_snapshot_id: str | None = None


def validate_project_readiness(
    policy: ProjectDeliveryPolicy,
    *,
    run_id: str | None = None,
    graph: TaskGraph | None,
    execution: TaskGraphExecutionState | None,
    requests: tuple[TaskExecutionRequest, ...] = (),
    execution_validations: tuple[TaskExecutionValidationResult, ...] = (),
    artifacts: tuple[EngineeringArtifact, ...] = (),
    materialization_validations: tuple[
        ArtifactMaterializationValidationResult, ...
    ] = (),
    change_sets: tuple[WorkspaceChangeSet, ...] = (),
    change_set_validations: tuple[WorkspaceChangeSetValidationResult, ...] = (),
    mutations: tuple[WorkspaceMutationResult, ...] = (),
    authoritative_snapshot: WorkspaceSnapshot | None = None,
    workspace_bound_requests: tuple[
        WorkspaceBoundTaskExecutionRequest, ...
    ] = (),
    workspace_snapshots: tuple[WorkspaceSnapshot, ...] = (),
    validation_execution_evidence: tuple[
        TaskValidationExecutionEvidence, ...
    ] = (),
    validation_provisioning_evidence: tuple[
        TaskValidationProvisioningEvidence, ...
    ] = (),
    final_validation_execution_evidence: tuple[
        TaskValidationExecutionEvidence, ...
    ] = (),
    final_validation_provisioning_evidence: tuple[
        TaskValidationProvisioningEvidence, ...
    ] = (),
    task_attempt_exit_decisions: tuple[TaskAttemptExitDecision, ...] = (),
) -> ProjectReadinessValidation:
    """Prove structural delivery readiness from retained authoritative evidence.

    This validator never runs commands. It verifies retained task-level evidence
    and application-required evidence for the authoritative final snapshot.
    """

    issues: list[ProjectReadinessIssue] = []
    evidence: list[ProjectReadinessRoleEvidence] = []
    task_runtime_status = required_validation_execution_status(
        graph,
        execution,
        run_id=run_id,
        bound_requests=workspace_bound_requests,
        evidence=validation_execution_evidence,
        provisioning_evidence=validation_provisioning_evidence,
        snapshots=workspace_snapshots,
        change_sets=change_sets,
        exit_decisions=task_attempt_exit_decisions,
    )
    final_runtime_status = final_workspace_validation_execution_status(
        policy,
        run_id=run_id,
        graph=graph,
        authoritative_snapshot=authoritative_snapshot,
        evidence=final_validation_execution_evidence,
        provisioning_evidence=final_validation_provisioning_evidence,
    )
    runtime_status = _combined_runtime_status(
        task_runtime_status, final_runtime_status
    )
    if task_runtime_status.required and not task_runtime_status.verified:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.RUNTIME_VALIDATION,
                role=None,
                detail=(
                    "Approved required validation lacks exact successful "
                    "final-attempt execution evidence."
                ),
            )
        )
    if final_runtime_status.required and not final_runtime_status.verified:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.RUNTIME_VALIDATION,
                role=None,
                detail=(
                    "Application-required validation lacks exact successful "
                    "evidence for the authoritative final workspace snapshot."
                ),
            )
        )
    if graph is not None and graph.delivery_policy != policy:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.POLICY_BINDING,
                role=None,
                detail=(
                    "Approved TaskGraph delivery policy differs from authoritative "
                    "application policy."
                ),
            )
        )

    if policy.mode is ProjectDeliveryMode.ENGINEERING_ARTIFACTS:
        return _validation(
            policy,
            evidence,
            issues,
            runtime_status,
            final_runtime_status=final_runtime_status,
            final_snapshot=authoritative_snapshot,
        )

    if graph is None:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.TASK_GRAPH_ROLE,
                role=None,
                detail="Runnable-project readiness requires an approved TaskGraph.",
            )
        )
        return _validation(
            policy,
            evidence,
            issues,
            runtime_status,
            final_runtime_status=final_runtime_status,
            final_snapshot=authoritative_snapshot,
        )
    if authoritative_snapshot is None:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.FINAL_SNAPSHOT,
                role=None,
                detail=(
                    "Runnable-project readiness requires the authoritative final "
                    "workspace snapshot."
                ),
            )
        )
    elif not workspace_snapshot_identity_is_valid(authoritative_snapshot):
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.FINAL_SNAPSHOT,
                role=None,
                detail=(
                    "Authoritative final workspace snapshot identity does not "
                    "match its canonical manifest."
                ),
            )
        )
    if execution is None:
        issues.append(
            ProjectReadinessIssue(
                code=ProjectReadinessIssueCode.ROLE_EVIDENCE,
                role=None,
                detail="Runnable-project readiness requires final execution state.",
            )
        )
    if execution is None or authoritative_snapshot is None:
        return _validation(
            policy,
            evidence,
            issues,
            runtime_status,
            final_runtime_status=final_runtime_status,
            final_snapshot=authoritative_snapshot,
        )

    for role in policy.required_roles:
        role_tasks = tuple(
            task
            for task in graph.tasks
            if role in task.deliverable_roles
            and task.materialization_policy is TaskMaterializationPolicy.REQUIRED
        )
        if not role_tasks:
            issues.append(
                ProjectReadinessIssue(
                    code=ProjectReadinessIssueCode.TASK_GRAPH_ROLE,
                    role=role,
                    detail=(
                        "Approved TaskGraph lacks REQUIRED materialization coverage "
                        f"for {role.value}."
                    ),
                )
            )
            continue
        role_evidence = tuple(
            item
            for task in role_tasks
            for item in _evidence_for_task_role(
                role,
                task,
                execution,
                requests,
                execution_validations,
                artifacts,
                materialization_validations,
                change_sets,
                change_set_validations,
                mutations,
                authoritative_snapshot,
            )
        )
        if role_evidence:
            evidence.extend(role_evidence)
        else:
            issues.append(
                ProjectReadinessIssue(
                    code=ProjectReadinessIssueCode.ROLE_EVIDENCE,
                    role=role,
                    detail=(
                        f"Final authoritative evidence does not materially satisfy "
                        f"{role.value}."
                    ),
                )
            )
    return _validation(
        policy,
        evidence,
        issues,
        runtime_status,
        final_runtime_status=final_runtime_status,
        final_snapshot=authoritative_snapshot,
    )


def _evidence_for_task_role(
    role: ProjectDeliverableRole,
    task: Task,
    execution: TaskGraphExecutionState,
    requests: tuple[TaskExecutionRequest, ...],
    execution_validations: tuple[TaskExecutionValidationResult, ...],
    artifacts: tuple[EngineeringArtifact, ...],
    materialization_validations: tuple[
        ArtifactMaterializationValidationResult, ...
    ],
    change_sets: tuple[WorkspaceChangeSet, ...],
    change_set_validations: tuple[WorkspaceChangeSetValidationResult, ...],
    mutations: tuple[WorkspaceMutationResult, ...],
    snapshot: WorkspaceSnapshot,
) -> tuple[ProjectReadinessRoleEvidence, ...]:
    runtime = next(
        (item for item in execution.task_states if item.task_id == task.task_id),
        None,
    )
    if (
        runtime is None
        or runtime.status is not TaskExecutionStatus.SUCCEEDED
        or runtime.attempt_count < 1
    ):
        return ()
    final_requests = tuple(
        item
        for item in requests
        if item.task_id == task.task_id
        and item.attempt_number == runtime.attempt_count
    )
    if len(final_requests) != 1:
        return ()
    request = final_requests[0]
    validations = tuple(
        item
        for item in execution_validations
        if item.task_id == task.task_id
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
        and item.passed
    )
    if len(validations) != 1:
        return ()
    validation = validations[0]
    final_artifacts = tuple(
        sorted(
            (
                item
                for item in artifacts
                if item.task_id == task.task_id
                and item.request_id == request.request_id
                and item.attempt_id == request.attempt_id
                and item.attempt_number == runtime.attempt_count
            ),
            key=lambda item: (item.output_index, item.artifact_id),
        )
    )
    if tuple(item.artifact_id for item in final_artifacts) != validation.artifact_ids:
        return ()
    artifacts_by_id = {item.artifact_id: item for item in final_artifacts}
    materializations = tuple(
        item
        for item in materialization_validations
        if item.task_id == task.task_id
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
        and item.passed
        and artifact_materialization_validation_identity_is_valid(item)
    )
    if len(materializations) != 1:
        return ()
    materialization = materializations[0]
    matching_change_sets = tuple(
        item
        for item in change_sets
        if item.materialization_validation_id
        == materialization.materialization_validation_id
        and workspace_change_set_identity_is_valid(item)
    )
    if len(matching_change_sets) != 1:
        return ()
    change_set = matching_change_sets[0]
    if change_set.workspace_id != snapshot.workspace_id:
        return ()
    matching_change_validations = tuple(
        item
        for item in change_set_validations
        if item.change_set_id == change_set.change_set_id
        and item.workspace_id == change_set.workspace_id
        and item.snapshot_id == change_set.base_snapshot_id
        and item.passed
        and not item.issues
    )
    if len(matching_change_validations) != 1:
        return ()
    matching_mutations = tuple(
        item
        for item in mutations
        if item.change_set_id == change_set.change_set_id
        and item.task_id == task.task_id
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
        and item.status is WorkspaceMutationStatus.APPLIED
    )
    if len(matching_mutations) != 1:
        return ()
    mutation = matching_mutations[0]

    evidence: list[ProjectReadinessRoleEvidence] = []
    for intent in materialization.intents:
        artifact = artifacts_by_id.get(intent.artifact_id)
        if artifact is None or not _artifact_satisfies_role(
            role, artifact, intent.target_path
        ):
            continue
        desired_hash = workspace_file_content_hash(artifact.content)
        file_changes = tuple(
            item
            for item in change_set.file_changes
            if item.artifact_id == artifact.artifact_id
            and item.path == intent.target_path
            and item.desired_content_hash == desired_hash
            and item.desired_content == artifact.content
        )
        mutation_evidence = tuple(
            item
            for item in mutation.file_evidence
            if item.path == intent.target_path
            and item.desired_postimage_hash == desired_hash
            and item.observed_postimage_hash == desired_hash
        )
        snapshot_file = snapshot.file_state(intent.target_path)
        if (
            len(file_changes) != 1
            or len(mutation_evidence) != 1
            or snapshot_file is None
            or snapshot_file.content_hash != desired_hash
        ):
            continue
        evidence.append(
            ProjectReadinessRoleEvidence(
                role=role,
                task_id=task.task_id,
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                artifact_id=artifact.artifact_id,
                target_path=intent.target_path,
                artifact_content_hash=artifact.content_hash,
                materialized_content_hash=desired_hash,
                final_snapshot_id=snapshot.snapshot_id,
            )
        )
    return tuple(evidence)


def _artifact_satisfies_role(
    role: ProjectDeliverableRole,
    artifact: EngineeringArtifact,
    path: str,
) -> bool:
    if role is ProjectDeliverableRole.RUNNABLE_ENTRYPOINT:
        return artifact.artifact_type is EngineeringArtifactType.SOURCE
    if role is ProjectDeliverableRole.AUTOMATED_TESTS:
        return artifact.artifact_type is EngineeringArtifactType.TEST
    return (
        artifact.artifact_type is EngineeringArtifactType.DOCUMENTATION
        and path == "README.md"
    )


def _validation(
    policy: ProjectDeliveryPolicy,
    evidence: list[ProjectReadinessRoleEvidence],
    issues: list[ProjectReadinessIssue],
    runtime_status: RequiredValidationExecutionStatus,
    *,
    final_runtime_status: RequiredValidationExecutionStatus,
    final_snapshot: WorkspaceSnapshot | None,
) -> ProjectReadinessValidation:
    ordered_evidence = tuple(
        sorted(
            evidence,
            key=lambda item: (
                item.role.value,
                item.task_id,
                item.target_path,
                item.artifact_id,
            ),
        )
    )
    ordered_issues = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.code.value,
                item.role.value if item.role is not None else "",
                item.detail,
            ),
        )
    )
    payload = {
        "policy": policy.model_dump(mode="json"),
        "passed": not ordered_issues,
        "required_roles": [role.value for role in policy.required_roles],
        "role_evidence": [item.model_dump(mode="json") for item in ordered_evidence],
        "issues": [item.model_dump(mode="json") for item in ordered_issues],
        "runtime_validation_required": runtime_status.required,
        "runtime_validation_required_count": runtime_status.required_count,
        "runtime_validation_verified_count": runtime_status.verified_count,
        "runtime_execution_verified": runtime_status.verified,
        "python_compile_verified_count": runtime_status.python_compile_verified_count,
        "python_pytest_verified_count": runtime_status.python_pytest_verified_count,
        "dependency_provisioning_verified_count": (
            runtime_status.dependency_provisioning_verified_count
        ),
        "final_workspace_validation_required": final_runtime_status.required,
        "final_workspace_validation_required_count": (
            final_runtime_status.required_count
        ),
        "final_workspace_validation_verified_count": (
            final_runtime_status.verified_count
        ),
        "final_workspace_validation_verified": final_runtime_status.verified,
        "final_workspace_snapshot_id": (
            final_snapshot.snapshot_id if final_snapshot is not None else None
        ),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    readiness_id = "READINESS-" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:16].upper()
    return ProjectReadinessValidation(
        readiness_validation_id=readiness_id,
        policy=policy,
        passed=not ordered_issues,
        required_roles=policy.required_roles,
        role_evidence=ordered_evidence,
        issues=ordered_issues,
        runtime_validation_required=runtime_status.required,
        runtime_validation_required_count=runtime_status.required_count,
        runtime_validation_verified_count=runtime_status.verified_count,
        runtime_execution_verified=runtime_status.verified,
        python_compile_verified_count=runtime_status.python_compile_verified_count,
        python_pytest_verified_count=runtime_status.python_pytest_verified_count,
        dependency_provisioning_verified_count=(
            runtime_status.dependency_provisioning_verified_count
        ),
        final_workspace_validation_required=final_runtime_status.required,
        final_workspace_validation_required_count=(
            final_runtime_status.required_count
        ),
        final_workspace_validation_verified_count=(
            final_runtime_status.verified_count
        ),
        final_workspace_validation_verified=final_runtime_status.verified,
        final_workspace_snapshot_id=(
            final_snapshot.snapshot_id if final_snapshot is not None else None
        ),
    )


def _combined_runtime_status(
    task_status: RequiredValidationExecutionStatus,
    final_status: RequiredValidationExecutionStatus,
) -> RequiredValidationExecutionStatus:
    required = task_status.required or final_status.required
    return RequiredValidationExecutionStatus(
        required=required,
        required_count=(task_status.required_count + final_status.required_count),
        verified_count=(task_status.verified_count + final_status.verified_count),
        verified=(
            required
            and (not task_status.required or task_status.verified)
            and (not final_status.required or final_status.verified)
        ),
        python_compile_verified_count=(
            task_status.python_compile_verified_count
            + final_status.python_compile_verified_count
        ),
        python_pytest_verified_count=(
            task_status.python_pytest_verified_count
            + final_status.python_pytest_verified_count
        ),
        dependency_provisioning_verified_count=(
            task_status.dependency_provisioning_verified_count
            + final_status.dependency_provisioning_verified_count
        ),
    )
