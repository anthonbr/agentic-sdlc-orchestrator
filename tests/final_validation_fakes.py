"""Deterministic governed final-validation executors for ordinary tests."""

from __future__ import annotations

import hashlib

from agentic_sdlc.task_graph import ValidationExecutionProfile
from agentic_sdlc.validation_execution_contracts import (
    GovernedValidationExecutionReport,
    GovernedValidationPolicy,
    TaskValidationExecutionEvidence,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    build_validation_execution_evidence,
    build_validation_provisioning_evidence,
)
from agentic_sdlc.workspace_runtime import IsolatedWorkspace
from agentic_sdlc.workspace_runtime import snapshot_isolated_workspace


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ScriptedFinalValidationExecutor:
    """Return exact application-owned evidence without launching processes."""

    def __init__(
        self,
        *,
        pytest_outcome: ValidationExecutionOutcome = ValidationExecutionOutcome.PASSED,
        compile_outcome: ValidationExecutionOutcome = ValidationExecutionOutcome.PASSED,
    ) -> None:
        self.pytest_outcome = pytest_outcome
        self.compile_outcome = compile_outcome
        self.calls: list[ValidationExecutionRequest] = []
        self.observed_contents: list[dict[str, str]] = []

    def execute(
        self,
        request: ValidationExecutionRequest,
        policy: GovernedValidationPolicy,
        workspace: IsolatedWorkspace,
    ) -> TaskValidationExecutionEvidence | GovernedValidationExecutionReport:
        self.calls.append(request)
        snapshot = snapshot_isolated_workspace(workspace)
        self.observed_contents.append(
            {
                item.path: (workspace.root / item.path).read_text()
                for item in snapshot.files
            }
        )
        if request.requirement.profile is ValidationExecutionProfile.PYTHON_COMPILE:
            return _execution_evidence(request, policy, self.compile_outcome)

        manifest = request.dependency_manifest
        assert manifest is not None
        container_id = "final-validation-container"
        image_id = "sha256:" + "1" * 64
        provisioning = build_validation_provisioning_evidence(
            request,
            policy,
            container_image_id=image_id,
            container_id=container_id,
            image_pulled=False,
            argv=(*policy.provisioning_argv_prefix, *manifest.normalized_dependencies),
            started_at="2026-08-14T12:00:00+00:00",
            ended_at="2026-08-14T12:00:01+00:00",
            duration_seconds=1.0,
            outcome=ValidationExecutionOutcome.PASSED,
            exit_code=0,
            stdout_total_bytes=0,
            stderr_total_bytes=0,
            retained_stdout="",
            retained_stderr="",
            stdout_sha256=_EMPTY_SHA256,
            stderr_sha256=_EMPTY_SHA256,
            stdout_truncated=False,
            stderr_truncated=False,
            container_cleanup_succeeded=True,
        )
        execution = _execution_evidence(
            request,
            policy,
            self.pytest_outcome,
            provisioning_evidence_ids=(provisioning.evidence_id,),
            container_image_reference=policy.container_image_reference,
            container_image_id=image_id,
            container_id=container_id,
            external_network_disconnected=True,
            container_cleanup_succeeded=True,
        )
        return GovernedValidationExecutionReport(
            provisioning_evidence=(provisioning,), execution_evidence=execution
        )


def _execution_evidence(
    request: ValidationExecutionRequest,
    policy: GovernedValidationPolicy,
    outcome: ValidationExecutionOutcome,
    **container_values: object,
) -> TaskValidationExecutionEvidence:
    exit_code = 0 if outcome is ValidationExecutionOutcome.PASSED else 1
    diagnostic = b"controlled final validation failure" if exit_code else b""
    return build_validation_execution_evidence(
        request,
        policy,
        started_at="2026-08-14T12:00:01+00:00",
        ended_at="2026-08-14T12:00:02+00:00",
        duration_seconds=1.0,
        outcome=outcome,
        exit_code=exit_code,
        stdout_total_bytes=0,
        stderr_total_bytes=len(diagnostic),
        retained_stdout="",
        retained_stderr=diagnostic.decode(),
        stdout_sha256=_EMPTY_SHA256,
        stderr_sha256=hashlib.sha256(diagnostic).hexdigest(),
        stdout_truncated=False,
        stderr_truncated=False,
        **container_values,
    )
