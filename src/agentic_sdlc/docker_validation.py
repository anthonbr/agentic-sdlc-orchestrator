"""Application-owned Docker backend for the fixed PYTHON_PYTEST profile."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Protocol

from agentic_sdlc.task_graph import ValidationExecutionProfile
from agentic_sdlc.validation_execution import (
    ValidationExecutionInfrastructureCode,
    ValidationExecutionInfrastructureError,
    _BoundedStreamCapture,
    _CapturedOutput,
    _terminate_process_group,
)
from agentic_sdlc.validation_execution_contracts import (
    GovernedValidationExecutionReport,
    GovernedValidationPolicy,
    PythonDependencyManifest,
    TaskValidationProvisioningEvidence,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    build_validation_execution_evidence,
    build_validation_provisioning_evidence,
    dependency_manifest_identity_is_valid,
    python_pytest_validation_policy,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    read_isolated_workspace_file,
    snapshot_isolated_workspace,
)


_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_REQUIREMENT_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?"
)
_FORBIDDEN_REQUIREMENT_FRAGMENTS = (
    "://",
    "git+",
    "hg+",
    "svn+",
    "bzr+",
    "file:",
    "--index-url",
    "--extra-index-url",
    "--find-links",
)


class ValidationDependencyManifestError(ValueError):
    """Candidate dependency metadata is invalid but may be repaired by the agent."""


@dataclass(frozen=True, slots=True)
class DockerCommandResult:
    """Bounded observation from one application-owned Docker CLI invocation."""

    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: _CapturedOutput
    stderr: _CapturedOutput


class DockerCommandRunner(Protocol):
    """Narrow runner for argv generated solely by the Docker validation backend."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        termination_grace_seconds: float,
    ) -> DockerCommandResult:
        """Run one deterministic Docker CLI argv with bounded output."""


class DockerCliCommandRunner:
    """Execute Docker CLI argv without shell or host environment inheritance."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        termination_grace_seconds: float,
    ) -> DockerCommandResult:
        stdout_capture = _BoundedStreamCapture(output_limit_bytes)
        stderr_capture = _BoundedStreamCapture(output_limit_bytes)
        with tempfile.TemporaryDirectory(prefix="agentic-sdlc-docker-config-") as config:
            environment = {
                "DOCKER_CONFIG": config,
                "HOME": config,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
            }
            try:
                process = subprocess.Popen(
                    argv,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            except (OSError, ValueError) as error:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.PROCESS_START,
                    "Application-owned Docker CLI process could not start.",
                ) from error
            if process.stdout is None or process.stderr is None:
                _terminate_process_group(process, termination_grace_seconds)
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                    "Docker CLI did not expose bounded output streams.",
                )
            stdout_thread = Thread(
                target=stdout_capture.consume,
                args=(process.stdout,),
                name="docker-validation-stdout",
                daemon=True,
            )
            stderr_thread = Thread(
                target=stderr_capture.consume,
                args=(process.stderr,),
                name="docker-validation-stderr",
                daemon=True,
            )
            stdout_started = False
            stderr_started = False
            try:
                stdout_thread.start()
                stdout_started = True
                stderr_thread.start()
                stderr_started = True
            except RuntimeError as error:
                _terminate_process_group(process, termination_grace_seconds)
                if stdout_started:
                    stdout_thread.join(timeout=termination_grace_seconds)
                if stderr_started:
                    stderr_thread.join(timeout=termination_grace_seconds)
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                    "Docker CLI output capture could not start.",
                ) from error
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process, termination_grace_seconds)
            except OSError as error:
                _terminate_process_group(process, termination_grace_seconds)
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.PROCESS_TERMINATION,
                    "Docker CLI process state could not be observed.",
                ) from error
            stdout_thread.join(timeout=termination_grace_seconds)
            stderr_thread.join(timeout=termination_grace_seconds)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                    "Docker CLI output capture did not terminate reliably.",
                )
            if stdout_capture.error is not None or stderr_capture.error is not None:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                    "Docker CLI output could not be captured reliably.",
                )
            return DockerCommandResult(
                argv=argv,
                exit_code=process.returncode,
                timed_out=timed_out,
                stdout=stdout_capture.result(),
                stderr=stderr_capture.result(),
            )


def build_python_dependency_manifest(
    workspace: IsolatedWorkspace,
    *,
    staged_snapshot_id: str,
) -> PythonDependencyManifest:
    """Normalize governed PEP 621 dependencies from the exact staged postimage."""

    try:
        before = snapshot_isolated_workspace(workspace)
        if before.snapshot_id != staged_snapshot_id:
            raise ValidationDependencyManifestError(
                "Dependency metadata does not belong to the authorized staged snapshot."
            )
        contents = read_isolated_workspace_file(workspace, "pyproject.toml")
        after = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Governed dependency metadata could not be read safely.",
        ) from error
    if before != after:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Staged candidate changed while dependency metadata was read.",
        )
    source_present = contents is not None
    source = contents or b""
    dependencies: tuple[str, ...] = ()
    if contents is not None:
        try:
            document = tomllib.loads(contents.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValidationDependencyManifestError(
                "Staged pyproject.toml is not valid UTF-8 TOML."
            ) from error
        project = document.get("project", {})
        if not isinstance(project, dict):
            raise ValidationDependencyManifestError(
                "Staged pyproject.toml [project] must be a table."
            )
        raw_dependencies = project.get("dependencies", [])
        if not isinstance(raw_dependencies, list) or not all(
            isinstance(item, str) for item in raw_dependencies
        ):
            raise ValidationDependencyManifestError(
                "Staged [project].dependencies must be an array of strings."
            )
        dependencies = tuple(
            sorted(
                {_validate_dependency_requirement(item) for item in raw_dependencies},
                key=str.casefold,
            )
        )
    values = {
        "staged_workspace_id": workspace.workspace_id,
        "staged_snapshot_id": staged_snapshot_id,
        "source_path": "pyproject.toml",
        "source_present": source_present,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "normalized_dependencies": dependencies,
    }
    manifest = PythonDependencyManifest(
        manifest_id="DEPENDENCY-MANIFEST-" + _content_hash(values)[:20].upper(),
        **values,
    )
    if not dependency_manifest_identity_is_valid(manifest):
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Normalized dependency manifest identity is inconsistent.",
        )
    return manifest


class DockerPytestValidationExecutor:
    """Provision and run fixed pytest inside one disposable Docker container."""

    def __init__(
        self,
        *,
        runner: DockerCommandRunner | None = None,
        docker_executable: str | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runner = runner or DockerCliCommandRunner()
        self._docker_executable = (
            docker_executable
            if runner is not None
            else _trusted_docker_executable(docker_executable)
        )
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock

    def execute(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        workspace: IsolatedWorkspace,
    ) -> GovernedValidationExecutionReport:
        expected_policy = python_pytest_validation_policy()
        if (
            request.requirement.profile is not ValidationExecutionProfile.PYTHON_PYTEST
            or policy != expected_policy
            or request.dependency_manifest is None
        ):
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE,
                "Docker backend received non-authoritative pytest policy or metadata.",
            )
        observed = _verified_staged_snapshot(workspace, request)
        manifest = request.dependency_manifest
        if (
            manifest.staged_workspace_id != request.staged_workspace_id
            or manifest.staged_snapshot_id != request.staged_snapshot_id
            or not dependency_manifest_identity_is_valid(manifest)
        ):
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Governed dependency manifest does not match the staged postimage.",
            )

        self._require_success(
            self._command(("version", "--format", "{{.Server.Version}}"), policy),
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Docker daemon is unavailable.",
        )
        image_pulled, image_id = self._ensure_image(policy)
        container_id: str | None = None
        cleaned = False
        cleanup_attempted = False
        provision_result: DockerCommandResult | None = None
        pytest_result: DockerCommandResult | None = None
        provision_started_at = ""
        provision_ended_at = ""
        provision_duration = 0.0
        pytest_started_at = ""
        pytest_ended_at = ""
        pytest_duration = 0.0
        network_disconnected = False
        try:
            container_id = self._create_container(request, policy)
            self._require_success(
                self._command(("start", container_id), policy),
                ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
                "Disposable Docker container could not start.",
            )
            self._require_success(
                self._command(
                    ("cp", f"{workspace.root}/.", f"{container_id}:/work"),
                    policy,
                ),
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Staged candidate could not be copied into Docker.",
            )
            if snapshot_isolated_workspace(workspace) != observed:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                    "Staged candidate changed while Docker input was copied.",
                )

            pip_argv = (
                *policy.provisioning_argv_prefix,
                *manifest.normalized_dependencies,
            )
            provision_started_at = self._wall_clock().isoformat()
            provision_start = self._monotonic_clock()
            provision_result = self._command(
                (
                    "exec",
                    "-w",
                    "/work",
                    "-e",
                    "HOME=/tmp/agentic-sdlc-home",
                    "-e",
                    "PIP_CONFIG_FILE=/dev/null",
                    "-e",
                    "PIP_NO_INPUT=1",
                    container_id,
                    *pip_argv,
                ),
                policy,
                timeout_seconds=policy.provisioning_timeout_seconds,
            )
            provision_duration = max(
                0.0, self._monotonic_clock() - provision_start
            )
            provision_ended_at = self._wall_clock().isoformat()
            if provision_result.timed_out or provision_result.exit_code != 0:
                cleanup_attempted = True
                cleaned = self._cleanup_container(container_id, policy)
                provisioning = self._provisioning_evidence(
                    request,
                    policy,
                    image_id=image_id,
                    container_id=container_id,
                    image_pulled=image_pulled,
                    pip_argv=pip_argv,
                    result=provision_result,
                    started_at=provision_started_at,
                    ended_at=provision_ended_at,
                    duration_seconds=provision_duration,
                    cleaned=cleaned,
                )
                return GovernedValidationExecutionReport(
                    provisioning_evidence=(provisioning,),
                    execution_evidence=None,
                )

            disconnect = self._command(
                ("network", "disconnect", "bridge", container_id), policy
            )
            network_disconnected = (
                not disconnect.timed_out and disconnect.exit_code == 0
            )
            pytest_environment = [
                "-e",
                "HOME=/tmp/agentic-sdlc-home",
                "-e",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "-e",
                "PYTHONPATH=/work/src",
            ]
            pytest_started_at = self._wall_clock().isoformat()
            pytest_start = self._monotonic_clock()
            pytest_result = self._command(
                (
                    "exec",
                    "-w",
                    "/work",
                    *pytest_environment,
                    container_id,
                    *policy.argv,
                ),
                policy,
                timeout_seconds=policy.timeout_seconds,
            )
            pytest_duration = max(0.0, self._monotonic_clock() - pytest_start)
            pytest_ended_at = self._wall_clock().isoformat()
            cleanup_attempted = True
            cleaned = self._cleanup_container(container_id, policy)
            provisioning = self._provisioning_evidence(
                request,
                policy,
                image_id=image_id,
                container_id=container_id,
                image_pulled=image_pulled,
                pip_argv=pip_argv,
                result=provision_result,
                started_at=provision_started_at,
                ended_at=provision_ended_at,
                duration_seconds=provision_duration,
                cleaned=cleaned,
            )
            execution = build_validation_execution_evidence(
                request,
                policy,
                started_at=pytest_started_at,
                ended_at=pytest_ended_at,
                duration_seconds=pytest_duration,
                outcome=_command_outcome(pytest_result),
                exit_code=pytest_result.exit_code,
                stdout_total_bytes=pytest_result.stdout.total_bytes,
                stderr_total_bytes=pytest_result.stderr.total_bytes,
                retained_stdout=pytest_result.stdout.retained_text,
                retained_stderr=pytest_result.stderr.retained_text,
                stdout_sha256=pytest_result.stdout.sha256,
                stderr_sha256=pytest_result.stderr.sha256,
                stdout_truncated=pytest_result.stdout.truncated,
                stderr_truncated=pytest_result.stderr.truncated,
                provisioning_evidence_ids=(provisioning.evidence_id,),
                container_image_reference=policy.container_image_reference,
                container_image_id=image_id,
                container_id=container_id,
                external_network_disconnected=network_disconnected,
                container_cleanup_succeeded=cleaned,
            )
            return GovernedValidationExecutionReport(
                provisioning_evidence=(provisioning,),
                execution_evidence=execution,
            )
        except Exception:
            if container_id is not None and not cleanup_attempted:
                self._cleanup_container(container_id, policy)
            raise

    def _ensure_image(self, policy: GovernedValidationPolicy) -> tuple[bool, str]:
        image = policy.container_image_reference
        if image is None:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE,
                "Container policy does not specify an application-owned image.",
            )
        inspected = self._command(("image", "inspect", "--format", "{{.Id}}", image), policy)
        image_pulled = False
        if inspected.timed_out or inspected.exit_code != 0:
            self._require_success(
                self._command(("pull", image), policy, timeout_seconds=policy.provisioning_timeout_seconds),
                ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
                "Application-owned Docker image could not be pulled.",
            )
            image_pulled = True
            inspected = self._command(("image", "inspect", "--format", "{{.Id}}", image), policy)
        self._require_success(
            inspected,
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Application-owned Docker image identity is unavailable.",
        )
        image_id = inspected.stdout.retained_text.strip()
        if not image_id:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
                "Docker returned no image identity.",
            )
        return image_pulled, image_id

    def _create_container(
        self, request: ValidationExecutionRequest, policy: GovernedValidationPolicy
    ) -> str:
        image = policy.container_image_reference
        if image is None:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE,
                "Container image authority is missing.",
            )
        name_suffix = hashlib.sha256(
            f"{request.run_id}:{request.attempt_id}:{request.requirement.requirement_id}".encode()
        ).hexdigest()[:16]
        result = self._command(
            (
                "create",
                "--name",
                f"agentic-sdlc-validation-{name_suffix}",
                "--init",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "256",
                "--memory",
                "512m",
                "--network",
                "bridge",
                "--workdir",
                "/work",
                image,
                "python",
                "-c",
                "import time; time.sleep(86400)",
            ),
            policy,
        )
        self._require_success(
            result,
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Disposable Docker container could not be created.",
        )
        container_id = result.stdout.retained_text.strip()
        if not _CONTAINER_ID.fullmatch(container_id):
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
                "Docker returned an invalid container identity.",
            )
        return container_id

    def _cleanup_container(
        self, container_id: str, policy: GovernedValidationPolicy
    ) -> bool:
        removed = self._command(("rm", "-f", container_id), policy)
        if removed.timed_out or removed.exit_code != 0:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.CLEANUP,
                "Disposable Docker container could not be removed reliably.",
            )
        observed = self._command(("container", "inspect", container_id), policy)
        absent_message = (
            observed.stderr.retained_text + observed.stdout.retained_text
        ).lower()
        if observed.timed_out or observed.exit_code == 0 or not any(
            marker in absent_message
            for marker in ("no such object", "no such container")
        ):
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.CLEANUP,
                "Disposable Docker container absence could not be proven.",
            )
        return True

    def _provisioning_evidence(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        *,
        image_id: str,
        container_id: str,
        image_pulled: bool,
        pip_argv: tuple[str, ...],
        result: DockerCommandResult,
        started_at: str,
        ended_at: str,
        duration_seconds: float,
        cleaned: bool,
    ) -> TaskValidationProvisioningEvidence:
        return build_validation_provisioning_evidence(
            request,
            policy,
            container_image_id=image_id,
            container_id=container_id,
            image_pulled=image_pulled,
            argv=pip_argv,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            outcome=_command_outcome(result),
            exit_code=result.exit_code,
            stdout_total_bytes=result.stdout.total_bytes,
            stderr_total_bytes=result.stderr.total_bytes,
            retained_stdout=result.stdout.retained_text,
            retained_stderr=result.stderr.retained_text,
            stdout_sha256=result.stdout.sha256,
            stderr_sha256=result.stderr.sha256,
            stdout_truncated=result.stdout.truncated,
            stderr_truncated=result.stderr.truncated,
            container_cleanup_succeeded=cleaned,
        )

    def _command(
        self,
        arguments: tuple[str, ...],
        policy: GovernedValidationPolicy,
        *,
        timeout_seconds: float | None = None,
    ) -> DockerCommandResult:
        return self._runner.run(
            (self._docker_executable, *arguments),
            timeout_seconds=timeout_seconds or policy.timeout_seconds,
            output_limit_bytes=max(
                policy.stdout_limit_bytes, policy.stderr_limit_bytes
            ),
            termination_grace_seconds=policy.termination_grace_seconds,
        )

    @staticmethod
    def _require_success(
        result: DockerCommandResult,
        code: ValidationExecutionInfrastructureCode,
        message: str,
    ) -> None:
        if result.timed_out or result.exit_code != 0:
            raise ValidationExecutionInfrastructureError(code, message)


def _validate_dependency_requirement(value: str) -> str:
    normalized = value.strip()
    lowered = normalized.casefold()
    if (
        not normalized
        or normalized.startswith(("-", ".", "/", "~"))
        or "@" in normalized
        or "/" in normalized
        or "\\" in normalized
        or any(ord(character) < 32 for character in normalized)
        or any(fragment in lowered for fragment in _FORBIDDEN_REQUIREMENT_FRAGMENTS)
        or _REQUIREMENT_NAME.match(normalized) is None
    ):
        raise ValidationDependencyManifestError(
            "Staged dependency uses an unsupported URL, VCS, path, option, or syntax."
        )
    return normalized


def _verified_staged_snapshot(
    workspace: IsolatedWorkspace, request: ValidationExecutionRequest
):
    try:
        observed = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Disposable validation postimage is unavailable.",
        ) from error
    if (
        observed.workspace_id != request.staged_workspace_id
        or observed.snapshot_id != request.staged_snapshot_id
    ):
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Disposable validation postimage does not match the request.",
        )
    return observed


def _trusted_docker_executable(executable: str | None) -> str:
    supplied = Path(executable or shutil.which("docker") or "")
    try:
        canonical = supplied.resolve(strict=True)
        metadata = canonical.stat()
    except OSError as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Docker executable is unavailable.",
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(canonical, os.X_OK):
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Docker executable is not an executable regular file.",
        )
    return str(canonical)


def _command_outcome(result: DockerCommandResult) -> ValidationExecutionOutcome:
    if result.timed_out:
        return ValidationExecutionOutcome.TIMED_OUT
    if result.exit_code == 0:
        return ValidationExecutionOutcome.PASSED
    return ValidationExecutionOutcome.FAILED


def _content_hash(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
