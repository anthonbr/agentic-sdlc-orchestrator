"""Focused contracts and real fixed-profile validation execution tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from pydantic import ValidationError
from pytest import MonkeyPatch, raises

from agentic_sdlc.task_graph import (
    TaskValidationRequirement,
    ValidationExecutionProfile,
)
from agentic_sdlc.validation_execution import (
    PythonCompileValidationExecutor,
    ValidationExecutionInfrastructureCode,
    ValidationExecutionInfrastructureError,
)
from agentic_sdlc.validation_execution_contracts import (
    DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES,
    ValidationDependencyProvisioning,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    build_validation_execution_evidence,
    python_compile_validation_policy,
    resolve_governed_validation_policy,
    validation_execution_evidence_errors,
    validation_execution_evidence_identity_is_valid,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    create_isolated_workspace,
    snapshot_isolated_workspace,
)


def _workspace(tmp_path: Path, files: dict[str, str]) -> IsolatedWorkspace:
    workspace = create_isolated_workspace(
        "VALIDATION-INTEGRATION-WORKSPACE",
        parent_directory=tmp_path,
    )
    for relative_path, content in files.items():
        target = workspace.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return workspace


def _request(workspace: IsolatedWorkspace) -> ValidationExecutionRequest:
    snapshot = snapshot_isolated_workspace(workspace)
    return ValidationExecutionRequest(
        run_id="RUN-VALIDATION-INTEGRATION",
        graph_id="GRAPH-VALIDATION-INTEGRATION-V001",
        graph_version=1,
        task_id="TASK-001",
        request_id="REQUEST-VALIDATION-INTEGRATION",
        attempt_id="ATTEMPT-VALIDATION-INTEGRATION",
        attempt_number=1,
        requirement=TaskValidationRequirement(
            requirement_id="TASK-001-VALIDATION-001",
            profile=ValidationExecutionProfile.PYTHON_COMPILE,
        ),
        source_workspace_id=workspace.workspace_id,
        source_snapshot_id=snapshot.snapshot_id,
        staged_workspace_id=workspace.workspace_id,
        staged_snapshot_id=snapshot.snapshot_id,
    )


def test_python_compile_valid_source_passes_with_content_bound_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {"src/example.py": "def answer() -> int:\n    return 42\n"},
    )

    request = _request(workspace)
    monotonic_values = iter((10.0, 12.5))
    fixed_wall_time = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    evidence = PythonCompileValidationExecutor(
        monotonic_clock=lambda: next(monotonic_values),
        wall_clock=lambda: fixed_wall_time,
    ).execute(
        request, python_compile_validation_policy(), workspace
    )

    assert evidence.profile is ValidationExecutionProfile.PYTHON_COMPILE
    assert evidence.outcome is ValidationExecutionOutcome.PASSED
    assert evidence.exit_code == 0
    assert evidence.passed is True
    assert evidence.timed_out is False
    assert evidence.started_at == fixed_wall_time.isoformat()
    assert evidence.ended_at == fixed_wall_time.isoformat()
    assert evidence.duration_seconds == 2.5
    assert evidence.dependency_provisioning is ValidationDependencyProvisioning.NONE
    assert evidence.network_access_allowed is False
    assert evidence.argv[1:] == ("-I", "-B", "-m", "compileall", "-q", ".")
    assert evidence.working_directory == "."
    assert evidence.environment_variable_names == (
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    )
    assert validation_execution_evidence_identity_is_valid(evidence)

    observations = {
        "started_at": evidence.started_at,
        "ended_at": evidence.ended_at,
        "duration_seconds": evidence.duration_seconds,
        "outcome": evidence.outcome,
        "exit_code": evidence.exit_code,
        "stdout_total_bytes": evidence.stdout_total_bytes,
        "stderr_total_bytes": evidence.stderr_total_bytes,
        "retained_stdout": evidence.retained_stdout,
        "retained_stderr": evidence.retained_stderr,
        "stdout_sha256": evidence.stdout_sha256,
        "stderr_sha256": evidence.stderr_sha256,
        "stdout_truncated": evidence.stdout_truncated,
        "stderr_truncated": evidence.stderr_truncated,
    }
    repeated = build_validation_execution_evidence(
        request, python_compile_validation_policy(), **observations
    )
    next_attempt = build_validation_execution_evidence(
        request.model_copy(
            update={"attempt_id": "ATTEMPT-VALIDATION-NEXT", "attempt_number": 2}
        ),
        python_compile_validation_policy(),
        **observations,
    )
    assert repeated.evidence_id == evidence.evidence_id
    assert next_attempt.evidence_id != evidence.evidence_id


def test_python_compile_invalid_source_fails_without_executing_it(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    workspace = _workspace(
        tmp_path,
        {
            "src/invalid.py": "def invalid(:\n    pass\n",
            "src/side_effect.py": (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
            ),
        },
    )

    evidence = PythonCompileValidationExecutor().execute(
        _request(workspace), python_compile_validation_policy(), workspace
    )

    assert evidence.outcome is ValidationExecutionOutcome.FAILED
    assert evidence.exit_code != 0
    assert evidence.passed is False
    assert evidence.stdout_total_bytes > 0
    assert evidence.stdout_total_bytes == len(
        evidence.retained_stdout.encode("utf-8")
    )
    assert evidence.stdout_sha256 == hashlib.sha256(
        evidence.retained_stdout.encode("utf-8")
    ).hexdigest()
    assert evidence.stderr_total_bytes == 0
    assert evidence.stderr_sha256 == hashlib.sha256(b"").hexdigest()
    assert not marker.exists()


def test_isolated_python_startup_ignores_staged_sitecustomize(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "sitecustomize-must-not-run"
    workspace = _workspace(
        tmp_path,
        {
            "sitecustomize.py": (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
            ),
            "module.py": "VALUE = 1\n",
        },
    )

    evidence = PythonCompileValidationExecutor().execute(
        _request(workspace), python_compile_validation_policy(), workspace
    )

    assert evidence.passed is True
    assert not marker.exists()


def test_python_compile_uses_argv_shell_false_and_minimal_environment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})
    real_popen = subprocess.Popen
    observed: dict[str, object] = {}

    def recording_popen(*args: object, **kwargs: object):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("agentic_sdlc.validation_execution.subprocess.Popen", recording_popen)

    evidence = PythonCompileValidationExecutor().execute(
        _request(workspace), python_compile_validation_policy(), workspace
    )

    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {"HOME", "TMPDIR", "LANG", "LC_ALL"}
    assert "OPENAI_API_KEY" not in environment
    assert evidence.passed is True


def test_python_compile_output_retention_is_bounded_and_hashed(
    tmp_path: Path,
) -> None:
    files = {
        f"bad/file_{index:04d}.py": "def invalid(:\n    pass\n"
        for index in range(350)
    }
    workspace = _workspace(tmp_path, files)

    evidence = PythonCompileValidationExecutor().execute(
        _request(workspace), python_compile_validation_policy(), workspace
    )

    assert evidence.passed is False
    assert evidence.stdout_total_bytes > DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES
    assert len(evidence.retained_stdout.encode("utf-8")) <= (
        DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES
    )
    assert evidence.stdout_truncated is True
    assert len(evidence.stdout_sha256) == 64
    assert evidence.stdout_sha256 != hashlib.sha256(b"").hexdigest()


def test_python_compile_timeout_terminates_process_group_and_returns_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})

    class TimedOutProcess:
        pid = 4242
        returncode: int | None = None
        stdout = BytesIO(b"partial \x1b[31moutput\xc2\x9b")
        stderr = BytesIO(b"")

        def wait(self, timeout: float) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("compileall", timeout)
            return self.returncode

    process = TimedOutProcess()
    signals: list[int] = []

    def fake_popen(*args: object, **kwargs: object) -> TimedOutProcess:
        del args, kwargs
        return process

    def terminate_group(pid: int, sent_signal: int) -> None:
        assert pid == process.pid
        signals.append(sent_signal)
        process.returncode = -sent_signal

    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr("agentic_sdlc.validation_execution.os.killpg", terminate_group)

    evidence = PythonCompileValidationExecutor().execute(
        _request(workspace), python_compile_validation_policy(), workspace
    )

    assert signals == [15]
    assert evidence.outcome is ValidationExecutionOutcome.TIMED_OUT
    assert evidence.timed_out is True
    assert evidence.passed is False
    assert evidence.exit_code == -15
    assert evidence.retained_stdout == "partial \\x1b[31moutput\\x9b"


def test_python_compile_unreliable_timeout_termination_fails_closed(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})

    class UnterminatedProcess:
        pid = 4343
        returncode: int | None = None
        stdout = BytesIO(b"")
        stderr = BytesIO(b"")

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("compileall", timeout)

    process = UnterminatedProcess()
    signal_count = 0

    def fake_popen(*args: object, **kwargs: object) -> UnterminatedProcess:
        del args, kwargs
        return process

    def unreliable_killpg(pid: int, sent_signal: int) -> None:
        nonlocal signal_count
        assert pid == process.pid
        signal_count += 1
        if signal_count == 2:
            raise PermissionError("Controlled termination denial.")

    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.os.killpg", unreliable_killpg
    )

    with raises(ValidationExecutionInfrastructureError) as captured:
        PythonCompileValidationExecutor().execute(
            _request(workspace), python_compile_validation_policy(), workspace
        )

    assert captured.value.code is (
        ValidationExecutionInfrastructureCode.PROCESS_TERMINATION
    )


def test_python_compile_process_start_failure_is_infrastructure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})

    def fail_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("Controlled process-start failure.")

    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.subprocess.Popen",
        fail_start,
    )

    with raises(ValidationExecutionInfrastructureError) as captured:
        PythonCompileValidationExecutor().execute(
            _request(workspace), python_compile_validation_policy(), workspace
        )

    assert captured.value.code is ValidationExecutionInfrastructureCode.PROCESS_START


def test_python_compile_output_capture_failure_is_infrastructure_after_reap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})

    class FailingStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            del size
            raise OSError("Controlled output-reader failure.")

    class ReapedProcess:
        pid = 4444
        returncode: int | None = None
        stdout = FailingStream()
        stderr = BytesIO(b"")
        wait_calls = 0

        def wait(self, timeout: float) -> int:
            del timeout
            self.wait_calls += 1
            self.returncode = 0
            return 0

    process = ReapedProcess()

    def fake_popen(*args: object, **kwargs: object) -> ReapedProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(
        "agentic_sdlc.validation_execution.subprocess.Popen",
        fake_popen,
    )

    with raises(ValidationExecutionInfrastructureError) as captured:
        PythonCompileValidationExecutor().execute(
            _request(workspace), python_compile_validation_policy(), workspace
        )

    assert captured.value.code is ValidationExecutionInfrastructureCode.OUTPUT_CAPTURE
    assert process.wait_calls == 1
    assert process.returncode == 0


def test_evidence_schema_rejects_unsupported_profile() -> None:
    workspace_data = {
        "evidence_id": "VALIDATION-EVIDENCE-CONTROLLED",
        "run_id": "RUN",
        "graph_id": "GRAPH",
        "graph_version": 1,
        "task_id": "TASK-001",
        "request_id": "REQUEST",
        "attempt_id": "ATTEMPT",
        "attempt_number": 1,
        "validation_requirement_id": "TASK-001-VALIDATION-001",
        "profile": "PYTHON_SHELL",
        "policy_id": "POLICY",
        "policy_version": "version",
        "source_workspace_id": "SOURCE",
        "source_snapshot_id": "SNAPSHOT",
        "staged_workspace_id": "STAGED",
        "staged_snapshot_id": "STAGED-SNAPSHOT",
        "environment_kind": "LOCAL_DISPOSABLE",
        "dependency_provisioning": "NONE",
        "network_access_allowed": False,
        "provisioning_evidence_ids": [],
        "argv": ["python"],
        "working_directory": ".",
        "environment_variable_names": [],
        "started_at": "2026-08-14T12:00:00+00:00",
        "ended_at": "2026-08-14T12:00:01+00:00",
        "duration_seconds": 1.0,
        "outcome": "PASSED",
        "exit_code": 0,
        "timed_out": False,
        "stdout_total_bytes": 0,
        "stderr_total_bytes": 0,
        "retained_stdout": "",
        "retained_stderr": "",
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "passed": True,
    }

    from agentic_sdlc.validation_execution_contracts import (
        TaskValidationExecutionEvidence,
    )

    with raises(ValidationError, match="profile"):
        TaskValidationExecutionEvidence.model_validate_json(
            json.dumps(workspace_data)
        )


def test_python_compile_policy_has_no_provisioning_or_general_command_surface() -> None:
    policy = resolve_governed_validation_policy(
        ValidationExecutionProfile.PYTHON_COMPILE
    )

    assert policy.profile is ValidationExecutionProfile.PYTHON_COMPILE
    assert policy.argv[1:] == ("-I", "-B", "-m", "compileall", "-q", ".")
    assert policy.dependency_provisioning is ValidationDependencyProvisioning.NONE
    assert policy.network_access_allowed is False
    assert policy.stdout_limit_bytes == DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES
    assert policy.stderr_limit_bytes == DEFAULT_VALIDATION_OUTPUT_LIMIT_BYTES


def test_backend_rejects_policy_not_resolved_by_application(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})
    altered = python_compile_validation_policy().model_copy(
        update={"timeout_seconds": 999.0}
    )

    with raises(ValidationExecutionInfrastructureError) as captured:
        PythonCompileValidationExecutor().execute(
            _request(workspace), altered, workspace
        )

    assert captured.value.code is (
        ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE
    )


def test_execution_evidence_rejects_every_altered_correlation_dimension(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"module.py": "VALUE = 1\n"})
    request = _request(workspace)
    policy = python_compile_validation_policy()
    evidence = build_validation_execution_evidence(
        request,
        policy,
        started_at="2026-08-14T12:00:00+00:00",
        ended_at="2026-08-14T12:00:01+00:00",
        duration_seconds=1.0,
        outcome=ValidationExecutionOutcome.PASSED,
        exit_code=0,
        stdout_total_bytes=0,
        stderr_total_bytes=0,
        retained_stdout="",
        retained_stderr="",
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        stdout_truncated=False,
        stderr_truncated=False,
    )
    altered_values = {
        "graph_id": "GRAPH-OTHER-V001",
        "graph_version": 2,
        "task_id": "TASK-999",
        "request_id": "REQUEST-OTHER",
        "attempt_id": "ATTEMPT-OTHER",
        "attempt_number": 2,
        "validation_requirement_id": "TASK-999-VALIDATION-001",
        "policy_id": "VALIDATION-POLICY-OTHER",
        "policy_version": "python-compile-other",
        "source_workspace_id": "WORKSPACE-OTHER",
        "source_snapshot_id": "SNAPSHOT-OTHER",
        "staged_workspace_id": "STAGED-WORKSPACE-OTHER",
        "staged_snapshot_id": "STAGED-SNAPSHOT-OTHER",
        "argv": (*policy.argv[:-1], "other-target"),
    }

    for field_name, altered_value in altered_values.items():
        altered = evidence.model_copy(update={field_name: altered_value})
        errors = validation_execution_evidence_errors(request, policy, altered)
        assert field_name in errors
        assert "evidence_id" in errors


def test_validation_profile_correlation_is_closed_by_schema() -> None:
    with raises(ValidationError, match="profile"):
        TaskValidationRequirement.model_validate(
            {
                "requirement_id": "TASK-001-VALIDATION-001",
                "profile": "PYTHON_SHELL",
            }
        )
