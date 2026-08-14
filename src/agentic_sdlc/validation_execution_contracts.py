"""Immutable contracts for governed task-validation execution and evidence."""

from __future__ import annotations

import hashlib
import json
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.task_execution_contracts import TaskExecutionRequest
from agentic_sdlc.task_graph import (
    TaskGraph,
    TaskValidationRequirement,
    ValidationExecutionProfile,
)
from agentic_sdlc.task_execution import (
    TaskExecutionStatus,
    TaskGraphExecutionState,
)
from agentic_sdlc.workspace_contracts import (
    WorkspaceChangeSet,
    WorkspaceFileState,
    WorkspaceSnapshot,
    build_workspace_snapshot,
    workspace_change_set_identity_is_valid,
    workspace_snapshot_identity_is_valid,
)
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDecision,
    TaskAttemptExitDisposition,
    WorkspaceBoundTaskExecutionRequest,
)


PYTHON_COMPILE_POLICY_VERSION = "python-compile-v1"
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 30.0
DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES = 16 * 1024
DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS = 2.0


class ValidationExecutionEnvironmentKind(StrEnum):
    """Application-owned execution-environment families."""

    LOCAL_DISPOSABLE = "LOCAL_DISPOSABLE"


class ValidationDependencyProvisioning(StrEnum):
    """Whether an execution environment contains governed dependencies."""

    NONE = "NONE"


class ValidationExecutionOutcome(StrEnum):
    """Trusted process-level outcomes for one required validation."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class GovernedValidationPolicy(BaseModel):
    """Complete application-owned policy resolved independently of the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    profile: ValidationExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    stdout_limit_bytes: int = Field(gt=0)
    stderr_limit_bytes: int = Field(gt=0)
    termination_grace_seconds: float = Field(gt=0)
    working_directory: str = "."
    environment_variable_names: tuple[str, ...]
    environment_kind: ValidationExecutionEnvironmentKind
    dependency_provisioning: ValidationDependencyProvisioning
    network_access_allowed: bool


class ValidationExecutionRequest(BaseModel):
    """Exact approved attempt and staged postimage authorized for validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    graph_id: str = Field(min_length=1)
    graph_version: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    requirement: TaskValidationRequirement
    source_workspace_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    staged_workspace_id: str = Field(min_length=1)
    staged_snapshot_id: str = Field(min_length=1)


class TaskValidationExecutionEvidence(BaseModel):
    """Immutable application-owned evidence from one governed process execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_id: str = Field(min_length=1)
    run_id: str
    graph_id: str
    graph_version: int = Field(ge=1)
    task_id: str
    request_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    validation_requirement_id: str
    profile: ValidationExecutionProfile
    policy_id: str
    policy_version: str
    source_workspace_id: str
    source_snapshot_id: str
    staged_workspace_id: str
    staged_snapshot_id: str
    environment_kind: ValidationExecutionEnvironmentKind
    dependency_provisioning: ValidationDependencyProvisioning
    network_access_allowed: bool
    provisioning_evidence_ids: tuple[str, ...] = ()
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str
    environment_variable_names: tuple[str, ...]
    started_at: str
    ended_at: str
    duration_seconds: float = Field(ge=0)
    outcome: ValidationExecutionOutcome
    exit_code: int | None
    timed_out: bool
    stdout_total_bytes: int = Field(ge=0)
    stderr_total_bytes: int = Field(ge=0)
    retained_stdout: str
    retained_stderr: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_truncated: bool
    stderr_truncated: bool
    passed: bool

    @field_validator("stdout_sha256", "stderr_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("Execution output hashes must be lowercase SHA-256.")
        return value

    @model_validator(mode="after")
    def _validate_outcome_flags(self) -> TaskValidationExecutionEvidence:
        expected_timed_out = self.outcome is ValidationExecutionOutcome.TIMED_OUT
        expected_passed = (
            self.outcome is ValidationExecutionOutcome.PASSED
            and self.exit_code == 0
            and not expected_timed_out
        )
        if self.outcome is ValidationExecutionOutcome.PASSED and self.exit_code != 0:
            raise ValueError("Passed execution evidence requires exit code zero.")
        if self.outcome is ValidationExecutionOutcome.FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("Failed execution evidence requires a non-zero exit code.")
        if self.timed_out != expected_timed_out:
            raise ValueError("Timeout flag does not match execution outcome.")
        if self.passed != expected_passed:
            raise ValueError("Pass judgment does not match execution outcome.")
        return self


class RequiredValidationExecutionStatus(BaseModel):
    """Deterministic final-state judgment without collapsing NOT_REQUIRED."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required: bool
    required_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    verified: bool


class ValidationExecutionContractError(ValueError):
    """Raised when validation authority or evidence correlation is invalid."""


def python_compile_validation_policy(
    *, executable: str | None = None
) -> GovernedValidationPolicy:
    """Resolve the sole Slice 1 profile to a fixed argv and bounded policy."""

    trusted_executable = str(Path(executable or sys.executable).resolve())
    argv = (
        trusted_executable,
        "-I",
        "-B",
        "-m",
        "compileall",
        "-q",
        ".",
    )
    payload = {
        "policy_version": PYTHON_COMPILE_POLICY_VERSION,
        "profile": ValidationExecutionProfile.PYTHON_COMPILE.value,
        "argv": argv,
        "timeout_seconds": DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        "stdout_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "stderr_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "termination_grace_seconds": DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        "working_directory": ".",
        "environment_variable_names": ("HOME", "LANG", "LC_ALL", "TMPDIR"),
        "environment_kind": ValidationExecutionEnvironmentKind.LOCAL_DISPOSABLE.value,
        "dependency_provisioning": ValidationDependencyProvisioning.NONE.value,
        "network_access_allowed": False,
    }
    digest = _content_hash(payload)[:16].upper()
    return GovernedValidationPolicy(
        policy_id=f"VALIDATION-POLICY-{digest}",
        policy_version=PYTHON_COMPILE_POLICY_VERSION,
        profile=ValidationExecutionProfile.PYTHON_COMPILE,
        argv=argv,
        timeout_seconds=DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        stdout_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        stderr_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        termination_grace_seconds=DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        working_directory=".",
        environment_variable_names=("HOME", "LANG", "LC_ALL", "TMPDIR"),
        environment_kind=ValidationExecutionEnvironmentKind.LOCAL_DISPOSABLE,
        dependency_provisioning=ValidationDependencyProvisioning.NONE,
        network_access_allowed=False,
    )


def resolve_governed_validation_policy(
    profile: ValidationExecutionProfile,
) -> GovernedValidationPolicy:
    """Resolve approved profile authority independently of any execution backend."""

    if profile is ValidationExecutionProfile.PYTHON_COMPILE:
        return python_compile_validation_policy()
    raise ValidationExecutionContractError(
        f"Unsupported governed validation profile: {profile!r}."
    )


def build_validation_execution_request(
    *,
    run_id: str,
    graph_id: str,
    graph_version: int,
    task_request: TaskExecutionRequest,
    requirement: TaskValidationRequirement,
    source_workspace_id: str,
    source_snapshot_id: str,
    staged_workspace_id: str,
    staged_snapshot_id: str,
) -> ValidationExecutionRequest:
    """Bind approved validation authority to one exact staged attempt postimage."""

    if task_request.graph_id != graph_id:
        raise ValidationExecutionContractError(
            "Task execution request belongs to a different TaskGraph."
        )
    if task_request.task_id != requirement.requirement_id.split("-VALIDATION-")[0]:
        raise ValidationExecutionContractError(
            "Validation requirement belongs to a different canonical task."
        )
    if requirement not in task_request.task.required_validations:
        raise ValidationExecutionContractError(
            "Validation requirement is not approved for this task attempt."
        )
    return ValidationExecutionRequest(
        run_id=run_id,
        graph_id=graph_id,
        graph_version=graph_version,
        task_id=task_request.task_id,
        request_id=task_request.request_id,
        attempt_id=task_request.attempt_id,
        attempt_number=task_request.attempt_number,
        requirement=requirement,
        source_workspace_id=source_workspace_id,
        source_snapshot_id=source_snapshot_id,
        staged_workspace_id=staged_workspace_id,
        staged_snapshot_id=staged_snapshot_id,
    )


def build_validation_execution_evidence(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    *,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    outcome: ValidationExecutionOutcome,
    exit_code: int | None,
    stdout_total_bytes: int,
    stderr_total_bytes: int,
    retained_stdout: str,
    retained_stderr: str,
    stdout_sha256: str,
    stderr_sha256: str,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> TaskValidationExecutionEvidence:
    """Create content-bound immutable evidence from trusted backend observations."""

    timed_out = outcome is ValidationExecutionOutcome.TIMED_OUT
    passed = (
        outcome is ValidationExecutionOutcome.PASSED
        and exit_code == 0
        and not timed_out
    )
    values = {
        "run_id": request.run_id,
        "graph_id": request.graph_id,
        "graph_version": request.graph_version,
        "task_id": request.task_id,
        "request_id": request.request_id,
        "attempt_id": request.attempt_id,
        "attempt_number": request.attempt_number,
        "validation_requirement_id": request.requirement.requirement_id,
        "profile": request.requirement.profile,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "source_workspace_id": request.source_workspace_id,
        "source_snapshot_id": request.source_snapshot_id,
        "staged_workspace_id": request.staged_workspace_id,
        "staged_snapshot_id": request.staged_snapshot_id,
        "environment_kind": policy.environment_kind,
        "dependency_provisioning": policy.dependency_provisioning,
        "network_access_allowed": policy.network_access_allowed,
        "provisioning_evidence_ids": (),
        "argv": policy.argv,
        "working_directory": policy.working_directory,
        "environment_variable_names": policy.environment_variable_names,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "outcome": outcome,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_total_bytes": stdout_total_bytes,
        "stderr_total_bytes": stderr_total_bytes,
        "retained_stdout": retained_stdout,
        "retained_stderr": retained_stderr,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "passed": passed,
    }
    return TaskValidationExecutionEvidence(
        evidence_id="VALIDATION-EVIDENCE-" + _content_hash(values)[:20].upper(),
        **values,
    )


def validation_execution_evidence_identity_is_valid(
    evidence: TaskValidationExecutionEvidence,
) -> bool:
    """Return whether the evidence ID still binds every immutable field."""

    payload = evidence.model_dump(mode="json", exclude={"evidence_id"})
    expected = "VALIDATION-EVIDENCE-" + _content_hash(payload)[:20].upper()
    return evidence.evidence_id == expected


def validation_execution_evidence_errors(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    evidence: TaskValidationExecutionEvidence,
) -> tuple[str, ...]:
    """Return exact correlation/integrity mismatches for fail-closed settlement."""

    expected = {
        "run_id": request.run_id,
        "graph_id": request.graph_id,
        "graph_version": request.graph_version,
        "task_id": request.task_id,
        "request_id": request.request_id,
        "attempt_id": request.attempt_id,
        "attempt_number": request.attempt_number,
        "validation_requirement_id": request.requirement.requirement_id,
        "profile": request.requirement.profile,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "source_workspace_id": request.source_workspace_id,
        "source_snapshot_id": request.source_snapshot_id,
        "staged_workspace_id": request.staged_workspace_id,
        "staged_snapshot_id": request.staged_snapshot_id,
        "environment_kind": policy.environment_kind,
        "dependency_provisioning": policy.dependency_provisioning,
        "network_access_allowed": policy.network_access_allowed,
        "argv": policy.argv,
        "working_directory": policy.working_directory,
        "environment_variable_names": policy.environment_variable_names,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(evidence, name) != value
    ]
    if policy.profile is not request.requirement.profile:
        mismatches.append("policy.profile")
    if evidence.provisioning_evidence_ids:
        mismatches.append("provisioning_evidence_ids")
    if not validation_execution_evidence_identity_is_valid(evidence):
        mismatches.append("evidence_id")
    if evidence.passed != (
        evidence.outcome is ValidationExecutionOutcome.PASSED
        and evidence.exit_code == 0
        and not evidence.timed_out
    ):
        mismatches.append("passed")
    if evidence.stdout_truncated != (
        evidence.stdout_total_bytes > policy.stdout_limit_bytes
    ):
        mismatches.append("stdout_truncated")
    if evidence.stderr_truncated != (
        evidence.stderr_total_bytes > policy.stderr_limit_bytes
    ):
        mismatches.append("stderr_truncated")
    return tuple(sorted(set(mismatches)))


def validation_retry_feedback(
    evidence: TaskValidationExecutionEvidence,
) -> str:
    """Bound prior output as explicitly untrusted Task Agent repair context."""

    diagnostics = evidence.retained_stderr or evidence.retained_stdout or "None retained."
    return (
        "Untrusted validation diagnostics from the previous governed execution "
        f"({evidence.profile.value}, {evidence.outcome.value}, "
        f"exit_code={evidence.exit_code}): {diagnostics}"
    )


def required_validation_execution_status(
    graph: TaskGraph | None,
    execution: TaskGraphExecutionState | None,
    *,
    run_id: str | None = None,
    bound_requests: tuple[WorkspaceBoundTaskExecutionRequest, ...] = (),
    evidence: tuple[TaskValidationExecutionEvidence, ...] = (),
    snapshots: tuple[WorkspaceSnapshot, ...] = (),
    change_sets: tuple[WorkspaceChangeSet, ...] = (),
    exit_decisions: tuple[TaskAttemptExitDecision, ...] = (),
) -> RequiredValidationExecutionStatus:
    """Verify exact final-attempt required evidence and staged input identities."""

    if graph is None:
        return RequiredValidationExecutionStatus(
            required=False,
            required_count=0,
            verified_count=0,
            verified=False,
        )
    requirements = tuple(
        (task, requirement)
        for task in graph.tasks
        for requirement in task.required_validations
    )
    if not requirements:
        return RequiredValidationExecutionStatus(
            required=False,
            required_count=0,
            verified_count=0,
            verified=False,
        )
    if execution is None or run_id is None:
        return RequiredValidationExecutionStatus(
            required=True,
            required_count=len(requirements),
            verified_count=0,
            verified=False,
        )

    states = {item.task_id: item for item in execution.task_states}
    snapshots_by_id = {item.snapshot_id: item for item in snapshots}
    verified_count = 0
    for task, requirement in requirements:
        runtime = states.get(task.task_id)
        if runtime is None or runtime.status is not TaskExecutionStatus.SUCCEEDED:
            continue
        matching_bound = tuple(
            item
            for item in bound_requests
            if item.task_id == task.task_id
            and item.attempt_number == runtime.attempt_count
        )
        if len(matching_bound) != 1:
            continue
        bound = matching_bound[0]
        source_snapshot = snapshots_by_id.get(
            bound.workspace_binding.snapshot_id
        )
        if (
            source_snapshot is None
            or source_snapshot.workspace_id
            != bound.workspace_binding.workspace_id
            or not workspace_snapshot_identity_is_valid(source_snapshot)
        ):
            continue
        staged_workspace_id = (
            "VALIDATION-WORKSPACE-"
            f"{bound.attempt_id}-{requirement.requirement_id}"
        )
        matching_change_sets = tuple(
            item
            for item in change_sets
            if item.task_id == task.task_id
            and item.request_id == bound.request_id
            and item.attempt_id == bound.attempt_id
            and item.attempt_number == bound.attempt_number
        )
        if any(
            item.workspace_id != source_snapshot.workspace_id
            or item.base_snapshot_id != source_snapshot.snapshot_id
            or not workspace_change_set_identity_is_valid(item)
            for item in matching_change_sets
        ):
            continue
        staged_snapshot_id = _expected_staged_snapshot_id(
            staged_workspace_id,
            source_snapshot,
            matching_change_sets,
        )
        if staged_snapshot_id is None:
            continue
        request = build_validation_execution_request(
            run_id=run_id,
            graph_id=graph.graph_id,
            graph_version=graph.version,
            task_request=bound.request,
            requirement=requirement,
            source_workspace_id=source_snapshot.workspace_id,
            source_snapshot_id=source_snapshot.snapshot_id,
            staged_workspace_id=staged_workspace_id,
            staged_snapshot_id=staged_snapshot_id,
        )
        matching_evidence = tuple(
            item
            for item in evidence
            if item.task_id == task.task_id
            and item.request_id == bound.request_id
            and item.attempt_id == bound.attempt_id
            and item.attempt_number == bound.attempt_number
            and item.validation_requirement_id == requirement.requirement_id
        )
        if len(matching_evidence) != 1:
            continue
        item = matching_evidence[0]
        policy = python_compile_validation_policy()
        if validation_execution_evidence_errors(request, policy, item):
            continue
        matching_exit = tuple(
            decision
            for decision in exit_decisions
            if decision.task_id == task.task_id
            and decision.attempt_number == bound.attempt_number
            and decision.request_id == bound.request_id
            and decision.attempt_id == bound.attempt_id
            and decision.disposition is TaskAttemptExitDisposition.SUCCEED_TASK
            and item.evidence_id in decision.evidence_ids
        )
        if len(matching_exit) != 1 or not item.passed:
            continue
        verified_count += 1
    return RequiredValidationExecutionStatus(
        required=True,
        required_count=len(requirements),
        verified_count=verified_count,
        verified=verified_count == len(requirements),
    )


def _expected_staged_snapshot_id(
    staged_workspace_id: str,
    source_snapshot: WorkspaceSnapshot,
    matching_change_sets: tuple[WorkspaceChangeSet, ...],
) -> str | None:
    if len(matching_change_sets) > 1:
        return None
    hashes = {item.path: item.content_hash for item in source_snapshot.files}
    if matching_change_sets:
        for change in matching_change_sets[0].file_changes:
            hashes[change.path] = change.desired_content_hash
    expected = build_workspace_snapshot(
        staged_workspace_id,
        tuple(
            WorkspaceFileState(path=path, content_hash=content_hash)
            for path, content_hash in sorted(hashes.items())
        ),
    )
    return expected.snapshot_id


def _content_hash(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.value if isinstance(item, StrEnum) else str(item),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
