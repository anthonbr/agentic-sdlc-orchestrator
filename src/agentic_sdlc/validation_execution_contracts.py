"""Immutable contracts for governed task-validation execution and evidence."""

from __future__ import annotations

import hashlib
import json
import sys
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.project_delivery import (
    ProjectDeliveryMode,
    ProjectDeliveryPolicy,
)
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
PYTHON_PYTEST_POLICY_VERSION = "python-pytest-docker-v2"
PYTHON_PYTEST_IMAGE = "python:3.12-slim"
PUBLIC_PYPI_INDEX_URL = "https://pypi.org/simple"
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 30.0
DEFAULT_PYTEST_TIMEOUT_SECONDS = 120.0
DEFAULT_PROVISIONING_TIMEOUT_SECONDS = 120.0
DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES = 16 * 1024
DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS = 2.0
FINAL_WORKSPACE_VALIDATION_TASK_ID = "TASK-000"


class ValidationExecutionEnvironmentKind(StrEnum):
    """Application-owned execution-environment families."""

    LOCAL_DISPOSABLE = "LOCAL_DISPOSABLE"
    DOCKER_DISPOSABLE = "DOCKER_DISPOSABLE"


class ValidationDependencyProvisioning(StrEnum):
    """Whether an execution environment contains governed dependencies."""

    NONE = "NONE"
    PIP = "PIP"


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
    provisioning_argv_prefix: tuple[str, ...] = ()
    provisioning_timeout_seconds: float | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(gt=0)
    stdout_limit_bytes: int = Field(gt=0)
    stderr_limit_bytes: int = Field(gt=0)
    termination_grace_seconds: float = Field(gt=0)
    working_directory: str = "."
    environment_variable_names: tuple[str, ...]
    environment_kind: ValidationExecutionEnvironmentKind
    dependency_provisioning: ValidationDependencyProvisioning
    network_access_allowed: bool
    container_image_reference: str | None = None


class PythonDependencyManifest(BaseModel):
    """Normalized governed dependency input from one exact staged postimage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_id: str = Field(min_length=1)
    staged_workspace_id: str = Field(min_length=1)
    staged_snapshot_id: str = Field(min_length=1)
    source_path: str = "pyproject.toml"
    source_present: bool
    source_sha256: str
    normalized_dependencies: tuple[str, ...]

    @field_validator("source_sha256")
    @classmethod
    def _validate_source_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("Dependency manifest hashes must be lowercase SHA-256.")
        return value


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
    dependency_manifest: PythonDependencyManifest | None = None


class TaskValidationProvisioningEvidence(BaseModel):
    """Immutable evidence for application-owned disposable pip provisioning."""

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
    dependency_manifest_id: str
    dependency_manifest_source_present: bool
    dependency_manifest_source_sha256: str
    normalized_dependencies: tuple[str, ...]
    container_image_reference: str
    container_image_id: str
    container_id: str
    image_pulled: bool
    argv: tuple[str, ...] = Field(min_length=1)
    package_index_url: str
    network_access_allowed: bool
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
    container_cleanup_succeeded: bool
    passed: bool

    @field_validator(
        "dependency_manifest_source_sha256", "stdout_sha256", "stderr_sha256"
    )
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("Provisioning hashes must be lowercase SHA-256.")
        return value

    @model_validator(mode="after")
    def _validate_outcome_flags(self) -> TaskValidationProvisioningEvidence:
        expected_timed_out = self.outcome is ValidationExecutionOutcome.TIMED_OUT
        expected_passed = (
            self.outcome is ValidationExecutionOutcome.PASSED
            and self.exit_code == 0
            and not expected_timed_out
            and self.container_cleanup_succeeded
        )
        if self.outcome is ValidationExecutionOutcome.PASSED and self.exit_code != 0:
            raise ValueError("Passed provisioning evidence requires exit code zero.")
        if self.outcome is ValidationExecutionOutcome.FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("Failed provisioning evidence requires non-zero exit code.")
        if self.timed_out != expected_timed_out:
            raise ValueError("Provisioning timeout flag does not match outcome.")
        if self.passed != expected_passed:
            raise ValueError("Provisioning pass judgment does not match outcome.")
        return self


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
    container_image_reference: str | None = None
    container_image_id: str | None = None
    container_id: str | None = None
    external_network_disconnected: bool | None = None
    container_cleanup_succeeded: bool | None = None
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


class GovernedValidationExecutionReport(BaseModel):
    """One backend result without creating a second persisted evidence system."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provisioning_evidence: tuple[TaskValidationProvisioningEvidence, ...] = ()
    execution_evidence: TaskValidationExecutionEvidence | None = None


class RequiredValidationExecutionStatus(BaseModel):
    """Deterministic final-state judgment without collapsing NOT_REQUIRED."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required: bool
    required_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    verified: bool
    python_compile_verified_count: int = Field(default=0, ge=0)
    python_pytest_verified_count: int = Field(default=0, ge=0)
    dependency_provisioning_verified_count: int = Field(default=0, ge=0)


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
        "provisioning_argv_prefix": (),
        "provisioning_timeout_seconds": None,
        "timeout_seconds": DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        "stdout_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "stderr_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "termination_grace_seconds": DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        "working_directory": ".",
        "environment_variable_names": ("HOME", "LANG", "LC_ALL", "TMPDIR"),
        "environment_kind": ValidationExecutionEnvironmentKind.LOCAL_DISPOSABLE.value,
        "dependency_provisioning": ValidationDependencyProvisioning.NONE.value,
        "network_access_allowed": False,
        "container_image_reference": None,
    }
    digest = _content_hash(payload)[:16].upper()
    return GovernedValidationPolicy(
        policy_id=f"VALIDATION-POLICY-{digest}",
        policy_version=PYTHON_COMPILE_POLICY_VERSION,
        profile=ValidationExecutionProfile.PYTHON_COMPILE,
        argv=argv,
        provisioning_argv_prefix=(),
        provisioning_timeout_seconds=None,
        timeout_seconds=DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        stdout_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        stderr_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        termination_grace_seconds=DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        working_directory=".",
        environment_variable_names=("HOME", "LANG", "LC_ALL", "TMPDIR"),
        environment_kind=ValidationExecutionEnvironmentKind.LOCAL_DISPOSABLE,
        dependency_provisioning=ValidationDependencyProvisioning.NONE,
        network_access_allowed=False,
        container_image_reference=None,
    )


def python_pytest_validation_policy() -> GovernedValidationPolicy:
    """Resolve Docker pytest to fixed application-owned execution authority."""

    argv = ("python", "-m", "pytest", "-q", "tests")
    provisioning_argv_prefix = (
        "python",
        "-m",
        "pip",
        "install",
        "--user",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--index-url",
        PUBLIC_PYPI_INDEX_URL,
        "pytest",
    )
    payload = {
        "policy_version": PYTHON_PYTEST_POLICY_VERSION,
        "profile": ValidationExecutionProfile.PYTHON_PYTEST.value,
        "argv": argv,
        "provisioning_argv_prefix": provisioning_argv_prefix,
        "provisioning_timeout_seconds": DEFAULT_PROVISIONING_TIMEOUT_SECONDS,
        "timeout_seconds": DEFAULT_PYTEST_TIMEOUT_SECONDS,
        "stdout_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "stderr_limit_bytes": DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        "termination_grace_seconds": DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        "working_directory": "/work",
        "environment_variable_names": (
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTHONPATH",
        ),
        "environment_kind": ValidationExecutionEnvironmentKind.DOCKER_DISPOSABLE.value,
        "dependency_provisioning": ValidationDependencyProvisioning.PIP.value,
        # V0.14 attempts best-effort bridge disconnection, but the policy remains
        # truthful when a Docker installation cannot provide that optional step.
        "network_access_allowed": True,
        "container_image_reference": PYTHON_PYTEST_IMAGE,
    }
    digest = _content_hash(payload)[:16].upper()
    return GovernedValidationPolicy(
        policy_id=f"VALIDATION-POLICY-{digest}",
        policy_version=PYTHON_PYTEST_POLICY_VERSION,
        profile=ValidationExecutionProfile.PYTHON_PYTEST,
        argv=argv,
        provisioning_argv_prefix=provisioning_argv_prefix,
        provisioning_timeout_seconds=DEFAULT_PROVISIONING_TIMEOUT_SECONDS,
        timeout_seconds=DEFAULT_PYTEST_TIMEOUT_SECONDS,
        stdout_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        stderr_limit_bytes=DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
        termination_grace_seconds=DEFAULT_VALIDATION_TERMINATION_GRACE_SECONDS,
        working_directory="/work",
        environment_variable_names=(
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTHONPATH",
        ),
        environment_kind=ValidationExecutionEnvironmentKind.DOCKER_DISPOSABLE,
        dependency_provisioning=ValidationDependencyProvisioning.PIP,
        network_access_allowed=True,
        container_image_reference=PYTHON_PYTEST_IMAGE,
    )


def resolve_governed_validation_policy(
    profile: ValidationExecutionProfile,
) -> GovernedValidationPolicy:
    """Resolve approved profile authority independently of any execution backend."""

    if profile is ValidationExecutionProfile.PYTHON_COMPILE:
        return python_compile_validation_policy()
    if profile is ValidationExecutionProfile.PYTHON_PYTEST:
        return python_pytest_validation_policy()
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
    dependency_manifest: PythonDependencyManifest | None = None,
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
        dependency_manifest=dependency_manifest,
    )


def final_workspace_validation_requirements(
    delivery_policy: ProjectDeliveryPolicy,
    authoritative_snapshot: WorkspaceSnapshot,
) -> tuple[TaskValidationRequirement, ...]:
    """Derive application-required Python validation from final project content."""

    if delivery_policy.mode is not ProjectDeliveryMode.RUNNABLE_PROJECT:
        return ()
    paths = tuple(PurePosixPath(item.path) for item in authoritative_snapshot.files)
    profiles: list[ValidationExecutionProfile] = []
    if any(path.suffix == ".py" for path in paths):
        profiles.append(ValidationExecutionProfile.PYTHON_COMPILE)
    if any(
        len(path.parts) > 1
        and path.parts[0] == "tests"
        and path.suffix == ".py"
        for path in paths
    ):
        profiles.append(ValidationExecutionProfile.PYTHON_PYTEST)
    return tuple(
        TaskValidationRequirement(
            requirement_id=(
                f"{FINAL_WORKSPACE_VALIDATION_TASK_ID}-VALIDATION-{index:03d}"
            ),
            profile=profile,
        )
        for index, profile in enumerate(profiles, start=1)
    )


def final_workspace_validation_staged_workspace_id(
    *,
    run_id: str,
    graph: TaskGraph,
    authoritative_snapshot: WorkspaceSnapshot,
) -> str:
    """Return the stable disposable-workspace identity for one final snapshot."""

    payload = {
        "run_id": run_id,
        "graph_id": graph.graph_id,
        "graph_version": graph.version,
        "source_workspace_id": authoritative_snapshot.workspace_id,
        "source_snapshot_id": authoritative_snapshot.snapshot_id,
    }
    return "FINAL-VALIDATION-WORKSPACE-" + _content_hash(payload)[:20].upper()


def build_final_workspace_validation_request(
    *,
    run_id: str,
    graph: TaskGraph,
    delivery_policy: ProjectDeliveryPolicy,
    authoritative_snapshot: WorkspaceSnapshot,
    staged_snapshot: WorkspaceSnapshot,
    requirement: TaskValidationRequirement,
    dependency_manifest: PythonDependencyManifest | None = None,
) -> ValidationExecutionRequest:
    """Bind deterministic application validation to the exact final snapshot."""

    requirements = final_workspace_validation_requirements(
        delivery_policy, authoritative_snapshot
    )
    if requirement not in requirements:
        raise ValidationExecutionContractError(
            "Final validation requirement is not authorized by delivery policy."
        )
    staged_workspace_id = final_workspace_validation_staged_workspace_id(
        run_id=run_id,
        graph=graph,
        authoritative_snapshot=authoritative_snapshot,
    )
    expected_staged_snapshot = build_workspace_snapshot(
        staged_workspace_id, authoritative_snapshot.files
    )
    if staged_snapshot != expected_staged_snapshot:
        raise ValidationExecutionContractError(
            "Final validation staging does not match the authoritative snapshot."
        )
    binding = {
        "run_id": run_id,
        "graph_id": graph.graph_id,
        "graph_version": graph.version,
        "source_workspace_id": authoritative_snapshot.workspace_id,
        "source_snapshot_id": authoritative_snapshot.snapshot_id,
        "staged_workspace_id": staged_workspace_id,
        "staged_snapshot_id": staged_snapshot.snapshot_id,
    }
    digest = _content_hash(binding)[:20].upper()
    return ValidationExecutionRequest(
        run_id=run_id,
        graph_id=graph.graph_id,
        graph_version=graph.version,
        task_id=FINAL_WORKSPACE_VALIDATION_TASK_ID,
        request_id=f"FINAL-VALIDATION-REQUEST-{digest}",
        attempt_id=f"FINAL-VALIDATION-ATTEMPT-{digest}",
        attempt_number=1,
        requirement=requirement,
        source_workspace_id=authoritative_snapshot.workspace_id,
        source_snapshot_id=authoritative_snapshot.snapshot_id,
        staged_workspace_id=staged_workspace_id,
        staged_snapshot_id=staged_snapshot.snapshot_id,
        dependency_manifest=dependency_manifest,
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
    provisioning_evidence_ids: tuple[str, ...] = (),
    container_image_reference: str | None = None,
    container_image_id: str | None = None,
    container_id: str | None = None,
    external_network_disconnected: bool | None = None,
    container_cleanup_succeeded: bool | None = None,
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
        "provisioning_evidence_ids": provisioning_evidence_ids,
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
        "container_image_reference": container_image_reference,
        "container_image_id": container_image_id,
        "container_id": container_id,
        "external_network_disconnected": external_network_disconnected,
        "container_cleanup_succeeded": container_cleanup_succeeded,
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


def dependency_manifest_identity_is_valid(manifest: PythonDependencyManifest) -> bool:
    """Return whether normalized dependency authority retains its content identity."""

    payload = manifest.model_dump(mode="json", exclude={"manifest_id"})
    expected = "DEPENDENCY-MANIFEST-" + _content_hash(payload)[:20].upper()
    return manifest.manifest_id == expected


def build_validation_provisioning_evidence(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    *,
    container_image_id: str,
    container_id: str,
    image_pulled: bool,
    argv: tuple[str, ...],
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
    container_cleanup_succeeded: bool,
) -> TaskValidationProvisioningEvidence:
    """Create immutable pip-provisioning evidence for one exact staged attempt."""

    manifest = request.dependency_manifest
    if manifest is None or policy.container_image_reference is None:
        raise ValidationExecutionContractError(
            "Containerized provisioning requires governed manifest and image authority."
        )
    timed_out = outcome is ValidationExecutionOutcome.TIMED_OUT
    passed = (
        outcome is ValidationExecutionOutcome.PASSED
        and exit_code == 0
        and not timed_out
        and container_cleanup_succeeded
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
        "dependency_manifest_id": manifest.manifest_id,
        "dependency_manifest_source_present": manifest.source_present,
        "dependency_manifest_source_sha256": manifest.source_sha256,
        "normalized_dependencies": manifest.normalized_dependencies,
        "container_image_reference": policy.container_image_reference,
        "container_image_id": container_image_id,
        "container_id": container_id,
        "image_pulled": image_pulled,
        "argv": argv,
        "package_index_url": PUBLIC_PYPI_INDEX_URL,
        "network_access_allowed": True,
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
        "container_cleanup_succeeded": container_cleanup_succeeded,
        "passed": passed,
    }
    return TaskValidationProvisioningEvidence(
        evidence_id="PROVISIONING-EVIDENCE-" + _content_hash(values)[:20].upper(),
        **values,
    )


def validation_provisioning_evidence_identity_is_valid(
    evidence: TaskValidationProvisioningEvidence,
) -> bool:
    """Return whether provisioning evidence ID still binds every field."""

    payload = evidence.model_dump(mode="json", exclude={"evidence_id"})
    expected = "PROVISIONING-EVIDENCE-" + _content_hash(payload)[:20].upper()
    return evidence.evidence_id == expected


def validation_provisioning_evidence_errors(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    evidence: TaskValidationProvisioningEvidence,
) -> tuple[str, ...]:
    """Return provisioning lineage, policy, and identity mismatches."""

    manifest = request.dependency_manifest
    if manifest is None:
        return ("dependency_manifest",)
    expected_argv = (*policy.provisioning_argv_prefix, *manifest.normalized_dependencies)
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
        "dependency_manifest_id": manifest.manifest_id,
        "dependency_manifest_source_present": manifest.source_present,
        "dependency_manifest_source_sha256": manifest.source_sha256,
        "normalized_dependencies": manifest.normalized_dependencies,
        "container_image_reference": policy.container_image_reference,
        "argv": expected_argv,
        "package_index_url": PUBLIC_PYPI_INDEX_URL,
        "network_access_allowed": True,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(evidence, name) != value
    ]
    if policy.profile is not request.requirement.profile:
        mismatches.append("policy.profile")
    if not dependency_manifest_identity_is_valid(manifest):
        mismatches.append("dependency_manifest.manifest_id")
    if not validation_provisioning_evidence_identity_is_valid(evidence):
        mismatches.append("evidence_id")
    if not evidence.container_image_id or not evidence.container_id:
        mismatches.append("container_identity")
    if not evidence.container_cleanup_succeeded:
        mismatches.append("container_cleanup_succeeded")
    if evidence.stdout_truncated != (
        evidence.stdout_total_bytes > policy.stdout_limit_bytes
    ):
        mismatches.append("stdout_truncated")
    if evidence.stderr_truncated != (
        evidence.stderr_total_bytes > policy.stderr_limit_bytes
    ):
        mismatches.append("stderr_truncated")
    return tuple(sorted(set(mismatches)))


def validation_execution_evidence_errors(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    evidence: TaskValidationExecutionEvidence,
    provisioning_evidence: tuple[TaskValidationProvisioningEvidence, ...] = (),
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
    if request.requirement.profile is ValidationExecutionProfile.PYTHON_COMPILE:
        if evidence.provisioning_evidence_ids:
            mismatches.append("provisioning_evidence_ids")
        if any(
            value is not None
            for value in (
                evidence.container_image_reference,
                evidence.container_image_id,
                evidence.container_id,
                evidence.external_network_disconnected,
                evidence.container_cleanup_succeeded,
            )
        ):
            mismatches.append("container_execution")
    elif request.requirement.profile is ValidationExecutionProfile.PYTHON_PYTEST:
        expected_ids = tuple(item.evidence_id for item in provisioning_evidence)
        if evidence.provisioning_evidence_ids != expected_ids or len(expected_ids) != 1:
            mismatches.append("provisioning_evidence_ids")
        if len(provisioning_evidence) == 1:
            provision = provisioning_evidence[0]
            if validation_provisioning_evidence_errors(request, policy, provision):
                mismatches.append("provisioning_evidence")
            if not provision.passed:
                mismatches.append("provisioning_passed")
            if evidence.container_image_id != provision.container_image_id:
                mismatches.append("container_image_id")
            if evidence.container_id != provision.container_id:
                mismatches.append("container_id")
        if evidence.container_image_reference != policy.container_image_reference:
            mismatches.append("container_image_reference")
        if not evidence.container_image_id or not evidence.container_id:
            mismatches.append("container_identity")
        if evidence.container_cleanup_succeeded is not True:
            mismatches.append("container_cleanup_succeeded")
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


def validation_provisioning_retry_feedback(
    evidence: TaskValidationProvisioningEvidence,
) -> str:
    """Bound pip output as explicitly untrusted Task Agent repair context."""

    diagnostics = evidence.retained_stderr or evidence.retained_stdout or "None retained."
    return (
        "Untrusted dependency-provisioning diagnostics from the previous governed "
        f"execution ({evidence.profile.value}, {evidence.outcome.value}, "
        f"exit_code={evidence.exit_code}): {diagnostics}"
    )


def required_validation_execution_status(
    graph: TaskGraph | None,
    execution: TaskGraphExecutionState | None,
    *,
    run_id: str | None = None,
    bound_requests: tuple[WorkspaceBoundTaskExecutionRequest, ...] = (),
    evidence: tuple[TaskValidationExecutionEvidence, ...] = (),
    provisioning_evidence: tuple[TaskValidationProvisioningEvidence, ...] = (),
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
    python_compile_verified_count = 0
    python_pytest_verified_count = 0
    dependency_provisioning_verified_count = 0
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
        matching_provisioning = tuple(
            candidate
            for candidate in provisioning_evidence
            if candidate.evidence_id in item.provisioning_evidence_ids
        )
        dependency_manifest: PythonDependencyManifest | None = None
        if requirement.profile is ValidationExecutionProfile.PYTHON_PYTEST:
            if len(matching_provisioning) != 1:
                continue
            provision = matching_provisioning[0]
            dependency_manifest = PythonDependencyManifest(
                manifest_id=provision.dependency_manifest_id,
                staged_workspace_id=provision.staged_workspace_id,
                staged_snapshot_id=provision.staged_snapshot_id,
                source_path="pyproject.toml",
                source_present=provision.dependency_manifest_source_present,
                source_sha256=provision.dependency_manifest_source_sha256,
                normalized_dependencies=provision.normalized_dependencies,
            )
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
            dependency_manifest=dependency_manifest,
        )
        policy = resolve_governed_validation_policy(requirement.profile)
        if validation_execution_evidence_errors(
            request, policy, item, matching_provisioning
        ):
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
            and all(
                provision.evidence_id in decision.evidence_ids
                for provision in matching_provisioning
            )
        )
        if len(matching_exit) != 1 or not item.passed:
            continue
        verified_count += 1
        if requirement.profile is ValidationExecutionProfile.PYTHON_COMPILE:
            python_compile_verified_count += 1
        elif requirement.profile is ValidationExecutionProfile.PYTHON_PYTEST:
            python_pytest_verified_count += 1
            dependency_provisioning_verified_count += 1
    return RequiredValidationExecutionStatus(
        required=True,
        required_count=len(requirements),
        verified_count=verified_count,
        verified=verified_count == len(requirements),
        python_compile_verified_count=python_compile_verified_count,
        python_pytest_verified_count=python_pytest_verified_count,
        dependency_provisioning_verified_count=(
            dependency_provisioning_verified_count
        ),
    )


def final_workspace_validation_execution_status(
    delivery_policy: ProjectDeliveryPolicy,
    *,
    run_id: str | None,
    graph: TaskGraph | None,
    authoritative_snapshot: WorkspaceSnapshot | None,
    evidence: tuple[TaskValidationExecutionEvidence, ...] = (),
    provisioning_evidence: tuple[TaskValidationProvisioningEvidence, ...] = (),
) -> RequiredValidationExecutionStatus:
    """Verify application-required evidence for the exact publishable snapshot."""

    if authoritative_snapshot is None:
        requirements: tuple[TaskValidationRequirement, ...] = ()
    else:
        requirements = final_workspace_validation_requirements(
            delivery_policy, authoritative_snapshot
        )
    if not requirements:
        return RequiredValidationExecutionStatus(
            required=False,
            required_count=0,
            verified_count=0,
            verified=False,
        )
    if (
        run_id is None
        or graph is None
        or authoritative_snapshot is None
        or not workspace_snapshot_identity_is_valid(authoritative_snapshot)
    ):
        return RequiredValidationExecutionStatus(
            required=True,
            required_count=len(requirements),
            verified_count=0,
            verified=False,
        )

    staged_workspace_id = final_workspace_validation_staged_workspace_id(
        run_id=run_id,
        graph=graph,
        authoritative_snapshot=authoritative_snapshot,
    )
    staged_snapshot = build_workspace_snapshot(
        staged_workspace_id, authoritative_snapshot.files
    )
    verified_count = 0
    compile_count = 0
    pytest_count = 0
    provisioning_count = 0
    for requirement in requirements:
        matching_evidence = tuple(
            item
            for item in evidence
            if item.task_id == FINAL_WORKSPACE_VALIDATION_TASK_ID
            and item.validation_requirement_id == requirement.requirement_id
            and item.profile is requirement.profile
        )
        if len(matching_evidence) != 1:
            continue
        item = matching_evidence[0]
        matching_provisioning = tuple(
            candidate
            for candidate in provisioning_evidence
            if candidate.evidence_id in item.provisioning_evidence_ids
        )
        dependency_manifest: PythonDependencyManifest | None = None
        if requirement.profile is ValidationExecutionProfile.PYTHON_PYTEST:
            if len(matching_provisioning) != 1:
                continue
            provision = matching_provisioning[0]
            dependency_manifest = PythonDependencyManifest(
                manifest_id=provision.dependency_manifest_id,
                staged_workspace_id=provision.staged_workspace_id,
                staged_snapshot_id=provision.staged_snapshot_id,
                source_path="pyproject.toml",
                source_present=provision.dependency_manifest_source_present,
                source_sha256=provision.dependency_manifest_source_sha256,
                normalized_dependencies=provision.normalized_dependencies,
            )
        try:
            request = build_final_workspace_validation_request(
                run_id=run_id,
                graph=graph,
                delivery_policy=delivery_policy,
                authoritative_snapshot=authoritative_snapshot,
                staged_snapshot=staged_snapshot,
                requirement=requirement,
                dependency_manifest=dependency_manifest,
            )
            policy = resolve_governed_validation_policy(requirement.profile)
        except ValidationExecutionContractError:
            continue
        if validation_execution_evidence_errors(
            request, policy, item, matching_provisioning
        ) or not item.passed:
            continue
        verified_count += 1
        if requirement.profile is ValidationExecutionProfile.PYTHON_COMPILE:
            compile_count += 1
        else:
            pytest_count += 1
            provisioning_count += 1
    return RequiredValidationExecutionStatus(
        required=True,
        required_count=len(requirements),
        verified_count=verified_count,
        verified=verified_count == len(requirements),
        python_compile_verified_count=compile_count,
        python_pytest_verified_count=pytest_count,
        dependency_provisioning_verified_count=provisioning_count,
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
