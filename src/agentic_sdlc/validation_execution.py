"""Governed fixed-profile validation against disposable staged postimages."""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import BinaryIO, Protocol

from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    TaskExecutionValidationResult,
)
from agentic_sdlc.task_graph import ValidationExecutionProfile
from agentic_sdlc.validation_execution_contracts import (
    GovernedValidationExecutionReport,
    GovernedValidationPolicy,
    TaskValidationExecutionEvidence,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    build_validation_execution_evidence,
    python_compile_validation_policy,
)
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationValidationResult,
    WorkspaceChangeSet,
    WorkspaceContractError,
    WorkspaceSnapshot,
    build_workspace_change_set,
    validate_workspace_change_set,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationStatus,
    apply_workspace_change_set,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    create_isolated_workspace,
    discard_isolated_workspace,
    snapshot_isolated_workspace,
)
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedingError,
    seed_isolated_workspace_from_approved_files,
)


class ValidationExecutionInfrastructureCode(StrEnum):
    """Fail-closed categories that are not Task Agent repair work."""

    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    STAGED_WORKSPACE = "STAGED_WORKSPACE"
    PROCESS_START = "PROCESS_START"
    PROCESS_TERMINATION = "PROCESS_TERMINATION"
    OUTPUT_CAPTURE = "OUTPUT_CAPTURE"
    CLEANUP = "CLEANUP"


class ValidationExecutionInfrastructureError(RuntimeError):
    """Governed validation could not produce trustworthy execution evidence."""

    def __init__(
        self,
        code: ValidationExecutionInfrastructureCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class GovernedValidationExecutor(Protocol):
    """Presentation-neutral backend for application-approved validation profiles."""

    def execute(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        workspace: IsolatedWorkspace,
    ) -> TaskValidationExecutionEvidence | GovernedValidationExecutionReport:
        """Execute one application-resolved policy against a staged postimage."""


@dataclass(frozen=True, slots=True)
class StagedValidationWorkspace:
    """Disposable candidate postimage that grants no live-workspace authority."""

    workspace: IsolatedWorkspace
    snapshot: WorkspaceSnapshot


@contextmanager
def disposable_staged_validation_workspace(
    *,
    source_workspace: IsolatedWorkspace,
    source_snapshot: WorkspaceSnapshot,
    task_validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    materialization_validation: ArtifactMaterializationValidationResult,
    authoritative_change_set: WorkspaceChangeSet | None,
    staged_workspace_id: str,
) -> Iterator[StagedValidationWorkspace]:
    """Clone exact authority, apply only one candidate change, then discard it."""

    staged: IsolatedWorkspace | None = None
    try:
        observed_before = snapshot_isolated_workspace(source_workspace)
        if observed_before != source_snapshot:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Authoritative source workspace changed before validation staging.",
            )
        staged = create_isolated_workspace(staged_workspace_id)
        _, staged_base = seed_isolated_workspace_from_approved_files(
            staged,
            source_root=source_workspace.root,
            source_root_label="governed/validation-source",
            relative_paths=tuple(item.path for item in source_snapshot.files),
        )
        observed_after_copy = snapshot_isolated_workspace(source_workspace)
        if observed_after_copy != source_snapshot:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Authoritative source workspace changed during validation staging.",
            )
        if staged_base.files != source_snapshot.files:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Disposable validation baseline differs from attempt authority.",
            )

        if materialization_validation.intents:
            staged_change_set = build_workspace_change_set(
                staged_base,
                task_validation,
                artifacts,
                materialization_validation,
            )
            staged_validation = validate_workspace_change_set(
                staged_change_set,
                staged_base,
                artifacts,
                materialization_validation,
            )
            if not staged_validation.passed:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                    "Disposable staged change set failed deterministic validation.",
                )
            if authoritative_change_set is None or not _same_candidate_changes(
                authoritative_change_set, staged_change_set
            ):
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                    "Disposable staged changes differ from the authorized candidate.",
                )
            mutation = apply_workspace_change_set(
                staged,
                staged_change_set,
                staged_validation,
            )
            if mutation.status is not WorkspaceMutationStatus.APPLIED:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                    "Disposable candidate postimage could not be constructed.",
                )
        elif authoritative_change_set is not None:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Authorized change set exists without materialization intents.",
            )

        staged_snapshot = snapshot_isolated_workspace(staged)
        yield StagedValidationWorkspace(staged, staged_snapshot)
    except (
        WorkspaceContractError,
        WorkspaceRuntimeError,
        WorkspaceSeedingError,
    ) as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
            "Disposable validation workspace could not be constructed.",
        ) from error
    finally:
        if staged is not None:
            try:
                discard_isolated_workspace(staged)
            except WorkspaceRuntimeError as cleanup_error:
                raise ValidationExecutionInfrastructureError(
                    ValidationExecutionInfrastructureCode.CLEANUP,
                    "Disposable validation workspace cleanup failed.",
                ) from cleanup_error


class PythonCompileValidationExecutor:
    """Execute only trusted isolated ``compileall`` over a disposable workspace."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._executable = _trusted_python_executable(executable)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock

    def execute(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        workspace: IsolatedWorkspace,
    ) -> TaskValidationExecutionEvidence:
        expected_policy = python_compile_validation_policy(
            executable=self._executable
        )
        if (
            request.requirement.profile
            is not ValidationExecutionProfile.PYTHON_COMPILE
            or policy != expected_policy
        ):
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE,
                "Execution backend received a non-authoritative validation policy.",
            )
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

        try:
            runtime_root = Path(
                tempfile.mkdtemp(
                    prefix=".validation-runtime-", dir=workspace.root
                )
            )
            home = runtime_root / "home"
            temporary = runtime_root / "tmp"
            home.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
        except OSError as error:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.STAGED_WORKSPACE,
                "Disposable validation runtime directories could not be created.",
            ) from error
        environment = {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        stdout_capture = _BoundedStreamCapture(policy.stdout_limit_bytes)
        stderr_capture = _BoundedStreamCapture(policy.stderr_limit_bytes)
        started_at = self._wall_clock().isoformat()
        started_monotonic = self._monotonic_clock()
        try:
            process = subprocess.Popen(
                policy.argv,
                cwd=workspace.root,
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
                "Governed PYTHON_COMPILE process could not start.",
            ) from error
        if process.stdout is None or process.stderr is None:
            _terminate_process_group(process, policy.termination_grace_seconds)
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                "Governed validation process did not expose bounded output streams.",
            )
        stdout_thread = Thread(
            target=stdout_capture.consume,
            args=(process.stdout,),
            name="validation-stdout",
            daemon=True,
        )
        stderr_thread = Thread(
            target=stderr_capture.consume,
            args=(process.stderr,),
            name="validation-stderr",
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
            _terminate_process_group(process, policy.termination_grace_seconds)
            if stdout_started:
                stdout_thread.join(timeout=policy.termination_grace_seconds)
            if stderr_started:
                stderr_thread.join(timeout=policy.termination_grace_seconds)
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                "Governed validation output capture could not start.",
            ) from error
        timed_out = False
        try:
            process.wait(timeout=policy.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process, policy.termination_grace_seconds)
        except OSError as error:
            _terminate_process_group(process, policy.termination_grace_seconds)
            stdout_thread.join(timeout=policy.termination_grace_seconds)
            stderr_thread.join(timeout=policy.termination_grace_seconds)
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.PROCESS_TERMINATION,
                "Governed validation process state could not be observed.",
            ) from error
        stdout_thread.join(timeout=policy.termination_grace_seconds)
        stderr_thread.join(timeout=policy.termination_grace_seconds)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                "Governed validation output capture did not terminate reliably.",
            )
        if stdout_capture.error is not None or stderr_capture.error is not None:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE,
                "Governed validation output could not be captured reliably.",
            )

        ended_monotonic = self._monotonic_clock()
        ended_at = self._wall_clock().isoformat()
        if timed_out:
            outcome = ValidationExecutionOutcome.TIMED_OUT
        elif process.returncode == 0:
            outcome = ValidationExecutionOutcome.PASSED
        else:
            outcome = ValidationExecutionOutcome.FAILED
        stdout = stdout_capture.result()
        stderr = stderr_capture.result()
        return build_validation_execution_evidence(
            request,
            policy,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=max(0.0, ended_monotonic - started_monotonic),
            outcome=outcome,
            exit_code=process.returncode,
            stdout_total_bytes=stdout.total_bytes,
            stderr_total_bytes=stderr.total_bytes,
            retained_stdout=stdout.retained_text,
            retained_stderr=stderr.retained_text,
            stdout_sha256=stdout.sha256,
            stderr_sha256=stderr.sha256,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
        )


@dataclass(frozen=True, slots=True)
class _CapturedOutput:
    total_bytes: int
    retained_text: str
    sha256: str
    truncated: bool


class _BoundedStreamCapture:
    """Stream/hash all bytes while retaining only one fixed-size prefix."""

    def __init__(self, limit_bytes: int) -> None:
        self._limit_bytes = limit_bytes
        self._total_bytes = 0
        self._retained = bytearray()
        self._digest = hashlib.sha256()
        self.error: OSError | None = None

    def consume(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8192):
                self._total_bytes += len(chunk)
                self._digest.update(chunk)
                remaining = self._limit_bytes - len(self._retained)
                if remaining > 0:
                    self._retained.extend(chunk[:remaining])
        except OSError as error:
            self.error = error
        finally:
            try:
                stream.close()
            except OSError as error:
                self.error = self.error or error

    def result(self) -> _CapturedOutput:
        return _CapturedOutput(
            total_bytes=self._total_bytes,
            retained_text=_sanitize_untrusted_output(bytes(self._retained)),
            sha256=self._digest.hexdigest(),
            truncated=self._total_bytes > len(self._retained),
        )


def _trusted_python_executable(executable: str | None) -> str:
    supplied = Path(executable or os.sys.executable)
    try:
        canonical = supplied.resolve(strict=True)
        metadata = canonical.stat()
    except OSError as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Trusted Python executable is unavailable.",
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(canonical, os.X_OK):
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
            "Trusted Python executable is not an executable regular file.",
        )
    return str(canonical)


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except ProcessLookupError:
        try:
            process.wait(timeout=grace_seconds)
            return
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.PROCESS_TERMINATION,
                "Governed validation process could not be reaped reliably.",
            ) from error
    except subprocess.TimeoutExpired:
        pass
    except OSError as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.PROCESS_TERMINATION,
            "Governed validation process could not be terminated reliably.",
        ) from error
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationExecutionInfrastructureError(
            ValidationExecutionInfrastructureCode.PROCESS_TERMINATION,
            "Governed validation process could not be terminated reliably.",
        ) from error


def _sanitize_untrusted_output(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    safe: list[str] = []
    for character in text:
        code = ord(character)
        if character in {"\n", "\r", "\t"}:
            safe.append(character)
        elif unicodedata.category(character).startswith("C"):
            escape = "x" if code <= 0xFF else "u"
            width = 2 if escape == "x" else 4
            safe.append(f"\\{escape}{code:0{width}x}")
        else:
            safe.append(character)
    return "".join(safe)


def _same_candidate_changes(
    authoritative: WorkspaceChangeSet,
    staged: WorkspaceChangeSet,
) -> bool:
    return tuple(
        (
            item.artifact_id,
            item.artifact_lineage_id,
            item.path,
            item.operation,
            item.expected_preimage_hash,
            item.desired_content_hash,
            item.desired_content,
        )
        for item in authoritative.file_changes
    ) == tuple(
        (
            item.artifact_id,
            item.artifact_lineage_id,
            item.path,
            item.operation,
            item.expected_preimage_hash,
            item.desired_content_hash,
            item.desired_content,
        )
        for item in staged.file_changes
    )
